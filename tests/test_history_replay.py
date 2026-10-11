"""Regression tests for the patch7 history-replay bug: a restart (or a scrolled /
virtualized AX view) must NEVER execute yesterday's (or any pre-startup) messages,
even an old 'ㅇㅇ' that could approve a push."""
import json
import sys
from datetime import datetime

from agent_runner import AgentRunner
from audit import Audit
from backend_base import ChatBackend, ChatGuardError, ChatMessage
from bridge import Bridge
from config import DEFAULTS, _deep_merge
from roles import RoleStore
from state import StateStore

AGENT = [sys.executable, "-c", "import sys; sys.stdin.read(); print('ok')"]
START = datetime(2026, 10, 11, 7, 0, 0)


class TsChat(ChatBackend):
    """Fake Teams whose messages carry timestamps; the visible list can be swapped
    to simulate scrolling / virtualization exposing more rows."""

    def __init__(self, msgs):
        super().__init__()
        self.msgs = list(msgs)                 # list of (text, ts)
        self.title = "Chat | Alex Kim (You) | Microsoft Teams"

    def focus_self_chat(self):
        if "(You)" not in self.title:
            raise ChatGuardError(self.title)
        return self.title

    def read_visible_messages(self):
        return [ChatMessage(text=t, timestamp=ts) for t, ts in self.msgs]

    def send_message(self, text):
        self.msgs.append((text, "오늘 07:10"))   # bot replies are "now"

    def add(self, text, ts):
        self.msgs.append((text, ts))


def _mk(chat, tmp_path):
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 5,
                  "custom": {"command": AGENT}},
        "reply": {"announce_start": True, "max_chars": 500, "ack_on_start": False},
    })
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    roles = RoleStore(str(tmp_path / "roles.json"))
    br = Bridge(cfg, chat, runner, st, audit, roles=roles, lock_check=lambda: False,
               start_time=START, now_dt=lambda: START)
    return br, chat, runner, audit


def _events(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _count(path, event):
    return sum(1 for e in _events(path) if e.get("event") == event)


def _actions(path):
    return [e.get("action") for e in _events(path) if e.get("event") == "decision"]


def test_restart_never_runs_visible_history(tmp_path):
    # Everything on screen at startup is yesterday's / before-start history,
    # including an old danger command and an old 'ㅇㅇ'.
    chat = TsChat([
        ("/role add dev 코드 담당", "어제 16:14."),
        ("@dev package.json 알려줘", "어제 16:16."),
        ("git push 해줘", "어제 16:25."),
        ("ㅇㅇ", "어제 16:26."),
        ("README 요약", "오늘 06:30"),           # today but BEFORE 07:00 start
    ])
    br, chat, runner, audit = _mk(chat, tmp_path)
    br.cycle()                                   # startup baseline
    for _ in range(3):
        br.cycle(); runner.wait(5); br.cycle()
    assert _count(audit.path, "agent_start") == 0
    assert "ask_approval" not in _actions(audit.path)
    assert "approved" not in _actions(audit.path)

    # a genuinely new message, after start, DOES run
    chat.add("README 다시 요약", "오늘 07:05")
    br.cycle(); runner.wait(10); br.cycle()
    assert _count(audit.path, "agent_start") == 1


def test_scrolled_in_old_history_is_time_gated(tmp_path):
    # Baseline sees only one recent message...
    chat = TsChat([("(어제 대화)", "어제 15:00.")])
    br, chat, runner, audit = _mk(chat, tmp_path)
    br.cycle()                                   # baseline = {(어제 대화)}

    # ...then the AX tree exposes MORE (older) rows at the bottom: anchor is lost,
    # so filter_new resyncs and surfaces them - but the time gate blocks each one.
    chat.msgs = [
        ("/role add dev x", "어제 16:14."),
        ("git push 해줘", "어제 16:25."),
        ("ㅇㅇ", "어제 16:26."),
    ]
    br.cycle(); runner.wait(10); br.cycle()
    assert _count(audit.path, "agent_start") == 0
    assert _count(audit.path, "ignore_old") >= 1
    assert "ask_approval" not in _actions(audit.path)

    # a new message after start still runs exactly once
    chat.add("README 요약", "오늘 07:05")
    br.cycle(); runner.wait(10); br.cycle()
    assert _count(audit.path, "agent_start") == 1


def test_old_approval_cannot_approve_in_process(tmp_path):
    # A real danger prompt is posted now; then an OLD 'ㅇㅇ' scrolls in. It must be
    # time-gated, never approving the pending push.
    chat = TsChat([("(어제 대화)", "어제 15:00.")])
    br, chat, runner, audit = _mk(chat, tmp_path)
    br.cycle()
    chat.add("git push 해줘", "오늘 07:03")        # new danger command -> prompt
    br.cycle()
    assert "ask_approval" in _actions(audit.path)
    chat.add("ㅇㅇ", "어제 16:26.")                 # replayed old approval
    br.cycle(); runner.wait(10); br.cycle()
    assert "approved" not in _actions(audit.path)
    assert _count(audit.path, "agent_start") == 0
