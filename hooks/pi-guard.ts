/**
 * pi-guard: a pi extension that blocks dangerous tool calls.
 *
 * It forwards every pi `tool_call` event to guard.py (the same rules Claude Code
 * and Codex use) and blocks the call when guard.py denies it (exit code 2).
 * Fail-closed: if guard.py cannot run, the call is blocked.
 *
 * Standalone use (outside the Teams bridge):
 *   pi -e /path/to/hooks/pi-guard.ts
 * guard.py must sit next to this file (or set AGENT_GUARD_PY). Rules come from
 * guard.py's built-in defaults, overridden by $AGENT_GUARD_CONFIG, ./guard.yaml
 * next to guard.py, or ~/.config/agent-guard/guard.yaml (schema:
 * guard.example.yaml). Optional env: AGENT_GUARD_PYTHON (default python3),
 * BRIDGE_TOOLS_LOG (JSONL audit log path), BRIDGE_WORKTREE (rm -rf scope).
 *
 * fabric_exec: its `code` argument is forwarded as-is; guard.py inspects it
 * heuristically (child_process/spawn/exec, fs writes to .env, and the same
 * literal patterns blocked for bash). Calls made via fabric.tools.call(...)
 * arrive here as their own tool_call events and are guarded individually.
 *
 * This is a heuristic deny-list, NOT a sandbox. See README "Security model".
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import * as path from "node:path";

export default function (pi: ExtensionAPI) {
  const here = typeof __dirname === "string" ? __dirname : ".";
  const guard =
    process.env.AGENT_GUARD_PY ||
    process.env.BRIDGE_GUARD_PY ||
    path.join(here, "guard.py");
  const py =
    process.env.AGENT_GUARD_PYTHON || process.env.BRIDGE_PYTHON || "python3";

  pi.on("tool_call", async (event) => {
    const payload = JSON.stringify({
      tool_name: event.toolName,
      tool_input: (event as { input?: unknown }).input ?? {},
    });
    try {
      const r = spawnSync(py, [guard], {
        input: payload,
        encoding: "utf8",
        timeout: 15000,
        env: process.env, // passes AGENT_GUARD_CONFIG / BRIDGE_* through
      });
      if (r.error) {
        return { block: true, reason: `guard failed to run (blocked): ${r.error.message}` };
      }
      if (r.status === 2) {
        const reason = (r.stderr || "").trim() || "blocked by agent-guard";
        return { block: true, reason };
      }
      if (r.status !== 0) {
        return { block: true, reason: `guard exited ${r.status} (blocked, fail-closed)` };
      }
    } catch (e) {
      return { block: true, reason: `guard exception (blocked): ${String(e)}` };
    }
    return undefined;
  });
}
