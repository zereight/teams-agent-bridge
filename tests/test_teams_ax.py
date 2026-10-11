"""Drive TeamsAXBackend with a fake AX tree + fake desktop (no pyobjc, no Teams)."""
import pytest

import ax_select
from backend_base import ChatGuardError
from config import DEFAULTS, _deep_merge
from teams_ax import TeamsAXBackend
from tests.test_ax_select import FakeAX, Node


class FakeDesktop:
    def __init__(self, bundle="com.microsoft.teams2"):
        self.front = bundle
        self.bundle = bundle
        self.clip = "original-clip"
        self.sent = []
        self.actions = []

    def pid_for(self, b):
        return 123 if b == self.bundle else None

    def activate(self, b):
        self.front = b
        self.actions.append(("activate", b))

    def frontmost_bundle_id(self):
        return self.front

    def get_clipboard(self):
        return self.clip

    def set_clipboard(self, t):
        self.clip = t

    def select_all_delete(self):
        self.actions.append(("clear",))

    def paste(self):
        # emulate paste into compose + enter -> message appears in the list
        self.actions.append(("paste", self.clip))

    def press_return(self):
        self.sent.append(self.clip)
        self.actions.append(("return",))


class FakeAXApp(FakeAX):
    """FakeAX plus app-element lookups and set_attr."""
    def __init__(self, app):
        self.app = app
        self.sets = []

    def app_element(self, pid):
        return self.app

    def set_attr(self, el, name, value):
        self.sets.append((name, value))
        return True

    def frame(self, el):
        return (0.0, 0.0, 1000.0, 800.0)


def build_tree(header_value="Alex Kim (You)"):
    compose = Node(role="AXTextArea", desc="메시지 입력")
    lst = Node(role="AXList", ident="message-pane", children=[
        Node(role="AXGroup", children=[Node(role="AXStaticText", value="예전 메시지")]),
    ])
    win = Node(role="AXWindow", title="Microsoft Teams", children=[
        Node(role="AXStaticText", value=header_value),
        lst, compose,
    ])
    app = Node(role="AXApplication", children=[win])
    app.attrs["AXFocusedWindow"] = win
    app.attrs["AXWindows"] = [win]
    app.attrs["AXMainWindow"] = win
    return app, lst


def make(cfg_over=None, header="Alex Kim (You)"):
    app, lst = build_tree(header)
    ax = FakeAXApp(app)
    desk = FakeDesktop()
    over = {"ax": {"message_list": {"role": "AXList", "identifier": "message-pane"},
                   "message_strategy": "generic",
                   "message_item": {"role": "AXGroup", "depth": 1},
                   "chat_header": {"role": "AXStaticText", "value_re": r"\(You\)|\(\ub098\)"},
                   "compose_box": {"role": "AXTextArea"}}}
    if cfg_over:
        over = _deep_merge(over, cfg_over)
    cfg = _deep_merge(DEFAULTS, over)
    be = TeamsAXBackend(cfg, ax=ax, desktop=desk, sleep=lambda *_: None)
    return be, ax, desk, lst


def test_focus_and_title_guard():
    be, ax, desk, lst = make()
    assert "(You)" in be.focus_self_chat()
    be2, _, _, _ = make(header="팀장님")
    with pytest.raises(ChatGuardError):
        be2.focus_self_chat()


def test_read_messages_requires_list():
    be, ax, desk, lst = make({"ax": {"message_list": None}})
    with pytest.raises(Exception):
        be.read_visible_messages()


def test_read_messages():
    be, ax, desk, lst = make()
    lst.kids.append(Node(role="AXGroup", children=[Node(role="AXStaticText", value="(b) 완료")]))
    lst.kids.append(Node(role="AXGroup", children=[Node(role="AXStaticText", value="테스트 돌려줘")]))
    msgs = be.read_visible_messages()
    assert [m.text for m in msgs] == ["예전 메시지", "(b) 완료", "테스트 돌려줘"]


def test_send_message_verifies_frontmost_and_restores_clipboard():
    be, ax, desk, lst = make()
    be.send_message("(b) 안녕하세요")
    assert desk.sent == ["(b) 안녕하세요"]
    assert desk.clip == "original-clip"               # restored
    assert ("activate", "com.microsoft.teams2") in desk.actions
    assert ("clear",) in desk.actions                 # cleared draft before paste
    assert ("AXFocused", True) in ax.sets             # focused the compose box


def test_send_aborts_if_not_frontmost():
    be, ax, desk, lst = make()

    def steal(_b):
        desk.front = "com.apple.Safari"          # user switched away during activate
    desk.activate = steal
    with pytest.raises(ChatGuardError):
        be.send_message("(b) x")
    assert desk.sent == []


def test_manual_accessibility_enabled():
    be, ax, desk, lst = make()
    be.read_visible_messages()
    assert ("AXManualAccessibility", True) in ax.sets
