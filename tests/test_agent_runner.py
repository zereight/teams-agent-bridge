import sys

from agent_runner import AgentRunner
from config import DEFAULTS, _deep_merge


def runner(tmp_path, **agent):
    cfg = _deep_merge(DEFAULTS, {"agent": {"workdir": str(tmp_path), **agent}})
    return AgentRunner(cfg["agent"], str(tmp_path), str(tmp_path / "out"))


def test_claude_argv(tmp_path):
    r = runner(tmp_path, type="claude")
    argv, stdin = r.build_command("hello & del *")
    assert argv[:2] == ["claude", "-p"]
    assert "hello & del *" not in argv and stdin == "hello & del *"   # prompt only via stdin
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in argv and "--max-turns" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv


def test_codex_argv(tmp_path):
    r = runner(tmp_path, type="codex")
    argv, stdin = r.build_command("fix tests", last_msg_file="/tmp/x.txt")
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[argv.index("--output-last-message") + 1] == "/tmp/x.txt"
    assert argv[-1] == "-" and stdin == "fix tests"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_claude_resume_and_allowed_override(tmp_path):
    from agent_runner import RunSpec
    r = runner(tmp_path, type="claude",
               claude={"disallowed_tools": ["Bash"]})
    from roles import sanitize_allowed
    # bridge sanitises per-role allow before it reaches the runner
    allowed = sanitize_allowed(["Read", "Bash", "Bash(git push:*)"], ["Bash"])
    spec = RunSpec(prompt="go", append_system_prompt="너는 QA야", resume_session_id="sess-123",
                   allowed_tools=allowed, role="qa")
    argv, stdin = r.build_command("go", spec=spec)
    assert argv[argv.index("--resume") + 1] == "sess-123"
    # role instructions ride on --append-system-prompt (appended after the guard text)
    sp = argv[argv.index("--append-system-prompt") + 1]
    assert "너는 QA야" in sp and "Teams" in sp
    # sanitised allow list drops every Bash variant that collides with the global disallow
    allow = argv[argv.index("--allowedTools") + 1]
    assert "Read" in allow and "Bash" not in allow


def test_codex_resume_argv(tmp_path):
    from agent_runner import RunSpec
    r = runner(tmp_path, type="codex")
    spec = RunSpec(prompt="keep going", resume_session_id="thread-9", track_session=True,
                   append_system_prompt="너는 dev야", role="dev")
    argv, stdin = r.build_command("keep going", last_msg_file="/tmp/x.txt", spec=spec)
    assert argv[:3] == ["codex", "exec", "resume"] and argv[3] == "thread-9"
    assert "--json" in argv and argv[-1].startswith("[역할 지침]")
    assert "너는 dev야" in argv[-1] and stdin is None


def test_custom_run_and_output_file(tmp_path):
    r = runner(tmp_path, type="custom",
               custom={"command": [sys.executable, "-c", "import sys;print('got:'+sys.stdin.read())"]})
    r.start("안녕")
    r.wait(10)
    res = r.results.get_nowait()
    assert res.ok and "got:안녕" in res.summary
    assert open(res.output_path, encoding="utf-8").read().count("안녕") >= 2


def test_missing_executable(tmp_path):
    r = runner(tmp_path, type="custom", custom={"command": ["definitely-not-a-real-binary-xyz"]})
    r.start("x")
    r.wait(10)
    res = r.results.get_nowait()
    assert not res.ok and "PATH" in res.summary
