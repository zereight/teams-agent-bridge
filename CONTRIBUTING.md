# Contributing (lite)

Thanks for helping. Keep changes small and focused.

## Before you open a PR

```bash
python -m pytest -q              # all green
python -m py_compile *.py hooks/guard.py
scripts/check_no_secrets.sh      # must pass
```

## Ground rules

- **No personal or company data.** No real names, paths (`/Users/<you>`),
  hostnames, chat titles, or product names in code, defaults, tests, fixtures,
  or docs. Use placeholders (`Alex Kim (나)`, `/Users/me/work/my-project`,
  `example-app`). Anything personal goes in `config.yaml`, which is gitignored.
- **No secrets in files.** Read them from env vars or the macOS Keychain.
- **AX tree dumps contain chat contents.** Never commit `axtree*.txt`. If you
  need a fixture, rebuild the structure in `tests/fixtures/` with fake text.
- **Safety changes need tests.** Anything touching `hooks/guard.py`, the danger
  gate, the time gate, or echo/loop detection needs a test that fails without
  your change.
- **The guard is heuristic.** Don't describe it as a sandbox in docs or code
  comments. New deny rules should be conservative and covered by a test.
- Tests must not depend on wall-clock time or the host OS lock state; inject
  `now`/`start_time` and mock platform calls.

## Adding an agent backend

1. `agent_runner.build_command` (argv) and `_fill_summary` (output parsing).
2. Wire the guard: reuse `hooks/guard.py` (it reads `{tool_name, tool_input}`
   on stdin and emits the PreToolUse deny shape + exit 2).
3. Add the name to `/agent`, `/role add|set`, and the `/<agent>` prefix.
4. Tests for argv, parsing, and that the guard actually blocks.
