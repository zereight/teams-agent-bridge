"""Extraction against the sanitized real-structure fixture (fake bodies)."""
import re

import ax_select
import safety
from config import DEFAULTS, _deep_merge
from state import base_fingerprint
from teams_ax import TeamsAXBackend
from tests.fixtures.mac_axtree import FakeMacAX, build_tree


class FakeDesktop:
    def __init__(self, bundle="com.microsoft.teams2"):
        self.front = bundle
        self.bundle = bundle
        self.clip = "orig"
        self.sent = []

    def pid_for(self, b):
        return 1 if b == self.bundle else None

    def activate(self, b):
        self.front = b

    def frontmost_bundle_id(self):
        return self.front

    def get_clipboard(self):
        return self.clip

    def set_clipboard(self, t):
        self.clip = t

    def select_all_delete(self):
        pass

    def paste(self):
        pass

    def press_return(self):
        self.sent.append(self.clip)


class FakeAXApp(FakeMacAX):
    def __init__(self, app):
        self.app = app
        self.sets = []

    def app_element(self, pid):
        return self.app

    def set_attr(self, el, name, value):
        self.sets.append((name, value))
        return True

    def frame(self, el):
        return (0.0, 0.0, 800.0, 600.0)


MAC_OVER = {
    "self_chat": {"title_regex": r"Alex Kim \(Alex\)\(나\)"},
    "ax": {
        "message_list": {"role": "AXGroup", "description_re": r"^메시지 목록$"},
        "message_strategy": "timegroup",
        "compose_box": {"role": "AXTextArea", "description_re": "메시지를 입력"},
    },
}


def make_backend():
    cfg = _deep_merge(DEFAULTS, MAC_OVER)
    app = build_tree()
    ax = FakeAXApp(app)
    desk = FakeDesktop()
    return TeamsAXBackend(cfg, ax=ax, desktop=desk, sleep=lambda *_: None), ax, desk


def test_pure_extraction_full_body_and_timestamp():
    ax = FakeMacAX()
    app = build_tree()
    mlist = ax_select.find_one(ax, app, {"role": "AXGroup", "description_re": r"^메시지 목록$"})
    rows = ax_select.extract_messages_timegroup(ax, mlist, status_words=["편집됨", "전송됨", "전송 안 됨"])
    bodies = [r["text"] for r in rows]
    # every message has a full timestamp from AXTimeGroup desc (ends with '.')
    assert rows and all(r["timestamp"].endswith(".") for r in rows)
    assert all(r["sender"] == "Alex Kim (Alex)" for r in rows)
    # FULL body from tokens, NOT the truncated heading/desc (which ended with …)
    long_msg = next(b for b in bodies if b.startswith("FAKE"))
    assert "gggg" in long_msg and "…" not in long_msg
    # link-preview card text must be excluded
    report = next(b for b in bodies if b.startswith("report link"))
    assert "Preview Title" not in report and "preview.example.com" not in report
    # image-only message falls back to the body group desc
    assert "이미지" in bodies
    # space tokens reconstruct a normal typed command
    assert "로그인 테스트 돌려줘" in bodies


def test_b_detection_runs_on_body_start():
    ax = FakeMacAX()
    mlist = ax_select.find_one(ax, build_tree(), {"role": "AXGroup", "description_re": r"^메시지 목록$"})
    rows = ax_select.extract_messages_timegroup(ax, mlist, status_words=["편집됨", "전송됨"])
    prefixes = DEFAULTS["bot_prefixes_detect"]
    bot = [r for r in rows if safety.is_bot_message(r["text"], prefixes)]
    assert len(bot) == 1 and bot[0]["text"].startswith("(b)")
    cmds = [r for r in rows if not safety.is_bot_message(r["text"], prefixes)]
    assert any(c["text"] == "로그인 테스트 돌려줘" for c in cmds)


def test_timestamp_is_part_of_fingerprint():
    # two different timestamps on identical text -> different fingerprints
    assert base_fingerprint("하이", "오늘 14:18.") != base_fingerprint("하이", "오늘 14:20.")


def test_backend_read_and_title_guard():
    be, ax, desk = make_backend()
    assert "Alex Kim (Alex)(나)" in be.focus_self_chat()
    msgs = be.read_visible_messages()
    assert msgs and all(m.timestamp for m in msgs)
    assert msgs[-1].text == "로그인 테스트 돌려줘"
    assert msgs[-1].extra.get("sender") == "Alex Kim (Alex)"
    # AXManualAccessibility was requested so the web tree builds
    assert ("AXManualAccessibility", True) in ax.sets


def test_backend_rejects_wrong_chat():
    import pytest
    from backend_base import ChatGuardError
    cfg = _deep_merge(DEFAULTS, MAC_OVER)
    app = build_tree(title="채팅 | 팀장님 | Microsoft Teams")
    be = TeamsAXBackend(cfg, ax=FakeAXApp(app), desktop=FakeDesktop(), sleep=lambda *_: None)
    with pytest.raises(ChatGuardError):
        be.focus_self_chat()


def test_backend_send_uses_compose_and_restores_clipboard():
    be, ax, desk = make_backend()
    be.send_message("(b) 안녕")
    assert desk.sent == ["(b) 안녕"] and desk.clip == "orig"
