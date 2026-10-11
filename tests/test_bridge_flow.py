"""End-to-end flow with a fake chat backend and a fake agent (no Teams, no CLI)."""
import sys
import time

import pytest

from agent_runner import AgentRunner
from audit import Audit
from backend_base import ChatBackend, ChatGuardError, ChatMessage
from bridge import Bridge
from config import DEFAULTS, _deep_merge
from state import StateStore


class FakeChat(ChatBackend):
    def __init__(self):
        super().__init__()
        self.lines = []          # visible chat, oldest first
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
         "import sys,time; p=sys.stdin.read(); "
         "time.sleep(5) if 'slow' in p else None; print('did: ' + p)"]


@pytest.fixture
def env(tmp_path):
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 3,
                  "default_prompt": "", "worktrees": {"enabled": False},
                  "custom": {"command": AGENT}},
        "reply": {"announce_start": True, "max_chars": 200},
    })
    chat = FakeChat()
    chat.user("(예전 대화) 어제 보낸 메시지")   # chat is never empty in practice
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    locked = [False]
    br = Bridge(cfg, chat, runner, st, audit, lock_check=lambda: locked[0])
    yield br, chat, runner, locked
    runner.stop()
    audit.close()


def run_until_idle(br, runner, cycles=1):
    for _ in range(cycles):
        br.cycle()
    runner.wait(10)
    br.cycle()


def test_empty_chat_first_read_does_not_initialise(tmp_path):
    from state import StateStore
    st = StateStore(str(tmp_path / "s.json"))
    assert st.filter_new([]) == [] and not st.initialized


def test_history_not_executed_and_basic_run(env):
    br, chat, runner, _ = env
    chat.user("이건 예전 명령이라 실행되면 안 됨")
    br.cycle()
    assert chat.bot() and "브리지가 켜졌어요" in chat.bot()[-1]
    assert not runner.busy
    chat.user("README 요약해줘")
    run_until_idle(br, runner)
    bots = chat.bot()
    assert any("시작했어요" in b for b in bots)
    assert "did: README 요약해줘" in bots[-1] and bots[-1].startswith("(b) ✅")
    assert "예전 명령" not in "".join(b for b in bots if "did:" in b)


def test_bot_output_never_reexecuted(env):
    br, chat, runner, _ = env
    br.cycle()                                   # init (announce)
    chat.user("hello")
    run_until_idle(br, runner)
    n_bot = len(chat.bot())
    for _ in range(3):                           # bot replies are now visible; must not loop
        br.cycle()
    assert len(chat.bot()) == n_bot
    assert not runner.busy


def test_dangerous_gate_approve(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("빌드 폴더 삭제해줘")
    br.cycle()
    assert "실행할까요?" in chat.bot()[-1] and not runner.busy
    chat.user("ㅇㅇ")
    run_until_idle(br, runner)
    assert "did: 빌드 폴더 삭제해줘" in chat.bot()[-1]


def test_dangerous_gate_cancel_and_other(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("git push 해줘")
    br.cycle()
    chat.user("취소")
    br.cycle()
    assert "취소했어요" in chat.bot()[-1] and not runner.busy
    chat.user("deploy now")
    br.cycle()
    chat.user("README 요약")          # not an approval -> cancel deploy, run this instead
    run_until_idle(br, runner)
    texts = "\n".join(chat.bot())
    assert "did: README 요약" in texts and "did: deploy now" not in texts


def test_stray_approval_does_nothing(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("ㅇㅇ")
    br.cycle()
    assert "승인하거나 취소할 작업이 없어요" in chat.bot()[-1] and not runner.busy


def test_stop_and_start(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("slow task")
    br.cycle()
    assert runner.busy
    chat.user("/stop")
    br.cycle()
    runner.wait(10)
    br.cycle()
    texts = "\n".join(chat.bot())
    assert "멈췄어요" in texts and "대기열" in texts and "명령을 실행하지 않아요" in texts
    assert br.state.paused
    chat.user("README 요약")
    br.cycle()
    assert "일시정지 중" in chat.bot()[-1] and not runner.busy
    chat.user("/status")
    br.cycle()
    assert "상태: 일시정지" in chat.bot()[-1]
    chat.user("/start")
    br.cycle()
    assert not br.state.paused
    chat.user("README 요약 다시")
    run_until_idle(br, runner)
    assert "did: README 요약 다시" in chat.bot()[-1]


def test_timeout(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("slow job")                 # sleeps 5s, timeout is 3s
    run_until_idle(br, runner)
    assert "시간 초과" in chat.bot()[-1] and chat.bot()[-1].startswith("(b) ❌")


def test_queue(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("first")
    chat.user("second")
    br.cycle()
    assert any("대기열" in b for b in chat.bot())
    runner.wait(10); br.cycle()           # first done, second starts
    runner.wait(10); br.cycle()
    texts = "\n".join(chat.bot())
    assert "did: first" in texts and "did: second" in texts


def test_guard_aborts_cycle(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.title = "Chat | 팀장님 | Microsoft Teams"
    chat.user("README 요약")
    n = len(chat.lines)
    assert br.cycle() == "guard"
    assert len(chat.lines) == n and not runner.busy


def test_locked_skips(env):
    br, chat, runner, locked = env
    br.cycle()
    locked[0] = True
    chat.user("hello")
    assert br.cycle() == "locked" and not runner.busy
    locked[0] = False
    run_until_idle(br, runner)
    assert "did: hello" in chat.bot()[-1]


def test_circuit_breaker(env):
    br, chat, runner, _ = env
    br.cycle()
    for i in range(4):
        chat.user("same thing")
        br.cycle()
        runner.wait(10)
        br.cycle()
    assert br.state.paused
    # a runaway is caught by EITHER the repeat rate-limiter ("안전장치") or the
    # new self-echo loop guard ("반복이 감지") - both pause the bridge.
    assert any(("안전장치" in b) or ("반복이 감지" in b) for b in chat.bot())


def test_truncation_mentions_file(env):
    br, chat, runner, _ = env
    br.cycle()
    chat.user("x" * 400)
    run_until_idle(br, runner)
    last = chat.bot()[-1]
    assert "(로그:" in last and ".txt" in last and len(last) < 600  # phone report truncates silently


def test_dry_run_never_runs(env, tmp_path):
    br, chat, runner, _ = env
    br.dry_run = True
    n = len(chat.lines)
    br.cycle()
    chat.user("README 요약")
    br.cycle()
    assert not runner.busy
    assert len(chat.lines) == n + 1          # only the user's line; dry run never types
