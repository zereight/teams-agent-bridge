"""Patch4: the startup resync-notice loop and swallowed /status during resync."""
import json

import pytest

from agent_runner import AgentRunner
from audit import Audit
from backend_base import ChatBackend, ChatGuardError, ChatMessage
from bridge import Bridge
from config import DEFAULTS, _deep_merge
from state import FP_VERSION, StateStore


# ----------------------------------------------- (2) fingerprint-scheme migration
def test_old_state_migrated_silently(tmp_path):
    p = str(tmp_path / "s.json")
    # a state.json written by an OLD version: initialized, old anchors, no fp_version
    json.dump({"version": 1, "initialized": True,
               "seen_tail": ["OLDFP1", "OLDFP2"], "processed": ["OLDOCC1"],
               "paused": False, "recent_sent": ["something"]},
              open(p, "w"))
    st = StateStore(p)
    assert st.data["fp_version"] == FP_VERSION
    assert st.data["seen_tail"] == [] and st.data["processed"] == []
    assert not st.initialized          # -> next read re-inits silently (no resync spam)
    assert st.recent_sent == ["something"]   # sent memory is preserved
    # first read after migration = init (returns None), NOT resync
    out = st.filter_new([ChatMessage("hello from before", "13:00")])
    assert out is None and st.last_event == "init"


# ----------------------------------------------- bridge harness
class ScriptedChat(ChatBackend):
    """Returns whatever visible list the test sets; records sent lines."""

    def __init__(self):
        super().__init__()
        self.visible = []
        self.sent = []
        self.title = "Chat | Alex Kim (You) | Microsoft Teams"

    def focus_self_chat(self):
        if "(You)" not in self.title:
            raise ChatGuardError(self.title)
        return self.title

    def read_visible_messages(self):
        return list(self.visible)

    def send_message(self, text):
        self.sent.append(text)

    def bot(self):
        return [t for t in self.sent if t.startswith("(b)")]


def _bridge(tmp_path, **over):
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 5,
                  "custom": {"command": ["true"]}},
        "reply": {"announce_start": True},
    })
    cfg = _deep_merge(cfg, over)
    chat = ScriptedChat()
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    clk = [1000.0]
    # Inject a FIXED wall clock so the Teams time-gate is deterministic regardless
    # of the real time of day. now = today 13:00; bridge start = today 00:05, so a
    # bare "HH:MM" timestamp earlier than 13:00 parses to today and passes the gate.
    from datetime import datetime
    now = datetime.now().replace(hour=13, minute=0, second=0, microsecond=0)
    start = now.replace(hour=0, minute=5)
    br = Bridge(cfg, chat, runner, st, audit, clock=lambda: clk[0],
                lock_check=lambda: False, now_dt=lambda: now, start_time=start)
    return br, chat, st, clk


# ----------------------------------------------- (1)(2) anchor missing every poll
def test_anchor_missing_every_poll_posts_at_most_one_notice(tmp_path):
    br, chat, st, clk = _bridge(tmp_path)
    # init
    chat.visible = [ChatMessage("seed", "12:00")]
    br.cycle()
    base_notices = sum("위치를 다시 맞췄" in b for b in chat.bot())
    assert base_notices == 0
    # now every poll shows a brand-new, never-aligning, UNTIMESTAMPED junk message
    notices = 0
    for i in range(20):
        clk[0] += 18          # 18s between polls, like the live bug
        chat.visible = [ChatMessage(f"junk-render-{i}")]   # no timestamp, never aligns
        br.cycle()
    notices = sum("위치를 다시 맞췄" in b for b in chat.bot())
    assert notices <= 1, f"posted {notices} resync notices"


def test_resync_notice_not_posted_for_own_bot_messages(tmp_path):
    br, chat, st, clk = _bridge(tmp_path)
    chat.visible = [ChatMessage("seed", "12:00")]
    br.cycle()
    # the bridge's own reply comes back shuffled & unaligned (bot-only change)
    reply = br.prefix + "완료 (claude, 3초)\n결과 문단"
    st.remember_sent(reply)
    clk[0] += 30
    chat.visible = [ChatMessage("결과 문단완료 (claude, 3초)")]   # shuffled echo, no ts anyway
    br.cycle()
    assert not any("위치를 다시 맞췄" in b for b in chat.bot())


# ----------------------------------------------- (3) /status during resync answered
def test_status_during_resync_is_answered(tmp_path):
    br, chat, st, clk = _bridge(tmp_path)
    chat.visible = [ChatMessage("old1", "11:00"), ChatMessage("old2", "12:00")]
    br.cycle()                                   # init (both marked processed)
    # a poll where the view jumped (anchor lost) AND the user's /status is at the
    # tail. old2 is still visible (processed); /status is freshly typed.
    clk[0] += 20
    chat.visible = [ChatMessage("old2", "12:00"), ChatMessage("/status", "12:30")]
    br.cycle()
    joined = "\n".join(chat.bot())
    assert "상태:" in joined                      # /status really got answered
    assert "위치를 다시 맞췄" not in joined         # and no apology notice (user msg present)
