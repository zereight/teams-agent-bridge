# Migration notes

## → 0.11 (patch11)

Existing `config.yaml` files keep working. Check these:

1. **`project.description` (recommended).** The built-in agent prompt is now
   generic: `You work pstack-first in {project_description}.` If you relied on
   the old hardcoded project text, add it back in **your** `config.yaml`:
   ```yaml
   project:
     description: '<your-repo> (<stack>, <product>)'
   ```
   Without it the prompt says `this project`. If you set `agent.default_prompt`
   yourself, nothing changes (a literal `{project_description}` in it is filled).

2. **AX selectors are atomic now.** A selector you set (`ax.message_list`,
   `ax.compose_box`, …) replaces the built-in one instead of being merged into
   it. If you never set them, the new built-in defaults recognise Korean and
   English Teams. If you set them, your exact selector is used, as before.

3. **Codex gets a guard hook.** `codex exec` now also receives a PreToolUse hook
   (`-c features.hooks=true -c hooks.PreToolUse=…`) pointing at `hooks/guard.py`.
   Needs Codex ≥ 0.154. To opt out: `agent.codex.hooks: false`.

4. **Guard is stricter in two places.** `fabric_exec` code that spawns
   processes or touches `.env` is denied, and `.env`/`configurations/env` is
   checked in every string argument of every tool (not only paths/commands).
   Relax with a guard file (`allow_rules: [fabric_exec]`, etc.); see
   `hooks/guard.example.yaml`.

5. **pi-guard is fail-closed on any non-zero exit** of guard.py (previously only
   exit 2 blocked). If pi suddenly blocks everything, check that `python3` can
   run `hooks/guard.py` (set `AGENT_GUARD_PYTHON` if needed).

6. **Default agent** can now be changed from Teams with `/agent <name>`; that
   choice is stored in `data/state.json` and wins over `agent.type`. Run
   `/agent <your config value>` or delete `default_agent` from state to reset.

7. `config.mac.yaml` is now a generic example. Your live settings belong only in
   `config.yaml`.
