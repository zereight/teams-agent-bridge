"""Role chain, guardrails and progress reporting with a fake chat + custom agent."""
import sys
import time

import pytest

from agent_runner import AgentResult, AgentRunner, AgentTask
from audit import Audit
from backend_base import ChatBackend, ChatGuardError, ChatMessage
from bridge import Bridge
from config import DEFAULTS, _deep_merge
from roles import Role, RoleStore
from state import StateStore


class FakeChat(ChatBackend):
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


# Custom agent: echoes a one-line summary; if its input contains HANDOFF_TO_QA,
# it appends a real handoff line "@qa ...". One line only, so the embedded
# summary never looks like a handoff.
AGENT = [sys.executable, "-c",
         "import sys;"
         "p=sys.stdin.read();"
         "s='처리함: '+p.replace(chr(10),' ')[:30];"
         "sys.stdout.write(s);"
         "sys.stdout.write(chr(10)+'@qa 테스트 부탁해') if 'HANDOFF_TO_QA' in p else None"]


@pytest.fixture
def env(tmp_path):
    cfg = _deep_merge(DEFAULTS, {
        "agent": {"type": "custom", "workdir": str(tmp_path), "timeout_sec": 5,
                  "default_prompt": "", "worktrees": {"enabled": False},
                  "custom": {"command": AGENT}},
        "reply": {"announce_start": True, "max_chars": 500, "ack_on_start": False},
    })
    chat = FakeChat()
    chat.user("(예전 대화)")
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    roles = RoleStore(str(tmp_path / "roles.json"))
    br = Bridge(cfg, chat, runner, st, audit, roles=roles, lock_check=lambda: False)
    yield br, chat, runner, roles
    runner.stop()
    audit.close()


def _idle(br, runner, n=1):
    for _ in range(n):
        br.cycle()
    runner.wait(10)
    br.cycle()


def test_role_commands(env):
    br, chat, runner, roles = env
    br.cycle()
    chat.user("/role add dev 코드 수정 담당")
    br.cycle()
    assert "'dev'" in chat.bot()[-1] and roles.exists("dev")
    chat.user("/role list")
    br.cycle()
    assert "dev" in chat.bot()[-1]
    chat.user("/role use dev")
    br.cycle()
    assert roles.default_role == "dev"


def test_role_rm_needs_confirmation(env):
    br, chat, runner, roles = env
    br.cycle()
    roles.add(Role(name="tmp", instructions="x", agent="custom"))
    chat.user("/role rm tmp")
    br.cycle()
    assert "삭제할까요" in chat.bot()[-1] and roles.exists("tmp")
    chat.user("ㅇㅇ")
    br.cycle()
    assert not roles.exists("tmp")


def test_unknown_role_task(env):
    br, chat, runner, roles = env
    br.cycle()
    chat.user("@ghost 안녕")
    br.cycle()
    assert "몰라" in chat.bot()[-1] and not runner.busy


def test_role_chain_dev_to_qa(env):
    br, chat, runner, roles = env
    br.cycle()
    roles.add(Role(name="dev", instructions="코드 수정 담당 HANDOFF_TO_QA", agent="custom"))
    roles.add(Role(name="qa", instructions="테스트만 담당", agent="custom"))
    chat.user("@dev 로그인 버그 고쳐줘")
    _idle(br, runner, n=1)        # dev runs
    _idle(br, runner, n=1)        # qa runs after handoff
    br.cycle()
    bots = "\n".join(chat.bot())
    assert any(b.startswith("(b) ✅ dev") for b in chat.bot())
    assert "@qa 에게 이어서 작업을 넘겼어요" in bots
    assert any(b.startswith("(b) ✅ qa") for b in chat.bot())
    # qa's prompt carried dev's summary forward
    assert any(b.startswith("(b) ✅ qa") and "처리함" in b for b in chat.bot())


def test_self_handoff_blocked(env):
    br, chat, runner, roles = env
    res = AgentResult(task_id="t1", prompt="p", ok=True, exit_code=0,
                      summary="끝.\n@dev 다시 해", role="dev",
                      meta={"chain_id": "c1", "hops": 0, "deadline": time.time() + 999})
    roles.add(Role(name="dev", instructions="x", agent="custom"))
    br.process_handoffs(res)
    assert any("자기 자신" in b for b in chat.bot()) and not runner.busy


def test_max_hops_blocked(env):
    br, chat, runner, roles = env
    br.rcfg = dict(br.rcfg, max_hops=1)
    roles.add(Role(name="qa", instructions="x", agent="custom"))
    res = AgentResult(task_id="t1", prompt="p", ok=True, exit_code=0,
                      summary="끝.\n@qa 테스트", role="dev",
                      meta={"chain_id": "c1", "hops": 1, "deadline": time.time() + 999})
    br.process_handoffs(res)
    assert any("한도" in b for b in chat.bot()) and not runner.busy


def test_progress_report(env):
    """patch8: progress is phase-change. maybe_progress() posts ONE silence ping
    after silence_ping_sec with no phase change, not a fixed-interval stream."""
    br, chat, runner, roles = env
    br.rcfg = dict(br.rcfg, silence_ping_sec=10)
    now = [1000.0]
    br.clock = lambda: now[0]

    class FakeRunner:
        current = AgentTask(task_id="tX", prompt="long", started_at=1000.0, role="dev")
        step = "Edit"
    br.runner = FakeRunner()

    now[0] = 1005.0
    br.maybe_progress()
    assert not any("진행 중" in b for b in chat.bot())   # under threshold
    now[0] = 1015.0
    br.maybe_progress()
    assert any("[dev] 진행 중" in b and "Edit" in b for b in chat.bot())
    n = len(chat.bot())
    now[0] = 1100.0
    br.maybe_progress()
    assert len(chat.bot()) == n                          # only one silence ping


def _count_agent_starts(audit_path):
    import json
    n = 0
    with open(audit_path, encoding="utf-8") as f:
        for ln in f:
            try:
                if json.loads(ln).get("event") == "agent_start":
                    n += 1
            except Exception:
                pass
    return n


def test_role_chain_runs_three_agents_without_pause(env):
    """patch5: /status -> README task -> @dev (hands off to qa). Every command is
    typed right after a bot reply (normal fast human) and the handoff is enqueued
    automatically. None of this must trip the loop guard: 3 agent runs, no pause.

    All cycles run in well under near_send_sec, so condition (a) of the suspicious
    rule is satisfied for every run; the test proves condition (b) (overlap with a
    recent bot message) is what keeps a fast human safe, and that handoff tasks are
    exempt from the suspicious rule entirely."""
    br, chat, runner, roles = env
    audit_path = br.audit.path
    br.cycle()                                           # init + announce
    roles.add(Role(name="dev", instructions="코드 수정 담당 HANDOFF_TO_QA", agent="custom"))
    roles.add(Role(name="qa", instructions="테스트만 담당", agent="custom"))

    chat.user("/status")                                 # builtin, no agent run
    br.cycle()
    chat.user("README 파일 내용을 요약해줘")               # agent run #1 (default)
    _idle(br, runner, n=1)
    chat.user("@dev 로그인 버그를 고쳐줘")                 # agent run #2 (dev) -> hands off
    _idle(br, runner, n=1)
    _idle(br, runner, n=1)                               # agent run #3 (qa, handoff)
    br.cycle()

    assert not br.state.paused
    assert not any("반복이 감지" in b for b in chat.bot())
    assert _count_agent_starts(audit_path) == 3
    bots = "\n".join(chat.bot())
    assert any(b.startswith("(b) ✅ qa") for b in chat.bot())  # the chain actually finished
