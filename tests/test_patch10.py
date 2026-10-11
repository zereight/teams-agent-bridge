"""Patch10: pi backend (argv, JSON parse, guard parity, unguarded refusal)."""
import json
import os
import subprocess
import sys

import pytest

import agent_runner as A
import hooks.guard as guard
from agent_runner import AgentRunner, RunSpec
from config import DEFAULTS, _deep_merge
from tests.fixtures.pi_json import PI_JSON_OK, PI_PRINT_OK

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")


def _runner(tmp_path, **pi_over):
    cfg = _deep_merge(DEFAULTS["agent"], {
        "type": "pi", "workdir": str(tmp_path), "timeout_sec": 5,
        "pi": pi_over,
    })
    return AgentRunner(cfg, str(tmp_path), str(tmp_path / "out"),
                       audit=None)


# ------------------------------------------------------------- argv building
def test_pi_argv_json_mode_plain_task(tmp_path):
    r = _runner(tmp_path)
    argv, stdin = r.build_command("요약해줘", spec=RunSpec(prompt="요약해줘"))
    assert argv[0].endswith("pi")
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    assert "--append-system-prompt" in argv            # base system prompt appended
    assert "--no-session" in argv                      # plain task -> fresh session
    assert "-e" in argv and argv[argv.index("-e") + 1].endswith("pi-guard.ts")
    assert "-ne" not in argv                           # user extensions kept
    assert stdin == "요약해줘"                           # prompt via stdin
    # provider/model NOT overridden by default (inherit user's pi settings)
    assert "--provider" not in argv and "--model" not in argv


def test_pi_argv_role_uses_session_id_and_role_prompt(tmp_path):
    r = _runner(tmp_path)
    spec = RunSpec(prompt="고쳐줘", agent_type="pi", role="dev",
                   append_system_prompt="dev 지침", resume_session_id="bridge-dev",
                   track_session=True)
    argv, _ = r.build_command("고쳐줘", spec=spec)
    assert "--session-id" in argv and argv[argv.index("--session-id") + 1] == "bridge-dev"
    assert "--no-session" not in argv
    # both the base prompt and the role instructions are appended
    assert argv.count("--append-system-prompt") == 2
    assert "dev 지침" in argv


def test_pi_argv_print_mode_and_dry_readonly(tmp_path):
    r = _runner(tmp_path, mode="print")
    argv, _ = r.build_command("볼래", spec=RunSpec(prompt="볼래", dry=True))
    assert "-p" in argv and "--mode" not in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == "read,grep,find,ls"


def test_pi_argv_model_and_excludes_when_configured(tmp_path):
    r = _runner(tmp_path, provider="anthropic", model="sonnet:high",
                exclude_tools=["web_fetch"], skills=["/skills/pstack"])
    argv, _ = r.build_command("x", spec=RunSpec(prompt="x"))
    assert argv[argv.index("--provider") + 1] == "anthropic"
    assert argv[argv.index("--model") + 1] == "sonnet:high"
    assert argv[argv.index("--exclude-tools") + 1] == "web_fetch"
    assert argv[argv.index("--skill") + 1] == "/skills/pstack"


# ------------------------------------------------------------- JSON parsing
def test_parse_pi_json_takes_final_assistant_text_cost_session():
    out = A.parse_pi_output(PI_JSON_OK)
    assert out["session_id"] == "bridge-dev"
    assert "example-app" in out["text"]
    assert out["cost_usd"] == 0.012          # highest cumulative cost
    assert out["is_error"] is False


def test_parse_pi_print_mode_plain_text():
    out = A.parse_pi_output(PI_PRINT_OK)      # no JSON events
    assert out["text"] == PI_PRINT_OK
    assert out["session_id"] is None


# ------------------------------------------------------------- guard parity (pi)
def test_guard_decide_pi_lowercase_bash_and_env():
    assert guard.decide({"tool_name": "bash",
                         "tool_input": {"command": "git push origin main"}}, "/wt")[0] == "deny"
    assert guard.decide({"tool_name": "read",
                         "tool_input": {"path": "configurations/env/.env.production"}}, "/wt")[0] == "deny"
    assert guard.decide({"tool_name": "read",
                         "tool_input": {"path": "src/app.ts"}}, "/wt")[0] == "allow"


def test_guard_decide_pi_block_tool_patterns():
    d, _ = guard.decide({"tool_name": "web_fetch", "tool_input": {"url": "http://x"}},
                        "/wt", [], ["(?i)web", "(?i)fetch"])
    assert d == "deny"


def test_guard_subprocess_denies_pi_event_and_logs(tmp_path):
    """The pi-guard.ts extension shells out to this exact script; prove it denies
    a pi-shaped (lowercase) bash push with exit code 2 and logs to tools.jsonl."""
    log = str(tmp_path / "tools.jsonl")
    env = dict(os.environ, BRIDGE_TOOLS_LOG=log, BRIDGE_TASK_ID="t1", BRIDGE_ROLE="dev")
    ev = json.dumps({"tool_name": "bash", "tool_input": {"command": "git push"}})
    p = subprocess.run([sys.executable, os.path.join(HOOKS, "guard.py")],
                       input=ev, env=env, capture_output=True, text=True)
    assert p.returncode == 2
    rec = json.loads(open(log).read().splitlines()[-1])
    assert rec["decision"] == "deny" and rec["tool"] == "bash" and rec["role"] == "dev"


def test_pi_guard_extension_file_is_shipped():
    assert os.path.exists(os.path.join(HOOKS, "pi-guard.ts"))


# ------------------------------------------------------------- unguarded refusal
def test_pi_refusal_when_guard_off_and_write_capable(tmp_path):
    r = _runner(tmp_path, allow_unguarded=False)
    r.cfg["hook"] = {"enabled": False}         # guard extension disabled
    assert r._pi_guard_ext() is None
    assert r._pi_write_capable(RunSpec(prompt="x")) is True
    assert r._pi_refusal_reason(RunSpec(prompt="x"))      # non-empty -> refuse


def test_pi_no_refusal_when_readonly_or_allowed(tmp_path):
    r = _runner(tmp_path, tools=["read", "grep"])
    r.cfg["hook"] = {"enabled": False}
    assert r._pi_write_capable(RunSpec(prompt="x")) is False
    assert r._pi_refusal_reason(RunSpec(prompt="x")) == ""
    r2 = _runner(tmp_path, allow_unguarded=True)
    r2.cfg["hook"] = {"enabled": False}
    assert r2._pi_refusal_reason(RunSpec(prompt="x")) == ""


def test_pi_run_refuses_and_reports(tmp_path):
    r = _runner(tmp_path, allow_unguarded=False)
    r.cfg["hook"] = {"enabled": False}
    r.start(RunSpec(prompt="쓰기 작업", agent_type="pi"))
    r.wait(5)
    res = r.results.get(timeout=5)
    assert res.is_error and not res.ok
    assert "거부" in res.summary


# dry run stays read-only -> no refusal even with guard off
def test_pi_dry_not_refused_even_without_guard(tmp_path):
    r = _runner(tmp_path, allow_unguarded=False)
    r.cfg["hook"] = {"enabled": False}
    assert r._pi_refusal_reason(RunSpec(prompt="x", dry=True)) == ""
