"""Regression tests for the live self-echo runaway loop (patch3).

Covers: (1) multi-paragraph body ordering, (2) position-independent bot-prefix
detection, (3) persistent sent-message / per-line echo memory, (4) the exact
loop reproduced with a fake chat that echoes replies back shuffled, and (5) the
hard loop guard.
"""
import sys

import pytest

import ax_select
import safety
from agent_runner import AgentRunner
from audit import Audit
from backend_base import ChatBackend, ChatGuardError, ChatMessage
from bridge import Bridge
from config import DEFAULTS, _deep_merge
from state import StateStore, norm_nospace
from tests.fixtures.mac_axtree import FakeMacAX, reversed_multiline_body


# ------------------------------------------------------------------ (1) ordering
def test_reversed_multiline_body_restored_to_visual_order():
    ax = FakeMacAX()
    lines = ["(b) 완료 (claude, 7초, exit 0, 1턴, 약 $0.01)",
             "첫 번째 문단입니다.",
             "두 번째 문단입니다.",
             "(로그: /tmp/out/x.txt)"]
    body = reversed_multiline_body(lines)
    out = ax_select.order_body_text(ax, body, body_join="")
    assert out == "\n".join(lines)              # correct top-to-bottom order
    assert out.startswith("(b) 완료")           # prefix is back at the very start


def test_order_falls_back_to_tree_order_without_frames():
    # the standard fixture has no frames -> document order, no crash
    from tests.fixtures.mac_axtree import _body_group
    ax = FakeMacAX()
    bg = _body_group("d", ["a", "b", "c"])
    assert ax_select.order_body_text(ax, bg, body_join="") == "abc"


# -------------------------------------------------- (2) position-independent prefix
PREFIXES = DEFAULTS["bot_prefixes_detect"]


@pytest.mark.parametrize("text", [
    "(b) 상태: 동작 중",                                   # at the start
    "(로그: /x/x.txt)`# ExampleApp Workspace`(b) 완료 (claude, 3초)",  # after ) and `
    "줄1\n줄2\n(b) 끝",                                  # a later line starts with it
])
def test_bot_prefix_detected_anywhere(text):
    # defence #2: prefix found even when it is not at char 0 (boundary before it)
    assert safety.has_bot_prefix(text, PREFIXES)
    assert safety.is_bot_message(text, PREFIXES)


@pytest.mark.parametrize("text", [
    "로그인 테스트 돌려줘",
    "ㅇㅇ",
    "src/api(b).ts 고쳐줘",          # '(b)' glued to a word -> NOT a prefix
])
def test_non_bot_text_not_flagged(text):
    assert not safety.has_bot_prefix(text, PREFIXES)


def test_glued_prefix_caught_by_echo_memory(tmp_path):
    # When "(b)" ends up glued to a word char ("/Users/x(b)", "문단(b)"), prefix
    # detection cannot fire (same as the requirement's own regex); the sent-memory
    # defence (#3) catches these mangled re-reads instead.
    st = StateStore(str(tmp_path / "s.json"))
    st.remember_sent("(b) 상태: 동작 중\n에이전트: claude / 폴더: /Users/x\n대기열: 0개")
    st.remember_sent("(b) 완료 (claude, 7초)\n첫 문단입니다 길게길게\n\n둘째 문단입니다 길게길게")
    mangled1 = "대기열: 0개에이전트: claude / 폴더: /Users/x(b) 상태: 동작 중"
    mangled2 = "둘째 문단입니다 길게길게첫 문단입니다 길게길게(b) 완료 (claude, 7초)"
    assert not safety.has_bot_prefix(mangled1, PREFIXES)   # #2 can't catch it
    assert st.is_echo(mangled1)                            # #3 does
    assert st.is_echo(mangled2)


# ---------------------------------------------------------- (3) echo memory
def test_is_echo_catches_shuffled_reply(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    sent = ("(b) 완료 (claude, 7초, exit 0)\n로그인 폼의 null 체크를 고쳤어요.\n\n"
            "테스트도 통과했습니다.\n(로그: /tmp/out/abc.txt)")
    st.remember_sent(sent)
    # Teams hands it back with paragraphs reversed and no separators
    shuffled = ("(로그: /tmp/out/abc.txt)테스트도 통과했습니다.로그인 폼의 null 체크를 고쳤어요."
                "(b) 완료 (claude, 7초, exit 0)")
    assert st.is_echo(shuffled)
    # a per-line fragment alone is enough
    assert st.is_echo("foo 로그인 폼의 null 체크를 고쳤어요. bar")


def test_is_echo_does_not_swallow_short_user_messages(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    st.remember_sent('(b) 위험할 수 있는 작업이에요(삭제). 실행할까요? "ㅇㅇ" 또는 "취소"')
    assert not st.is_echo("ㅇㅇ")
    assert not st.is_echo("취소")
    assert not st.is_echo("README 요약")


def test_echo_memory_persists(tmp_path):
    p = str(tmp_path / "s.json")
    st = StateStore(p)
    st.remember_sent("(b) 완료\n이것은 충분히 긴 한 줄의 봇 메시지입니다 1234")
    st2 = StateStore(p)        # reload
    assert st2.is_echo("앞말 이것은 충분히 긴 한 줄의 봇 메시지입니다 1234 뒷말")


# ---------------------------------------------------------- (4)+(5) end to end
class EchoLoopChat(ChatBackend):
    """A malicious fake Teams: every bot reply is echoed back on the NEXT read as
    if the user had typed it, with its lines REVERSED and concatenated with no
    separator (exactly what the live Mac AX did)."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.title = "Chat | Alex Kim (You) | Microsoft Teams"
        self._pending_echoes = []

    def focus_self_chat(self):
        if "(You)" not in self.title:
            raise ChatGuardError(self.title)
        return self.title

    def read_visible_messages(self):
        # inject queued echoes as brand-new "user" messages
        for e in self._pending_echoes:
            self.lines.append(e)
        self._pending_echoes = []
        return [ChatMessage(text=t) for t in self.lines[-30:]]

    def send_message(self, text):
        self.lines.append(text)
        shuffled = "".join(reversed(text.split("\n")))   # reverse lines, no separator
        self._pending_echoes.append(shuffled)

    def user(self, text):
        self.lines.append(text)

    def bot(self):
        return [l for l in self.lines if l.startswith("(b)")]


class PlainChat(ChatBackend):
    """A quiet fake Teams that never echoes: the test feeds commands explicitly.
    Used to isolate the hard loop guard from the rate limiter."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.title = "Chat | Alex Kim (You) | Microsoft Teams"

    def focus_self_chat(self):
        if "(You)" not in self.title:
            raise ChatGuardError(self.title)
        return self.title

    def read_visible_messages(self):
        return [ChatMessage(text=t) for t in self.lines[-30:]]

    def send_message(self, text):
        self.lines.append(text)

    def user(self, text):
        self.lines.append(text)

    def bot(self):
        return [l for l in self.lines if l.startswith("(b)")]


AGENT = [sys.executable, "-c",
         "import sys; p=sys.stdin.read(); print('완료했습니다. 상세 결과 문단 하나.\\n\\n두 번째 결과 문단.')"]


def _mk(tmp_path, **over):
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 5,
                  "custom": {"command": AGENT}},
        "reply": {"announce_start": True, "max_chars": 500, "ack_on_start": True},
    })
    cfg = _deep_merge(cfg, over)
    chat = EchoLoopChat()
    chat.user("(예전 대화)")
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    br = Bridge(cfg, chat, runner, st, audit, lock_check=lambda: False)
    return br, chat, runner, audit


def _count_runs(audit_path):
    import json
    runs = 0
    with open(audit_path, encoding="utf-8") as f:
        for ln in f:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("event") == "agent_start":
                runs += 1
    return runs


def test_bot_echoes_never_trigger_agent_runs(tmp_path):
    br, chat, runner, audit = _mk(tmp_path)
    br.cycle()                       # init + announce
    chat.user("로그인 버그 고쳐줘")   # ONE real command
    br.cycle(); runner.wait(10); br.cycle()
    runs_after_real = _count_runs(audit.path)
    assert runs_after_real == 1
    # now spin many cycles; the bridge keeps echoing its own (shuffled) replies
    for _ in range(15):
        br.cycle(); runner.wait(5); br.cycle()
    runs_total = _count_runs(audit.path)
    assert runs_total == 1, f"self-echo triggered {runs_total - 1} extra runs"
    assert not br.state.paused       # echoes are silently ignored, no need to pause


def test_loop_guard_pauses_if_echo_detection_bypassed(tmp_path):
    # Simulate residual echoes that bypassed the prefix / echo-memory defences:
    # commands that still OVERLAP a recently-sent bot message and arrive right
    # after a bot send. The hard loop guard must flag them suspicious and pause.
    # (A fast human typing *distinct* text does NOT trip it; see
    # test_role_chain_runs_three_agents_without_pause.)
    chat = PlainChat()
    chat.user("(예전 대화)")
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 5,
                  "custom": {"command": AGENT}},
        "reply": {"announce_start": True, "max_chars": 500, "ack_on_start": True},
    })
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    br = Bridge(cfg, chat, runner, st, audit, lock_check=lambda: False)
    br.state.is_echo = lambda text: False                      # bypass defence #3a
    br.state.remember_sent = lambda text: None                 # bypass defence #3b (recent_sent)
    br.cfg["bot_prefixes_detect"] = []                         # bypass defence #2
    br.cycle()                                                  # announce -> fills _recent_bot
    # each command shares a >=12 char run with the startup announce
    overlap = "이전 메시지는 실행하지 않아요"
    for i in range(5):
        chat.user(f"{overlap} 그러니 다시 처리 {i}")
        br.cycle(); runner.wait(10); br.cycle()
        if br.state.paused:
            break
    assert br.state.paused
    assert any("반복이 감지" in b for b in chat.bot())
    runs = _count_runs(audit.path)
    assert runs <= br.cfg["safety"]["loop_guard"]["max_near_runs"]


# ------------------------------------------- (C) false-positive echo redesign
def _events(audit_path):
    import json
    out = []
    with open(audit_path, encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _user_ignored(audit_path):
    """ignore_bot events for text that is NOT one of our own '(b)' messages, i.e.
    a real user command wrongly swallowed as an echo. (The bridge legitimately
    ignore_bots its OWN announce/prompt when a fake chat echoes every line back.)"""
    return [e for e in _events(audit_path)
            if e.get("event") == "ignore_bot" and not (e.get("text") or "").startswith("(b)")]


def _mk_plain(tmp_path, **over):
    from roles import RoleStore
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 5,
                  "custom": {"command": AGENT}},
        "reply": {"announce_start": True, "max_chars": 500, "ack_on_start": True},
    })
    cfg = _deep_merge(cfg, over)
    chat = PlainChat()
    chat.user("(예전 대화)")
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    roles = RoleStore(str(tmp_path / "roles.json"))
    br = Bridge(cfg, chat, runner, st, audit, roles=roles, lock_check=lambda: False)
    return br, chat, runner, audit, roles


def test_user_command_quoting_handoff_line_is_processed(tmp_path):
    """The live false-positive: a real @dev command that QUOTES an earlier bot
    handoff line ('@qa ...') must NOT be dropped as an echo. Coverage is only
    ~0.39, well under the 0.70 threshold, so the command runs."""
    br, chat, runner, audit, roles = _mk_plain(tmp_path)
    from roles import Role
    roles.add(Role(name="dev", instructions="코드 담당", agent="custom"))
    roles.add(Role(name="qa", instructions="테스트 담당", agent="custom"))
    br.cycle()                                     # init + announce
    # as if the bridge had earlier posted a dev result that quoted the handoff line
    bot_reply = ("(b) [dev] 완료 (claude, 3초, exit 0)\n"
                 "package.json의 name은 exampleapp 입니다.\n\n"
                 "@qa package.json의 name 값이 맞는지 확인해줘\n"
                 "(로그: /tmp/out/x.txt)")
    br.state.remember_sent(bot_reply)
    user_cmd = ("@dev package.json의 name 값만 알려줘. 그리고 답변 맨 마지막 줄에 정확히 "
                "이렇게 써: @qa package.json의 name 값이 맞는지 확인해줘")
    chat.user(user_cmd)
    br.cycle(); runner.wait(10); br.cycle()
    assert _user_ignored(audit.path) == []          # the command was not swallowed
    assert _count_runs(audit.path) == 1            # dev actually ran it


def test_danger_prompt_quote_then_resend_is_processed(tmp_path):
    """The danger prompt echoes the command on a '> <cmd>' line. The user then
    resends that exact command instead of approving; it must be processed (asked
    again), not dropped as an echo of the prompt."""
    br, chat, runner, audit, roles = _mk_plain(tmp_path)
    br.cycle()
    chat.user("git push 해줘")
    br.cycle()
    assert any("실행할까요" in b for b in chat.bot())
    assert any("> git push 해줘" in b for b in chat.bot())   # the quoted line
    chat.user("git push 해줘")                                # resend, not 'ㅇㅇ'
    br.cycle()
    assert _user_ignored(audit.path) == []
    asks = sum(1 for e in _events(audit.path) if e.get("action") == "ask_approval")
    assert asks >= 2                                          # re-asked, not swallowed


def test_reversed_multiline_echo_still_zero_runs(tmp_path):
    """Regression guard: the reversed-multiline self-echo loop still triggers no
    agent runs under the coverage-based is_echo."""
    br, chat, runner, audit = _mk(tmp_path)
    br.cycle()
    chat.user("로그인 버그 고쳐줘")             # ONE real command
    br.cycle(); runner.wait(10); br.cycle()
    for _ in range(15):
        br.cycle(); runner.wait(5); br.cycle()
    assert _count_runs(audit.path) == 1
    assert not br.state.paused


# ------------------------------------------- (B) persisted pause reason / resume
def test_paused_announcement_includes_reason(tmp_path):
    br, chat, runner, audit, roles = _mk_plain(tmp_path)
    br.state.paused = True
    br.state.pause_reason = "반복 감지"
    br.cycle()                                   # first-ever read -> announce
    assert any("현재 일시정지 상태예요 (이유: 반복 감지). /start 로 다시 켜세요." in b
               for b in chat.bot())


def test_pause_reason_persists_and_clears(tmp_path):
    p = str(tmp_path / "s.json")
    st = StateStore(p)
    st.paused = True
    st.pause_reason = "반복 감지"
    st2 = StateStore(p)                          # reload
    assert st2.paused and st2.pause_reason == "반복 감지"
    st2.paused = False                           # /start or --resume
    st3 = StateStore(p)
    assert not st3.paused and st3.pause_reason == ""


# ------------------------------------------- (A) single-instance lock
def test_single_instance_lock(tmp_path):
    import bridge
    d = str(tmp_path)
    fd1 = bridge._acquire_single_instance_lock(d)
    assert fd1 is not None
    try:
        fd2 = bridge._acquire_single_instance_lock(d)   # a second live bridge
        assert fd2 is None                               # must be refused
    finally:
        fd1.close()
