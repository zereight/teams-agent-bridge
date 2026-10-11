#!/usr/bin/env python3
"""agent-guard: a standalone, reusable tool-call guard for coding agents.

One script, three agents. It reads a PreToolUse-style event on stdin and either
allows it (exit 0) or denies it (prints a deny decision + exits 2). The deny
output uses the shape Claude Code AND Codex both accept:
    {"hookSpecificOutput":{"hookEventName":"PreToolUse",
      "permissionDecision":"deny","permissionDecisionReason":"..."}}
plus exit code 2 with the reason on stderr. The pi guard extension
(hooks/pi-guard.ts) forwards pi's `tool_call` events here in the same shape.

Because it is one dependency-light file, you can use it on its own:
    pi -e /path/to/pi-guard.ts         # pi-guard shells out to this script
    claude --settings <generated>      # PreToolUse -> this script
    codex -c hooks.PreToolUse=...       # PreToolUse -> this script

WHAT IT IS / IS NOT (security model):
  It is a HEURISTIC deny-list on tool arguments, not a sandbox. It cannot see
  side effects, cannot stop a tool it does not intercept, and can be bypassed by
  obfuscation. Treat it as defense-in-depth on top of each agent's own sandbox
  (Codex workspace-write + network off) and worktree isolation, not a substitute.

Rules come from built-in DEFAULT_RULES, overridden (deep-merged) by, in order:
    1. $AGENT_GUARD_CONFIG                 (file path; .yaml or .json)
    2. ./guard.yaml / ./guard.json        (next to this script)
    3. ~/.config/agent-guard/guard.yaml   (or guard.json)
Legacy bridge env vars still apply and are merged in:
    BRIDGE_WORKTREE          absolute workdir; rm -rf is allowed only inside it
    BRIDGE_NET_ALLOWLIST     comma-separated extra allowed hosts
    BRIDGE_BLOCK_TOOL_PATTERNS  comma-separated extra tool-name deny regexes
    BRIDGE_TOOLS_LOG / BRIDGE_TASK_ID / BRIDGE_ROLE   JSONL audit log + tags

Rule schema (all keys optional; see guard.example.yaml):
    allowed_hosts:        [str]   hosts curl/wget/ssh/scp/rsync may reach
    protected_path_globs: [str]   fnmatch globs always denied (plus built-in .env)
    deny_bash_patterns:   [str]   extra regexes denied in bash commands
    deny_tool_patterns:   [str]   regexes matched against the tool NAME
    extra_deny_patterns:  [str]   regexes denied in bash commands AND fabric code
    allow_rules:          [str]   disable a built-in rule by id:
                                  git_push | git_reset_hard | git_clean_force |
                                  sudo | rm_rf_outside | net_tools | env_files |
                                  fabric_exec
    fabric_tools:         [str]   tool names whose `code` arg is inspected
                                  (default ["fabric_exec"])
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import time

# ---- built-in dangerous patterns (ids match allow_rules) -------------------
_GIT_PUSH = re.compile(r"\bgit\s+(?:-\S+\s+|\S+=\S+\s+)*push\b", re.I)
_GIT_RESET_HARD = re.compile(r"\bgit\s+reset\b[^\n|&;]*--hard\b", re.I)
_GIT_CLEAN_F = re.compile(r"\bgit\s+clean\b[^\n|&;]*-\S*f", re.I)
_SUDO = re.compile(r"(^|[\s|&;(])sudo\b", re.I)
_RM_RECURSIVE_FORCE = re.compile(r"\brm\b(?=[^\n|&;]*\s-\S*r)(?=[^\n|&;]*\s-\S*f)", re.I)
_NET_TOOLS = re.compile(r"(^|[\s|&;(])(curl|wget|ssh|scp|sftp|rsync)\b", re.I)
# config / env files, anywhere (configurations/env/..., .env, .env.production, **/.env*)
_ENV_FILE = re.compile(r"(configurations/env|(^|[\s/\"'=:(])\.env(\.[\w.\-]+)?)", re.I)
# fabric_exec: JS that spawns processes directly (bypassing per-tool guards)
# Distinctive names match anywhere; bare spawn(/exec( only when NOT a method call
# (so ordinary JS like /re/.exec(str) or regex.exec(s) is not a false positive).
_JS_SPAWN = re.compile(
    r"(child_process|\bspawnSync\b|\bexecSync\b|\bexecFileSync\b|\bexecFile\s*\("
    r"|(?<![.\w$])(?:spawn|exec)\s*\("
    r"|\bBun\.(?:spawn|spawnSync|\$)|\bDeno\.(?:Command|run)\b|process\.binding)")
_JS_FS_ENV_WRITE = re.compile(r"(writeFile|appendFile|createWriteStream|\.write\s*\()"
                              r"[^\n;]*\.env", re.I)

DEFAULT_RULES = {
    "allowed_hosts": [],
    "protected_path_globs": [],      # user globs; built-in .env rule always applies
    "deny_bash_patterns": [],
    "deny_tool_patterns": [],
    "extra_deny_patterns": [],
    "allow_rules": [],
    "fabric_tools": ["fabric_exec"],
}


# ---- config loading --------------------------------------------------------
def _load_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {}
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # optional; standalone users can use guard.json instead
            return yaml.safe_load(raw) or {}
        except Exception:
            return {}
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _candidate_config_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.get("AGENT_GUARD_CONFIG")
    paths = []
    if env:
        paths.append(env)
    paths += [os.path.join(here, "guard.yaml"), os.path.join(here, "guard.json")]
    home = os.path.expanduser("~/.config/agent-guard")
    paths += [os.path.join(home, "guard.yaml"), os.path.join(home, "guard.json")]
    return paths


def load_rules(path: str = "") -> dict:
    """Built-in defaults, deep-merged with the first config file found + env."""
    rules = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_RULES.items()}
    for p in ([path] if path else []) + _candidate_config_paths():
        if p and os.path.exists(p):
            over = _load_file(p)
            if isinstance(over, dict):
                for k, v in over.items():
                    if isinstance(v, list) and isinstance(rules.get(k), list):
                        rules[k] = v           # replace list wholesale (explicit)
                    else:
                        rules[k] = v
            break
    # legacy env merges (append, do not override the file)
    allow = os.environ.get("BRIDGE_NET_ALLOWLIST", "")
    if allow:
        rules["allowed_hosts"] = list(rules.get("allowed_hosts") or []) + \
            [h for h in allow.split(",") if h]
    blockpat = os.environ.get("BRIDGE_BLOCK_TOOL_PATTERNS", "")
    if blockpat:
        rules["deny_tool_patterns"] = list(rules.get("deny_tool_patterns") or []) + \
            [p for p in blockpat.split(",") if p]
    return rules


# ---- helpers ---------------------------------------------------------------
def _host_from_token(tok: str) -> str:
    t = tok.strip().strip("'\"")
    m = re.match(r"[a-zA-Z][a-zA-Z0-9+.\-]*://([^/\s:]+)", t)
    if m:
        return m.group(1).lower()
    if "@" in t and "/" not in t.split("@", 1)[0]:
        return t.split("@", 1)[1].split(":", 1)[0].split("/", 1)[0].lower()
    if re.match(r"^[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(:\d+)?$", t):
        return t.split(":", 1)[0].lower()
    return ""


def _net_hosts(command: str):
    hosts, unknown = [], False
    for seg in re.split(r"[|&;]+|\$\(|\)|`", command):
        if not _NET_TOOLS.search(seg):
            continue
        found = False
        for tok in seg.split()[1:]:
            if tok.startswith("-"):
                continue
            h = _host_from_token(tok)
            if h:
                hosts.append(h)
                found = True
        if not found:
            unknown = True
    return hosts, unknown


def _rm_targets_outside(command: str, worktree: str):
    wt = os.path.realpath(worktree) if worktree else ""
    for seg in re.split(r"[|&;]+", command):
        if not _RM_RECURSIVE_FORCE.search(seg):
            continue
        toks = seg.split()
        try:
            toks = toks[toks.index("rm") + 1:]
        except ValueError:
            return True
        paths = [t for t in toks if not t.startswith("-")]
        if not paths:
            return True
        for p in paths:
            p = p.strip("'\"")
            if p in ("/", "~", "~/", "..", "*") or p.startswith(("/", "~")) or ".." in p.split("/"):
                if not (wt and os.path.realpath(os.path.join(wt, p)).startswith(wt + os.sep)):
                    if not (p.startswith(wt) if wt else False):
                        return True
            if wt:
                full = os.path.realpath(os.path.join(wt, p))
                if full != wt and not full.startswith(wt + os.sep):
                    return True
            else:
                return True
    return False


def _strings_in_input(tool_input: dict):
    """Every string value in the tool input (shallow + one level of lists)."""
    out = []
    if not isinstance(tool_input, dict):
        return out
    for v in tool_input.values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
    return out


def _enabled(rules: dict, rule_id: str) -> bool:
    return rule_id not in set(rules.get("allow_rules") or [])


def _bash_denials(command: str, rules: dict, worktree: str):
    """Return (reason or '') for a bash-like command string under `rules`."""
    allowed_hosts = {h.strip().lower() for h in (rules.get("allowed_hosts") or []) if h.strip()}
    if _enabled(rules, "git_push") and _GIT_PUSH.search(command):
        return "git push 는 금지돼 있어요 (브리지는 절대 푸시하지 않아요)."
    if _enabled(rules, "git_reset_hard") and _GIT_RESET_HARD.search(command):
        return "git reset --hard 는 금지돼 있어요."
    if _enabled(rules, "git_clean_force") and _GIT_CLEAN_F.search(command):
        return "git clean -f 는 금지돼 있어요."
    if _enabled(rules, "sudo") and _SUDO.search(command):
        return "sudo 는 금지돼 있어요."
    if _enabled(rules, "rm_rf_outside") and _RM_RECURSIVE_FORCE.search(command) \
            and _rm_targets_outside(command, worktree):
        return "작업 폴더(워크트리) 밖의 rm -rf 는 금지돼 있어요."
    if _enabled(rules, "net_tools"):
        hosts, unknown = _net_hosts(command)
        bad = [h for h in hosts if h not in allowed_hosts]
        if unknown or bad:
            tgt = ", ".join(bad) if bad else "(알 수 없는 호스트)"
            return f"허용되지 않은 네트워크 접근({tgt}). 허용 목록: {sorted(allowed_hosts) or '없음'}"
    for pat in (rules.get("deny_bash_patterns") or []) + (rules.get("extra_deny_patterns") or []):
        try:
            if pat and re.search(pat, command, re.I):
                return f"차단 패턴에 걸렸어요: {pat}"
        except re.error:
            continue
    return ""


def _inspect_fabric_code(code: str, rules: dict, worktree: str):
    """HEURISTIC inspection of fabric_exec JS `code`.

    fabric_exec runs arbitrary JS. Calls it makes through fabric.tools.call(...)
    surface as their own tool_call events and are guarded there, but the code can
    ALSO spawn processes or touch the filesystem directly, bypassing per-tool
    guards. So we scan the source text for: direct process spawning
    (child_process/spawn/exec/execSync), fs writes to .env, and the same literal
    patterns guard.py blocks for bash (git push, .env, non-allowlisted curl, and
    any configured extra_deny_patterns). This is best-effort string matching, not
    real JS analysis; obfuscated code can slip past. Keep it conservative."""
    if not code:
        return ""
    if _JS_SPAWN.search(code):
        return "fabric_exec 코드가 프로세스를 직접 실행(child_process/spawn/exec)하려 해요."
    if _JS_FS_ENV_WRITE.search(code):
        return "fabric_exec 코드가 .env 파일에 쓰려고 해요."
    if _ENV_FILE.search(code):
        return "fabric_exec 코드가 .env/설정 파일을 다루려고 해요."
    # reuse the bash literal gates against the code text. Commands in JS sit
    # inside string literals ('curl https://x', `git push`), so blank out quote
    # and backtick characters first; otherwise the shell-boundary regexes (which
    # expect whitespace/|&;( before a command) would miss 'curl ...'.
    unquoted = re.sub(r"[\"'`]", " ", code)
    reason = _bash_denials(unquoted, rules, worktree)
    if reason:
        return "fabric_exec 코드: " + reason
    return ""


# ---- main decision ---------------------------------------------------------
def decide(event: dict, worktree: str = "", net_allowlist=None,
           block_tool_patterns=None, rules=None):
    """Return (decision, reason). decision is 'allow' or 'deny'. Pure + testable.

    `rules` lets callers/tests pass an explicit ruleset; otherwise load_rules()
    is used. `net_allowlist`/`block_tool_patterns` stay for backward-compat and
    are merged into the effective rules."""
    if rules is None:
        rules = load_rules()
    else:
        rules = {**DEFAULT_RULES, **rules}
    if net_allowlist:
        rules = dict(rules)
        rules["allowed_hosts"] = list(rules.get("allowed_hosts") or []) + list(net_allowlist)
    if block_tool_patterns:
        rules = dict(rules)
        rules["deny_tool_patterns"] = list(rules.get("deny_tool_patterns") or []) + list(block_tool_patterns)

    tool = event.get("tool_name", "")
    tool_l = str(tool).lower()
    ti = event.get("tool_input") or {}
    command = ti.get("command", "") if isinstance(ti, dict) else ""

    # 0) custom tool-name denylist
    for pat in (rules.get("deny_tool_patterns") or []):
        try:
            if pat and re.search(pat, str(tool), re.I):
                return "deny", f"도구 '{tool}' 는 차단 목록에 걸려 있어요."
        except re.error:
            continue

    # 0b) fabric_exec code inspection (before generic checks)
    fabric_tools = {str(t).lower() for t in (rules.get("fabric_tools") or ["fabric_exec"])}
    if _enabled(rules, "fabric_exec") and tool_l in fabric_tools:
        code = ""
        if isinstance(ti, dict):
            code = ti.get("code") or ti.get("script") or ti.get("source") or ""
            if not code:
                strs = _strings_in_input(ti)
                code = max(strs, key=len) if strs else ""
        reason = _inspect_fabric_code(str(code), rules, worktree)
        if reason:
            return "deny", reason

    # 1) protected / env files for ANY tool (scan command + every input string)
    haystacks = list(_strings_in_input(ti))
    if command:
        haystacks.append(command)
    globs = rules.get("protected_path_globs") or []
    env_on = _enabled(rules, "env_files")
    for h in haystacks:
        if not isinstance(h, str):
            continue
        if env_on and _ENV_FILE.search(h):
            return "deny", "환경/설정 파일(.env, configurations/env) 접근은 금지돼 있어요."
        for g in globs:
            try:
                if g and (fnmatch.fnmatch(h, g) or fnmatch.fnmatch(h, "*/" + g.lstrip("*/"))):
                    return "deny", f"보호된 경로 접근은 금지돼 있어요: {g}"
            except Exception:
                continue

    # 2) bash-like command gates (bash for pi/codex, Bash for Claude, powershell)
    if tool_l in ("bash", "powershell", "shell", "exec_command") and command:
        reason = _bash_denials(command, rules, worktree)
        if reason:
            return "deny", reason

    return "allow", ""


# ---- logging ---------------------------------------------------------------
def _log(event: dict, decision: str, reason: str) -> None:
    path = os.environ.get("BRIDGE_TOOLS_LOG")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    ti = event.get("tool_input") or {}
    detail = ""
    if isinstance(ti, dict):
        detail = (ti.get("command") or ti.get("code") or ti.get("script") or
                  ti.get("file_path") or ti.get("path") or ti.get("pattern") or "")
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task_id": os.environ.get("BRIDGE_TASK_ID", ""),
        "role": os.environ.get("BRIDGE_ROLE", ""),
        "tool": event.get("tool_name", ""),
        "decision": decision,
        "reason": reason,
        "detail": str(detail)[:800],
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}
    worktree = os.environ.get("BRIDGE_WORKTREE", "")
    decision, reason = decide(event, worktree)
    _log(event, decision, reason)
    if decision == "deny":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }), flush=True)
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
