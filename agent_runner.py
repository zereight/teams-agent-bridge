"""Runs a headless coding agent CLI (Claude Code or Codex) in a worker thread.

Exact flags (verified against the docs on 2026-10-10, see README):

Claude Code  (https://code.claude.com/docs/en/cli-reference, .../headless)
    claude -p --output-format json --permission-mode <mode>
           --allowedTools <rules...> [--disallowedTools <rules...>]
           --max-turns <n> [--append-system-prompt <text>] [--resume <session_id>]
    prompt is written to stdin (prompt_via_stdin). `-p --output-format json`
    emits ONE json value whose shape varies by version: a single result object,
    OR an array of events, OR JSONL/stream-json lines. We tolerate all of them
    (see parse_claude_output) and keep only the last type=="result" event, so
    the system-init + tools dump is never shown to the user. session_id from the
    result lets a role resume its context with --resume.

Codex  (https://developers.openai.com/codex/noninteractive, .../cli/reference)
    codex exec --sandbox <mode> --cd <workdir> --output-last-message <file>
               [--json] [--skip-git-repo-check] [--ephemeral] -
    codex exec resume <session_id> --sandbox <mode> --cd <workdir> ... <prompt>
    "-" = read the prompt from stdin. `codex exec` never asks for approval
    (headless), so the sandbox is the only permission dial. With --json the first
    event is {"type":"thread.started","thread_id":...}; that thread_id is the
    session id used by `codex exec resume`.

The prompt is passed via stdin by default. On Windows `claude`/`codex` are
often `.cmd` shims executed through cmd.exe, where a user-supplied argument
containing & | % ^ " could be interpreted by the shell. stdin avoids that.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------------------------------------------- JSON parsing
def iter_json_values(text: str) -> List[Any]:
    """Return every top-level JSON value in `text`.

    Handles a single object, a single array, and JSONL / stream-json (one value
    per line, possibly separated by commas). Uses JSONDecoder(strict=False) so
    invalid control characters inside strings do not abort the parse. Lines that
    cannot be decoded are skipped rather than raising.
    """
    dec = json.JSONDecoder(strict=False)
    s = text or ""
    i, n = 0, len(text or "")
    out: List[Any] = []
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        try:
            val, end = dec.raw_decode(s, i)
        except json.JSONDecodeError:
            nl = s.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        out.append(val)
        i = end
    return out


def _flatten_events(values: List[Any]) -> List[Any]:
    events: List[Any] = []
    for v in values:
        if isinstance(v, list):
            events.extend(v)
        else:
            events.append(v)
    return events


def parse_claude_output(stdout: str) -> Optional[Dict[str, Any]]:
    """Pull the final result out of `claude -p --output-format json` output.

    Accepts a single object, an array of events, or JSONL/stream-json. Returns a
    dict {result, is_error, num_turns, total_cost_usd, session_id} taken from the
    LAST event with type=="result", or None if no result-like value is present.
    The system/init event and its tools list are never returned.
    """
    events = _flatten_events(iter_json_values(stdout))
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "result"]
    if not results:
        # some versions emit a bare object with a "result" key and no "type"
        results = [e for e in events
                   if isinstance(e, dict) and "result" in e and "type" not in e]
    if not results:
        return None
    r = results[-1]
    is_error = bool(r.get("is_error")) or r.get("subtype") not in (None, "success")
    return {
        "result": r.get("result"),
        "is_error": is_error,
        "num_turns": r.get("num_turns"),
        "total_cost_usd": r.get("total_cost_usd"),
        "session_id": r.get("session_id"),
    }


def parse_codex_output(stdout: str, last_msg_file: str = "") -> Dict[str, Any]:
    """Best-effort parse of `codex exec --json` output.

    Returns {text, session_id, is_error}. Final text prefers the
    --output-last-message file; otherwise the last agent_message item. session_id
    comes from the thread.started event's thread_id.
    """
    text = ""
    if last_msg_file and os.path.exists(last_msg_file):
        with open(last_msg_file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    session_id = None
    is_error = False
    for e in _flatten_events(iter_json_values(stdout)):
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t == "thread.started" and e.get("thread_id"):
            session_id = e["thread_id"]
        elif t == "turn.failed" or t == "error":
            is_error = True
        elif t == "item.completed" and not text:
            item = e.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                text = str(item["text"]).strip()
    return {"text": text, "session_id": session_id, "is_error": is_error}


def _pi_text_from_message(msg: Any) -> str:
    """Extract plain text from a pi assistant message's `content`."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(str(b["text"]))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts).strip()
    return ""


def parse_pi_output(stdout: str) -> Dict[str, Any]:
    """Parse `pi --mode json` JSONL output.

    Framing is strict JSONL (one JSON object per LF-terminated line); we also
    tolerate stray lines via iter_json_values. We take the LAST assistant
    `message_end` as the final text, the `session` header's id as the session id,
    and the highest cumulative `usage.cost.total` seen as the cost. Falls back to
    plain stdout text when no structured assistant message is present (print mode).
    """
    events = _flatten_events(iter_json_values(stdout))
    session_id = None
    cost = None
    text = ""
    is_error = False
    saw_event = False
    for e in events:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t:
            saw_event = True
        if t == "session" and e.get("id"):
            session_id = e["id"]
        elif t == "message_end":
            msg = e.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                tx = _pi_text_from_message(msg)
                if tx:
                    text = tx            # keep the last assistant message
                u = msg.get("usage") if isinstance(msg, dict) else None
                c = (u or {}).get("cost", {}).get("total") if isinstance(u, dict) else None
                if isinstance(c, (int, float)):
                    cost = c if cost is None else max(cost, c)
        elif t == "message_update":
            u = e.get("usage")
            c = (u or {}).get("cost", {}).get("total") if isinstance(u, dict) else None
            if isinstance(c, (int, float)):
                cost = c if cost is None else max(cost, c)
        elif t in ("error", "auto_retry_end") and e.get("success") is False:
            is_error = True
        elif t == "error":
            is_error = True
    if not text and not saw_event:
        text = (stdout or "").strip()    # print mode (-p): plain final text
    return {"text": text, "session_id": session_id, "cost_usd": cost,
            "is_error": is_error and not text}


@dataclass
class RunSpec:
    """One agent invocation. Carries role overrides + chain metadata."""
    prompt: str
    agent_type: Optional[str] = None          # override claude|codex|custom
    append_system_prompt: Optional[str] = None  # role instructions (+ handoff hint)
    allowed_tools: Optional[List[str]] = None   # per-role allow override (pre-sanitised by caller)
    resume_session_id: Optional[str] = None     # claude --resume / codex exec resume
    track_session: bool = False                 # capture session id (codex adds --json)
    role: str = ""
    workdir: Optional[str] = None               # per-task worktree (overrides runner workdir)
    dry: bool = False                           # /dry: plan/read-only, no writes
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTask:
    task_id: str
    prompt: str
    started_at: float = 0.0
    role: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    task_id: str
    prompt: str
    ok: bool
    exit_code: Optional[int]
    summary: str
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    timed_out: bool = False
    stopped: bool = False
    cost_usd: Optional[float] = None
    output_path: str = ""
    argv: List[str] = field(default_factory=list)
    session_id: Optional[str] = None
    num_turns: Optional[int] = None
    is_error: bool = False
    role: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


DEFAULT_SYSTEM_PROMPT = (
    "You are being driven remotely through a Microsoft Teams self-chat. "
    "Work only inside the current working directory. Do not push, deploy, "
    "publish, or delete files unless the instruction explicitly says so. "
    "Finish with a short summary in Korean (what you did, files changed, "
    "test results, anything the user must check)."
)


class AgentRunner:
    def __init__(self, cfg: Dict[str, Any], workdir: str, output_dir: str, audit=None):
        self.cfg = cfg
        self.workdir = workdir
        self.output_dir = output_dir
        self.audit = audit
        os.makedirs(output_dir, exist_ok=True)
        self.data_dir = os.path.dirname(os.path.abspath(output_dir))
        self.logs_dir = os.path.join(self.data_dir, "logs")
        self.tools_log = os.path.join(self.logs_dir, "tools.jsonl")
        _hooks = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks")
        self.guard_path = os.path.join(_hooks, "guard.py")
        self.pi_guard_path = os.path.join(_hooks, "pi-guard.ts")
        self._settings_path = os.path.join(self.data_dir, "claude-settings.json")
        self.results: "queue.Queue[AgentResult]" = queue.Queue()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._current: Optional[AgentTask] = None
        self._stop_requested = False
        self._thread: Optional[threading.Thread] = None
        self._step = ""           # best-effort last step name (progress)

    # ------------------------------------------------------------- argv
    def _resolve_exe(self, name: str) -> str:
        exe = shutil.which(name)
        if not exe:
            raise FileNotFoundError(f"'{name}' 실행 파일을 PATH에서 못 찾았어요")
        return exe

    def _agent_type(self, spec: Optional[RunSpec]) -> str:
        if spec and spec.agent_type:
            return spec.agent_type
        return self.cfg.get("type") or self.cfg.get("default") or "claude"

    def _ensure_claude_settings(self) -> Optional[str]:
        """Write (once) a settings file that installs the PreToolUse guard hook,
        then return its path. Passed with `claude --settings <file>` so the user's
        global ~/.claude config is untouched. Returns None if disabled/missing."""
        if not (self.cfg.get("hook", {}) or {}).get("enabled", True):
            return None
        if not os.path.exists(self.guard_path):
            return None
        py = shutil.which("python3") or shutil.which("python") or "python3"
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [
                        {"type": "command", "command": py,
                         "args": [self.guard_path], "timeout": 15,
                         "onFailure": "block"}
                    ]}
                ]
            }
        }
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except OSError:
            return None
        return self._settings_path

    def _pi_guard_ext(self) -> Optional[str]:
        """Path to the pi guard extension, or None when disabled/missing."""
        if not (self.cfg.get("hook", {}) or {}).get("enabled", True):
            return None
        return self.pi_guard_path if os.path.exists(self.pi_guard_path) else None

    def _codex_hook_args(self) -> List[str]:
        """`-c` overrides installing a PreToolUse hook -> guard.py, or [] if off."""
        c = self.cfg.get("codex", {}) or {}
        if not c.get("hooks", True):
            return []
        if not (self.cfg.get("hook", {}) or {}).get("enabled", True):
            return []
        if not os.path.exists(self.guard_path):
            return []
        py = shutil.which("python3") or shutil.which("python") or "python3"
        cmd = f"{py} {shlex.quote(self.guard_path)}"
        def toml_str(x: str) -> str:
            return '"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"'
        pre = ("[{matcher=\".*\",hooks=[{type=\"command\",command="
               + toml_str(cmd) + ",timeout=15}]}]")
        return ["-c", "features.hooks=true", "-c", "hooks.PreToolUse=" + pre]

    def _pi_write_capable(self, spec: Optional[RunSpec]) -> bool:
        """True if a pi run could modify files/run shell (bash/edit/write/powershell)."""
        if spec and spec.dry:
            return False
        tools = (self.cfg.get("pi", {}) or {}).get("tools") or []
        if not tools:
            return True                      # pi defaults include bash/edit/write
        heads = {str(t).lower().lstrip("+-") for t in tools}
        return bool(heads & {"bash", "edit", "write", "powershell"})

    def _pi_refusal_reason(self, spec: Optional[RunSpec]) -> str:
        """Non-empty reason to REFUSE an unguarded, write-capable pi run."""
        c = self.cfg.get("pi", {}) or {}
        if c.get("allow_unguarded"):
            return ""
        if not self._pi_write_capable(spec):
            return ""
        if self._pi_guard_ext():
            return ""
        return ("pi 안전 가드(pi-guard.ts)가 꺼져 있거나 없어서 쓰기 가능한 pi 실행을 거부했어요. "
                "agent.hook.enabled 를 켜거나(가드 설치), 읽기 전용 도구만 쓰도록 agent.pi.tools 를 "
                "제한하거나, 정말 필요하면 agent.pi.allow_unguarded: true 로 허용하세요.")

    def build_command(self, prompt: str, last_msg_file: str = "",
                      spec: Optional[RunSpec] = None) -> Tuple[List[str], Optional[str]]:
        """Return (argv, stdin_text)."""
        kind = self._agent_type(spec)
        via_stdin = bool(self.cfg.get("prompt_via_stdin", True))
        role_instr = spec.append_system_prompt if spec else None
        resume = spec.resume_session_id if spec else None
        track = bool(spec and spec.track_session)
        dry = bool(spec and spec.dry)
        wd = (spec.workdir if spec and spec.workdir else self.workdir)

        if kind == "claude":
            c = self.cfg["claude"]
            argv = [c.get("executable", "claude"), "-p"]
            if not via_stdin:
                argv.append(prompt)
            argv += ["--output-format", "json"]
            mode = "plan" if dry else c.get("permission_mode")
            if mode:
                argv += ["--permission-mode", mode]
            settings = self._ensure_claude_settings()
            if settings:
                argv += ["--settings", settings]
            allowed = (spec.allowed_tools if spec and spec.allowed_tools is not None
                       else c.get("allowed_tools"))
            if allowed:
                argv += ["--allowedTools", ",".join(allowed)]
            if c.get("disallowed_tools"):
                argv += ["--disallowedTools", ",".join(c["disallowed_tools"])]
            if c.get("max_turns"):
                argv += ["--max-turns", str(int(c["max_turns"]))]
            base = c.get("append_system_prompt") or DEFAULT_SYSTEM_PROMPT
            sp = base + ("\n\n" + role_instr if role_instr else "")
            argv += ["--append-system-prompt", sp]
            if resume:
                argv += ["--resume", resume]
            argv += [str(a) for a in c.get("extra_args") or []]
            return argv, (prompt if via_stdin else None)

        if kind == "codex":
            c = self.cfg["codex"]
            exe = c.get("executable", "codex")
            sandbox = "read-only" if dry else c.get("sandbox", "read-only")
            net = "false" if not c.get("network_access", False) else "true"
            net_cfg = ["-c", f"sandbox_workspace_write.network_access={net}"]
            # Codex 0.154 fires PreToolUse hooks that can DENY a tool call with the
            # same shape guard.py already emits (permissionDecision=deny / exit 2);
            # verified in https://developers.openai.com/codex/hooks . We inject it
            # inline with -c (highest precedence) so the user's ~/.codex config is
            # untouched, and enable the hooks feature for the run. The sandbox stays
            # the primary dial; this is defense-in-depth for git push / .env reads.
            net_cfg += self._codex_hook_args()
            text = prompt
            if role_instr:
                text = f"[역할 지침]\n{role_instr}\n\n[작업]\n{prompt}"
            if resume:
                # `codex exec resume <id>` continues that session; prompt as arg
                argv = [exe, "exec", "resume", resume,
                        "--sandbox", sandbox, "--cd", wd] + net_cfg
                if last_msg_file:
                    argv += ["--output-last-message", last_msg_file]
                if track:
                    argv.append("--json")
                argv += [str(a) for a in c.get("extra_args") or []]
                argv.append(text)
                return argv, None
            argv = [exe, "exec", "--sandbox", sandbox, "--cd", wd] + net_cfg
            if last_msg_file:
                argv += ["--output-last-message", last_msg_file]
            if track:
                argv.append("--json")
            if c.get("skip_git_repo_check"):
                argv.append("--skip-git-repo-check")
            if c.get("ephemeral"):
                argv.append("--ephemeral")
            argv += [str(a) for a in c.get("extra_args") or []]
            argv.append("-" if via_stdin else text)
            return argv, (text if via_stdin else None)

        if kind == "pi":
            c = self.cfg.get("pi", {}) or {}
            exe = c.get("executable", "pi")
            argv = [exe]
            mode = "print" if dry and c.get("mode", "json") == "print" else c.get("mode", "json")
            if mode == "print":
                argv.append("-p")
            else:
                argv += ["--mode", "json"]
            # Only override provider/model when explicitly configured; otherwise
            # inherit the user's ~/.pi/agent/settings.json (defaultProvider/model).
            if c.get("model"):
                if c.get("provider"):
                    argv += ["--provider", str(c["provider"])]
                argv += ["--model", str(c["model"])]
            if c.get("thinking"):
                argv += ["--thinking", str(c["thinking"])]
            # tools: dry => read-only; else honour config allow/deny (empty = pi defaults)
            if dry:
                argv += ["--tools", "read,grep,find,ls"]
            elif c.get("tools"):
                argv += ["--tools", ",".join(str(t) for t in c["tools"])]
            if c.get("exclude_tools"):
                argv += ["--exclude-tools", ",".join(str(t) for t in c["exclude_tools"])]
            # system prompt: base + role instructions, appended (keeps pi's own prompt)
            base = c.get("append_system_prompt") or DEFAULT_SYSTEM_PROMPT
            argv += ["--append-system-prompt", base]
            if role_instr:
                argv += ["--append-system-prompt", role_instr]
            # per-role stable session id (created if missing); plain tasks: no session
            if resume:
                argv += ["--session-id", resume]
            else:
                argv.append("--no-session")
            # skills (e.g. pstack-claude skills dir) and the guard extension
            for sk in c.get("skills") or []:
                argv += ["--skill", str(sk)]
            guard_ext = self._pi_guard_ext()
            if guard_ext:
                argv += ["-e", guard_ext]
            argv += [str(a) for a in c.get("extra_args") or []]
            return argv, (prompt if via_stdin else None)

        if kind == "custom":
            tmpl = self.cfg["custom"]["command"]
            if not tmpl:
                raise ValueError("agent.custom.command 가 비어 있어요")
            text = prompt
            if role_instr:
                text = f"[role:{role_instr}]\n{prompt}"
            argv = [str(a).replace("{workdir}", wd)
                    .replace("{prompt}", "" if via_stdin else text) for a in tmpl]
            return argv, (text if via_stdin else None)
        raise ValueError(f"알 수 없는 agent.type: {kind}")

    # ------------------------------------------------------------- state
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    @property
    def current(self) -> Optional[AgentTask]:
        with self._lock:
            return self._current

    @property
    def step(self) -> str:
        with self._lock:
            return self._step

    # ------------------------------------------------------------- control
    def start(self, prompt_or_spec, *, role: str = "", meta: Optional[dict] = None) -> AgentTask:
        spec = prompt_or_spec if isinstance(prompt_or_spec, RunSpec) else RunSpec(
            prompt=str(prompt_or_spec), role=role, meta=meta or {})
        if role and not spec.role:
            spec.role = role
        if meta and not spec.meta:
            spec.meta = meta
        with self._lock:
            if self._current is not None:
                raise RuntimeError("busy")
            task = AgentTask(task_id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                             prompt=spec.prompt, started_at=time.time(),
                             role=spec.role, meta=dict(spec.meta))
            self._current = task
            self._stop_requested = False
            self._step = ""
        self._thread = threading.Thread(target=self._run, args=(task, spec), daemon=True)
        self._thread.start()
        return task

    def stop(self) -> bool:
        with self._lock:
            self._stop_requested = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        _kill_tree(proc)
        return True

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------- worker
    def _run(self, task: AgentTask, spec: RunSpec) -> None:
        t0 = time.time()
        timeout = float(self.cfg.get("timeout_sec", 1800))
        kind = self._agent_type(spec)
        wd = (spec.workdir if spec and spec.workdir else self.workdir)
        if kind == "pi":
            refusal = self._pi_refusal_reason(spec)
            if refusal:
                res = AgentResult(task_id=task.task_id, prompt=task.prompt, ok=False,
                                  exit_code=None, summary=refusal, is_error=True,
                                  duration_sec=round(time.time() - t0, 1),
                                  role=task.role, meta=dict(task.meta))
                res.output_path = self._write_output(res)
                if self.audit:
                    self.audit.event("agent_refused", task_id=task.task_id,
                                     role=task.role, reason="pi_unguarded")
                with self._lock:
                    self._current = None
                    self._step = ""
                self.results.put(res)
                return
        last_msg_file = ""
        if kind == "codex":
            fd, last_msg_file = tempfile.mkstemp(prefix="codex-last-", suffix=".txt", dir=self.output_dir)
            os.close(fd)
        argv: List[str] = []
        stdout = stderr = ""
        exit_code: Optional[int] = None
        timed_out = False
        try:
            argv, stdin_text = self.build_command(task.prompt, last_msg_file, spec)
            exe = self._resolve_exe(argv[0])
            if (os.name == "nt" and exe.lower().endswith((".cmd", ".bat"))
                    and stdin_text is None and kind != "codex"):
                raise RuntimeError("prompt_via_stdin=false 는 .cmd/.bat 실행 파일에서 허용하지 않아요 "
                                   "(cmd.exe 인용 문제)")
            argv[0] = exe
            if self.audit:
                self.audit.event("agent_start", task_id=task.task_id, role=task.role,
                                 argv=_redact_argv(argv), cwd=wd)
            env = dict(os.environ)
            env["BRIDGE_TASK_ID"] = task.task_id
            env["BRIDGE_ROLE"] = task.role or ""
            env["BRIDGE_WORKTREE"] = wd or ""
            env["BRIDGE_TOOLS_LOG"] = self.tools_log
            allow = self.cfg.get("network_allowlist") or []
            env["BRIDGE_NET_ALLOWLIST"] = ",".join(str(h) for h in allow)
            env["BRIDGE_GUARD_PY"] = self.guard_path
            env["BRIDGE_PYTHON"] = shutil.which("python3") or shutil.which("python") or "python3"
            blockpat = (self.cfg.get("pi", {}) or {}).get("block_tool_patterns") or []
            env["BRIDGE_BLOCK_TOOL_PATTERNS"] = ",".join(str(p) for p in blockpat)
            gcfg = self.cfg.get("guard_config") or ""
            if gcfg:
                env["AGENT_GUARD_CONFIG"] = os.path.expanduser(str(gcfg))
            kwargs: Dict[str, Any] = dict(cwd=wd, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, encoding="utf-8", errors="replace", env=env)
            if os.name == "nt":
                kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                           | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs)
            with self._lock:
                self._proc = proc
                stop_now = self._stop_requested
            if stop_now:
                _kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(stdin_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                stdout, stderr = proc.communicate()
            exit_code = proc.returncode
        except Exception as e:
            stderr = (stderr or "") + f"\n[bridge] {type(e).__name__}: {e}"
        finally:
            with self._lock:
                stopped = self._stop_requested
                self._proc = None

        res = AgentResult(task_id=task.task_id, prompt=task.prompt, ok=False, exit_code=exit_code,
                          summary="", stdout=stdout or "", stderr=stderr or "",
                          duration_sec=round(time.time() - t0, 1), timed_out=timed_out,
                          stopped=stopped, argv=_redact_argv(argv), role=task.role,
                          meta=dict(task.meta))
        self._fill_summary(res, kind, exit_code, last_msg_file)
        if timed_out:
            res.ok = False
            res.is_error = True
            res.summary = f"시간 초과({int(timeout)}초)로 중단했어요. 전체 로그를 확인하세요."
        if stopped:
            res.ok = False
            res.is_error = True
            res.summary = "/stop 으로 중단했어요."
        res.output_path = self._write_output(res)
        if last_msg_file:
            try:
                os.remove(last_msg_file)
            except OSError:
                pass
        if self.audit:
            self.audit.event("agent_end", task_id=task.task_id, role=task.role, exit_code=exit_code,
                             ok=res.ok, is_error=res.is_error, timed_out=timed_out, stopped=stopped,
                             duration=res.duration_sec, cost_usd=res.cost_usd,
                             session_id=res.session_id, output=res.output_path)
        with self._lock:
            self._current = None
            self._step = ""
        self.results.put(res)

    def _fill_summary(self, res: AgentResult, kind: str, exit_code: Optional[int],
                      last_msg_file: str) -> None:
        if kind == "claude":
            parsed = parse_claude_output(res.stdout)
            if parsed is None:
                res.ok = False
                res.is_error = True
                res.summary = ("에이전트 출력을 해석하지 못했어요. 전체 로그를 확인하세요."
                               if exit_code == 0 else
                               f"에이전트가 실패했어요 (exit {exit_code}). 전체 로그를 확인하세요.")
                return
            res.session_id = parsed.get("session_id")
            res.num_turns = parsed.get("num_turns")
            res.cost_usd = parsed.get("total_cost_usd")
            res.is_error = bool(parsed.get("is_error")) or exit_code not in (0, None)
            text = parsed.get("result")
            if res.is_error or not text:
                res.ok = False
                res.summary = (str(text).strip() if text else
                               "에이전트가 오류로 끝났어요.") + "\n(자세한 내용은 전체 로그를 확인하세요.)"
                return
            res.ok = True
            res.summary = str(text).strip()
            return
        if kind == "codex":
            parsed = parse_codex_output(res.stdout, last_msg_file)
            res.session_id = parsed.get("session_id")
            text = parsed.get("text")
            res.is_error = bool(parsed.get("is_error")) or exit_code not in (0, None)
            if res.is_error or not text:
                res.ok = False
                res.summary = (str(text).strip() if text else
                               "에이전트가 오류로 끝났어요.") + "\n(자세한 내용은 전체 로그를 확인하세요.)"
                return
            res.ok = True
            res.summary = str(text).strip()
            return
        if kind == "pi":
            parsed = parse_pi_output(res.stdout)
            res.session_id = parsed.get("session_id")
            res.cost_usd = parsed.get("cost_usd")
            text = parsed.get("text")
            res.is_error = bool(parsed.get("is_error")) or exit_code not in (0, None) or not text
            if res.is_error or not text:
                res.ok = False
                res.summary = (str(text).strip() if text else
                               "에이전트가 오류로 끝났어요.") + "\n(자세한 내용은 전체 로그를 확인하세요.)"
                return
            res.ok = True
            res.summary = str(text).strip()
            return
        # custom / fallback
        res.ok = exit_code == 0
        res.is_error = not res.ok
        text = (res.stdout or "").strip()
        if not text:
            text = (res.stderr or "").strip()[-2000:] or "(출력 없음)"
        res.summary = text

    def _write_output(self, r: AgentResult) -> str:
        path = os.path.join(self.output_dir, f"{r.task_id}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"task_id: {r.task_id}\nrole: {r.role}\nexit_code: {r.exit_code}\n"
                    f"ok: {r.ok}\nis_error: {r.is_error}\nsession_id: {r.session_id}\n"
                    f"num_turns: {r.num_turns}\nduration_sec: {r.duration_sec}\n"
                    f"timed_out: {r.timed_out}\nstopped: {r.stopped}\ncost_usd: {r.cost_usd}\n"
                    f"argv: {r.argv}\n\n===== PROMPT =====\n{r.prompt}\n\n"
                    f"===== SUMMARY =====\n{r.summary}\n\n"
                    f"===== STDOUT =====\n{r.stdout}\n\n===== STDERR =====\n{r.stderr}\n")
        return path


def _redact_argv(argv: List[str]) -> List[str]:
    return [a if len(a) <= 200 else a[:200] + "…" for a in argv]


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
