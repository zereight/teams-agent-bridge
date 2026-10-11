# teams-agent-bridge

Drive local coding agents — **Claude Code**, **pi**, and **Codex** — from your
Microsoft Teams **"chat with yourself"**, and get phone-sized reports back.

The bridge reads your self-chat through the OS accessibility layer (no Microsoft
Graph, no bot registration), runs each command with a local agent CLI inside an
isolated git worktree, and types a short report back into the same chat.

> 한국어 문서: [README.ko.md](README.ko.md)

## How it works

```
you (phone/desktop) ──► Teams self-chat ──► bridge (AX read, poll every N s)
                                              │  dedupe · time gate · echo guard
                                              │  danger gate ("ㅇㅇ"/"yes" to approve)
                                              ▼
                              agent CLI (claude / pi / codex) in a git worktree
                                              │  guard hook on every tool call
                                              ▼
                     "🤖 ✅ dev · 2m · bridge/dev-1011-1258"  ◄── report typed back
```

Messages starting with your bot prefix (e.g. `🤖 `) are the bridge's own output
and are ignored; everything else is a command.

## Quick start (macOS)

```bash
./install_mac.sh                 # venv + deps; copies config.example.yaml -> config.yaml
cp config.mac.yaml config.yaml   # or start from the macOS example
$EDITOR config.yaml              # set agent.workdir, allowed_roots, project.description
python bridge.py --config config.yaml --dump-tree      # inspect the Teams AX tree
python bridge.py --config config.yaml --show-messages  # confirm messages are read
./run_mac.sh
```

Grant Accessibility permission to your terminal (System Settings → Privacy &
Security → Accessibility). The screen must stay unlocked; the bridge pauses
while locked.

`config.yaml` is **gitignored** — put everything personal there (paths, project
description, Teams title, executable locations). Never put secrets in any file;
use environment variables or the macOS Keychain.

## Commands (type these in your self-chat)

| Command | What it does |
|---|---|
| `<task>` | Run with the default agent (or default role) |
| `/claude <task>` · `/pi <task>` · `/codex <task>` | Run **once** with that agent |
| `/agent` | Show default agent + installed agents and versions |
| `/agent <claude\|pi\|codex>` | Change the default (persisted in `data/state.json`) |
| `@<role> <task>` | Send to a named role |
| `/role add <name> [agent=claude\|pi\|codex] <instructions>` | Add a role |
| `/role set <name> agent=<x>` | Change a role's agent |
| `/role preset pstack [agent]` | Create dev / qa / reviewer roles |
| `/role list` · `show` · `use` · `rm` | Manage roles |
| `/dry <task>` | Plan / read-only run, no writes |
| `/branches` · `/clean` | List / clean bridge worktrees |
| `/status` · `/stop` · `/start` · `/help` | Control |

Dangerous wording (`push`, `deploy`, `delete`, `rm`, `prod`, …) asks for
confirmation first: reply `ㅇㅇ`/`yes` or `취소`/`no`.

## Agents

| Agent | Headless call | Tool-layer guard |
|---|---|---|
| Claude Code | `claude -p --output-format json` | PreToolUse hook via a bridge-generated `--settings` file |
| pi | `pi --mode json` | `-e hooks/pi-guard.ts` extension (`tool_call` → block) |
| Codex | `codex exec` | `--sandbox workspace-write`, network off, **plus** a PreToolUse hook injected with `-c hooks.PreToolUse=…` (Codex ≥ 0.154) |

All three call the same `hooks/guard.py`, so one ruleset governs every agent.
Nothing is written to your global `~/.claude`, `~/.pi`, or `~/.codex` config.

## Security model

Read this before pointing the bridge at anything important.

**What protects you, in layers:**

1. **Input gates** — only your self-chat (title check), dedupe + startup
   baseline + time gate (old history is never replayed), echo/loop guards so the
   bridge can't execute its own output, and confirmation for dangerous wording.
2. **Isolation** — each task runs in its own git worktree on a `bridge/…`
   branch. The bridge never pushes.
3. **Agent sandbox** — Codex runs `workspace-write` with network off.
4. **Tool-layer guard** (`hooks/guard.py`) — denies, per tool call:
   `git push`, `git reset --hard`, `git clean -f`, `sudo`, `rm -rf` outside the
   worktree, `curl/wget/ssh/scp/rsync` to non-allowlisted hosts, any access to
   `.env*` / `configurations/env`, your extra protected globs, and
   `fabric_exec` code that spawns processes or touches `.env`.
5. **Redaction** — common secret shapes are masked before anything is posted.

**What the guard does NOT do:**

- **It is a heuristic deny-list on tool arguments, not a sandbox.** It matches
  strings. Obfuscation (`g""it push`, base64, building commands at runtime, a
  script that runs `git push` internally) can get past it.
- It cannot see side effects and cannot stop a tool it doesn't intercept.
- `fabric_exec` inspection is best-effort text matching, not JS analysis.
- Under Claude `bypassPermissions` or pi (no permission prompts by design), the
  guard is the main thing standing between the model and your shell.

Treat the guard as defense-in-depth. For real isolation use an OS/container
sandbox, a throwaway checkout, credentials scoped to the minimum, and no
production access from the machine the bridge runs on.

## The guard as a standalone component

`hooks/guard.py` (single file, stdlib only; PyYAML optional) and
`hooks/pi-guard.ts` work without the bridge.

**pi:**
```bash
pi -e /path/to/hooks/pi-guard.ts      # guard.py must sit next to it (or set AGENT_GUARD_PY)
```

**Claude Code** (`.claude/settings.json` or a `--settings` file):
```json
{"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
  {"type": "command", "command": "python3 /path/to/hooks/guard.py"}]}]}}
```

**Codex** (≥ 0.154):
```bash
codex exec -c features.hooks=true \
  -c 'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command="python3 /path/to/hooks/guard.py",timeout=15}]}]' "task"
```

### Rules

Built-in defaults live in code. Override them with a YAML/JSON file found at
(first match wins): `$AGENT_GUARD_CONFIG`, `hooks/guard.yaml` (next to
`guard.py`), `~/.config/agent-guard/guard.yaml`. In the bridge, set
`agent.guard_config` to a path. Use `guard.json` if PyYAML isn't installed.

```yaml
allowed_hosts: [api.github.com]          # curl/wget/ssh/scp may reach these
protected_path_globs: ["configurations/env/**", "**/*.pem"]
deny_bash_patterns: ['\bkubectl\s+delete\b']
deny_tool_patterns: ['(?i)web']          # matched against the tool NAME
extra_deny_patterns: []                  # bash commands AND fabric_exec code
allow_rules: []                          # disable built-ins: git_push, git_reset_hard,
                                         # git_clean_force, sudo, rm_rf_outside,
                                         # net_tools, env_files, fabric_exec
fabric_tools: [fabric_exec]
```

Full annotated schema: [`hooks/guard.example.yaml`](hooks/guard.example.yaml).
Every decision is appended to `$BRIDGE_TOOLS_LOG` (JSONL) when set.

## Configuration

- `config.example.yaml` — cross-platform reference with comments.
- `config.mac.yaml` — macOS starting point.
- `project.description` fills `{project_description}` in the default agent
  prompt (`agent.default_prompt`).
- AX selectors for Korean and English Teams are built in. Override
  `ax.message_list` / `ax.compose_box` only if your Teams differs; a selector
  you set **replaces** the built-in one.

## Development

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q
scripts/check_no_secrets.sh      # fails if personal strings leak into the tree
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
