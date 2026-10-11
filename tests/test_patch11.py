"""Patch 11: per-message agent prefixes, /agent, /role set, preset [agent],
codex PreToolUse hook, fabric_exec heuristics, configurable guard ruleset,
and open-source readiness (no personal data, generic prompt, atomic selectors)."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "hooks", "guard.py")
sys.path.insert(0, ROOT)

from agent_runner import AgentRunner, RunSpec  # noqa: E402
import config  # noqa: E402
from config import DEFAULTS, _deep_merge  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "hooks"))
import guard  # noqa: E402


# ------------------------------------------------------------------ helpers
def _bridge(tmp_path, agent_over=None):
    from audit import Audit
    from backend_base import ChatBackend, ChatMessage  # noqa: F401
    from bridge import Bridge
    from roles import Role, RoleStore
    from state import StateStore

    class FakeChat(ChatBackend):
        def __init__(self):
            super().__init__()
            self.lines = []
            self.title = "Chat | Alex Kim (You) | Microsoft Teams"

        def focus_self_chat(self):
            return self.title

        def read_visible_messages(self):
            return []

        def send_message(self, text):
            self.lines.append(text)

    acfg = {"type": "claude", "workdir": str(tmp_path), "default_prompt": "",
            "worktrees": {"enabled": False}}
    acfg.update(agent_over or {})
    cfg = _deep_merge(DEFAULTS, {"agent": acfg,
                                 "reply": {"announce_start": False, "ack_on_start": False}})
    chat = FakeChat()
    audit = Audit(str(tmp_path / "logs"))
    st = StateStore(str(tmp_path / "state.json"))
    runner = AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"), audit)
    roles = RoleStore(str(tmp_path / "roles.json"))
    br = Bridge(cfg, chat, runner, st, audit, roles=roles, lock_check=lambda: False)
    br._agent_available = lambda a: True            # all agents "installed"
    br._agent_version = lambda a: f"{a} 1.0"
    specs = []
    br._start = lambda task, approved=False: specs.append(br._build_spec(task))
    for n in ("dev", "qa"):
        roles.add(Role(name=n, instructions="x", agent="claude"))
    return br, chat, specs, roles, st


def _msg(text):
    from backend_base import ChatMessage
    return ChatMessage(text=text)


# ------------------------------------------------- 1. per-message agent prefix
@pytest.mark.parametrize("ag", ["claude", "pi", "codex"])
def test_prefix_runs_once_with_that_agent(tmp_path, ag):
    br, chat, specs, _, _ = _bridge(tmp_path)
    br.handle(_msg(f"/{ag} README 첫 줄 알려줘"))
    assert len(specs) == 1
    assert specs[0].agent_type == ag and specs[0].meta["agent"] == ag
    assert specs[0].prompt == "README 첫 줄 알려줘"
    # default is unchanged: next plain message uses claude
    br.handle(_msg("그냥 메시지"))
    assert specs[1].agent_type == "claude"


def test_prefix_danger_still_gated_then_runs_with_agent(tmp_path):
    br, chat, specs, _, _ = _bridge(tmp_path)
    br.handle(_msg("/codex git push 해줘"))
    assert specs == [] and br.pending is not None
    assert any("실행할까요" in l for l in chat.lines)
    br.handle(_msg("ㅇㅇ"))
    assert len(specs) == 1 and specs[0].agent_type == "codex"


def test_prefix_without_task_shows_usage(tmp_path):
    br, chat, specs, _, _ = _bridge(tmp_path)
    br.handle(_msg("/pi"))
    assert specs == [] and any("사용법: /pi" in l for l in chat.lines)


def test_prefix_unavailable_agent_refused(tmp_path):
    br, chat, specs, _, _ = _bridge(tmp_path)
    br._agent_available = lambda a: a != "codex"
    br.handle(_msg("/codex 뭔가 해줘"))
    assert specs == [] and any("쓸 수 없어요" in l for l in chat.lines)


# ------------------------------------------------------------- 2. /agent
def test_agent_status_lists_versions(tmp_path):
    br, chat, *_ = _bridge(tmp_path)
    br.handle(_msg("/agent"))
    out = chat.lines[-1]
    assert "기본 에이전트: claude" in out
    for a in ("claude", "pi", "codex"):
        assert f"{a}: {a} 1.0" in out


def test_agent_switch_persists_in_state_not_config(tmp_path):
    from state import StateStore
    br, chat, specs, _, st = _bridge(tmp_path)
    br.handle(_msg("/agent pi"))
    assert br.agent_name == "pi"
    br.handle(_msg("할 일"))
    assert specs[-1].agent_type == "pi"
    assert StateStore(str(tmp_path / "state.json")).default_agent == "pi"
    assert br.cfg["agent"]["type"] == "claude"          # config untouched
    # a new bridge on the same state picks it up
    br2, _, specs2, _, _ = _bridge(tmp_path)
    assert br2.agent_name == "pi"


def test_agent_bad_name(tmp_path):
    br, chat, *_ = _bridge(tmp_path)
    br.handle(_msg("/agent gpt"))
    assert "사용법: /agent" in chat.lines[-1] and br.agent_name == "claude"


# ----------------------------------------------- 3. /role set + preset agent
def test_role_set_changes_agent_and_resets_session(tmp_path):
    br, chat, specs, roles, _ = _bridge(tmp_path)
    r = roles.get("dev"); r.session_id = "abc"; roles.add(r)
    br.handle(_msg("/role set dev agent=codex"))
    assert roles.get("dev").agent == "codex"
    assert roles.get("dev").session_id in (None, "")
    assert "claude → codex" in chat.lines[-1]


def test_role_set_unknown_and_bad_syntax(tmp_path):
    br, chat, *_ = _bridge(tmp_path)
    br.handle(_msg("/role set nobody agent=pi"))
    assert "몰라요" in chat.lines[-1]
    br.handle(_msg("/role set dev pi"))
    assert "사용법: /role set" in chat.lines[-1]


def test_role_preset_with_agent_sets_all_roles(tmp_path):
    br, chat, specs, roles, _ = _bridge(tmp_path)
    br.handle(_msg("/role preset pstack pi"))
    for n in ("dev", "qa", "reviewer"):
        assert roles.get(n).agent == "pi"


def test_role_preset_bad_agent(tmp_path):
    br, chat, specs, roles, _ = _bridge(tmp_path)
    br.handle(_msg("/role preset pstack gpt"))
    assert "claude|pi|codex" in chat.lines[-1]


# ------------------------------------------------------ 4. codex PreToolUse
def _codex_runner(tmp_path, **over):
    cfg = _deep_merge(DEFAULTS["agent"], {"type": "codex", "workdir": str(tmp_path), **over})
    return AgentRunner(cfg, str(tmp_path), str(tmp_path / "out"), audit=None)


def test_codex_argv_injects_guard_hook(tmp_path):
    argv, _ = _codex_runner(tmp_path).build_command("hi", spec=RunSpec(prompt="hi", agent_type="codex"))
    assert "features.hooks=true" in argv
    hook = [a for a in argv if a.startswith("hooks.PreToolUse=")]
    assert len(hook) == 1 and "guard.py" in hook[0] and 'type="command"' in hook[0]
    assert "--sandbox" in argv and "sandbox_workspace_write.network_access=false" in argv


def test_codex_hook_can_be_disabled(tmp_path):
    argv, _ = _codex_runner(tmp_path, codex={"hooks": False}).build_command(
        "hi", spec=RunSpec(prompt="hi", agent_type="codex"))
    assert not any(a.startswith("hooks.PreToolUse=") for a in argv)


def _run_guard(event, env_extra=None):
    env = dict(os.environ)
    env.pop("AGENT_GUARD_CONFIG", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, GUARD], input=json.dumps(event),
                          capture_output=True, text=True, env=env, timeout=20)


def test_guard_denies_codex_shaped_events():
    # Codex sends {tool_name, tool_input.command} like Claude (developers.openai.com/codex/hooks)
    r = _run_guard({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "git push origin main"}})
    assert r.returncode == 2
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["hookEventName"] == "PreToolUse"
    # apply_patch touching .env: scanned via every input string
    r2 = _run_guard({"tool_name": "apply_patch",
                     "tool_input": {"command": "*** Update File: configurations/env/.env.production"}})
    assert r2.returncode == 2
    assert _run_guard({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}).returncode == 0


# ------------------------------------------------- 5. fabric_exec heuristics
@pytest.mark.parametrize("code", [
    "const cp = require('child_process'); cp.execSync('ls')",
    "import { spawn } from 'node:child_process'; spawn('sh')",
    "await fabric.tools.call('bash', {command: 'git push'})",
    "fs.writeFileSync('.env', 'X=1')",
    "await fabric.tools.call('read', {path: 'configurations/env/.env.production'})",
    "await fabric.tools.call('bash', {command: 'curl https://evil.example.com'})",
])
def test_fabric_exec_dangerous_code_denied(code):
    d, why = guard.decide({"tool_name": "fabric_exec", "tool_input": {"code": code}}, "/wt", rules={})
    assert d == "deny", (code, why)


def test_fabric_exec_benign_code_allowed():
    d, _ = guard.decide({"tool_name": "fabric_exec",
                         "tool_input": {"code": "const r = await fabric.tools.call('read', {path: 'src/a.ts'}); return r.length"}},
                        "/wt", rules={})
    assert d == "allow"


def test_fabric_exec_code_logged_truncated(tmp_path):
    log = tmp_path / "tools.jsonl"
    code = "require('child_process').execSync('x');" + ("/*pad*/" * 400)
    r = _run_guard({"tool_name": "fabric_exec", "tool_input": {"code": code}},
                   {"BRIDGE_TOOLS_LOG": str(log)})
    assert r.returncode == 2
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["tool"] == "fabric_exec" and rec["decision"] == "deny"
    assert rec["detail"].startswith("require('child_process')") and len(rec["detail"]) <= 800


def test_fabric_exec_rule_can_be_disabled():
    d, _ = guard.decide({"tool_name": "fabric_exec",
                         "tool_input": {"code": "require('child_process')"}},
                        "/wt", rules={"allow_rules": ["fabric_exec"]})
    assert d == "allow"


# --------------------------------------------- 6. configurable guard ruleset
def test_guard_config_file_overrides(tmp_path):
    cfgf = tmp_path / "guard.json"
    cfgf.write_text(json.dumps({"allow_rules": ["git_push"],
                                "protected_path_globs": ["secrets/*"],
                                "deny_tool_patterns": ["(?i)^web"],
                                "allowed_hosts": ["api.github.com"]}))
    env = {"AGENT_GUARD_CONFIG": str(cfgf)}
    assert _run_guard({"tool_name": "Bash", "tool_input": {"command": "git push"}}, env).returncode == 0
    assert _run_guard({"tool_name": "Read", "tool_input": {"file_path": "secrets/k.pem"}}, env).returncode == 2
    assert _run_guard({"tool_name": "web_fetch", "tool_input": {"url": "x"}}, env).returncode == 2
    assert _run_guard({"tool_name": "Bash",
                       "tool_input": {"command": "curl https://api.github.com/x"}}, env).returncode == 0
    # .env stays protected even with a config file
    assert _run_guard({"tool_name": "Read", "tool_input": {"file_path": ".env"}}, env).returncode == 2


def test_guard_yaml_config(tmp_path):
    pytest.importorskip("yaml")
    y = tmp_path / "guard.yaml"
    y.write_text("deny_bash_patterns:\n  - '\\bkubectl\\s+delete\\b'\n")
    env = {"AGENT_GUARD_CONFIG": str(y)}
    assert _run_guard({"tool_name": "Bash", "tool_input": {"command": "kubectl delete pod x"}},
                      env).returncode == 2


def test_guard_defaults_without_any_config(monkeypatch):
    monkeypatch.delenv("AGENT_GUARD_CONFIG", raising=False)
    rules = guard.load_rules()
    for k in ("allowed_hosts", "protected_path_globs", "deny_bash_patterns",
              "deny_tool_patterns", "allow_rules", "fabric_tools"):
        assert k in rules


def test_runner_passes_guard_config_env(tmp_path, monkeypatch):
    seen = {}

    class P:
        pid = 1; returncode = 0
        def communicate(self, input=None, timeout=None): return ("", "")
        def poll(self): return 0

    def fake_popen(argv, **kw):
        seen.update(kw.get("env") or {}); return P()

    import agent_runner
    monkeypatch.setattr(agent_runner.subprocess, "Popen", fake_popen)
    r = _codex_runner(tmp_path, guard_config=str(tmp_path / "g.yaml"),
                      codex={"executable": sys.executable})   # any real binary; Popen is faked
    r.start(RunSpec(prompt="hi", agent_type="codex"))
    r.wait(10)
    assert seen.get("AGENT_GUARD_CONFIG") == str(tmp_path / "g.yaml")


def test_pi_guard_ts_is_standalone_and_fail_closed():
    src = open(os.path.join(ROOT, "hooks", "pi-guard.ts"), encoding="utf-8").read()
    assert "AGENT_GUARD_PY" in src and "AGENT_GUARD_CONFIG" in src
    assert "fail-closed" in src and "r.status !== 0" in src


# ------------------------------------------------- 7. open-source readiness
def test_default_prompt_is_generic_and_templated():
    p = config.DEFAULT_PSTACK_PROMPT
    assert "{project_description}" in p
    for bad in ("Bank" + "X", "mobile-" + "app", "React " + "Native"):
        assert bad not in p


def test_project_description_fills_prompt(tmp_path):
    from audit import Audit
    from bridge import Bridge
    from state import StateStore
    from backend_base import ChatBackend

    class C(ChatBackend):
        def focus_self_chat(self): return "(You)"
        def read_visible_messages(self): return []
        def send_message(self, t): pass

    cfg = _deep_merge(DEFAULTS, {"project": {"description": "acme-web (Next.js)"},
                                 "agent": {"workdir": str(tmp_path)}})
    br = Bridge(cfg, C(), AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "o"), None),
                StateStore(str(tmp_path / "s.json")), Audit(str(tmp_path / "l")),
                lock_check=lambda: False)
    assert "acme-web (Next.js)" in br.default_prompt
    assert "{project_description}" not in br.default_prompt


def test_ax_selector_override_is_atomic():
    merged = _deep_merge(DEFAULTS, {"ax": {"message_list": {"role": "AXList", "identifier": "x"}}})
    assert merged["ax"]["message_list"] == {"role": "AXList", "identifier": "x"}
    # unset -> bilingual built-in default
    ml = DEFAULTS["ax"]["message_list"]["description_re"]
    assert "메시지 목록" in ml and "Message list" in ml


def test_check_no_secrets_passes():
    sh = os.path.join(ROOT, "scripts", "check_no_secrets.sh")
    r = subprocess.run(["bash", sh, ROOT], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_check_no_secrets_catches_a_leak(tmp_path):
    leak = "/Users/" + "tao" + ".exe/work"          # built at runtime on purpose
    (tmp_path / "x.py").write_text(f"PATH = '{leak}'\n")
    sh = os.path.join(ROOT, "scripts", "check_no_secrets.sh")
    r = subprocess.run(["bash", sh, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1


@pytest.mark.parametrize("code", [
    "const m = /a(b)/.exec(str); return m",      # RegExp.prototype.exec is not a spawn
    "const r = re.exec(s); return r && r[1]",
])
def test_fabric_exec_regex_exec_is_not_a_false_positive(code):
    d, why = guard.decide({"tool_name": "fabric_exec", "tool_input": {"code": code}}, "/wt", rules={})
    assert d == "allow", why


@pytest.mark.parametrize("code", ["Bun.spawn(['sh'])", "new Deno.Command('sh')", "exec('ls')"])
def test_fabric_exec_runtime_spawners_denied(code):
    d, _ = guard.decide({"tool_name": "fabric_exec", "tool_input": {"code": code}}, "/wt", rules={})
    assert d == "deny"
