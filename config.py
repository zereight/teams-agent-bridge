"""Config loading: YAML file merged over built-in defaults.

Secrets are NEVER read from this file. Only environment variable *names* are
configured here (e.g. vision.api_key_env: ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any, Dict

import yaml

# Prepended to every role and plain task so agents work "pstack-first".
# pstack = poteto's engineering-workflow plugin (entry: /pstack:poteto-mode).
# {project_description} is filled from config project.description at runtime.
DEFAULT_PSTACK_PROMPT = (
    "You work pstack-first in {project_description}. "
    "Start every task with /pstack:poteto-mode. Reach for a specific skill only "
    "when the task calls for it: architect/how/why for unfamiliar or cross-module "
    "work, interrogate when the ask is ambiguous, tdd for bugs, blast-radius before "
    "touching shared code, reflect after a failed attempt. If the /pstack commands "
    "are not installed, follow the same principles manually. "
    "Work only in the worktree you were given. Never push, never deploy, never read "
    "configurations/env/.env.*. Prove claims with commands you actually ran; say "
    "'not verified' otherwise. Smallest change that works; no drive-by refactors. "
    "Answer in Korean. End with the report format. To hand off, put exactly one "
    "final line `@<role> <one-sentence task>`; only dev->qa, dev->reviewer, qa->dev "
    "are allowed."
)

# Seeded by `/role preset pstack` (also shippable as data/roles.preset.json).
PSTACK_PRESET_ROLES = [
    {"name": "dev", "agent": "claude", "instructions": (
        "Implement the task on the given bridge/ branch. Read before writing: run "
        "/pstack:poteto-mode, then how/architect if the change crosses modules. "
        "Before finishing run the repo's own typecheck/lint/jest for touched "
        "packages (note any that can't run). If UI behavior changed, end with @qa "
        "naming the screen and expected behavior. Otherwise end with @reviewer.")},
    {"name": "qa", "agent": "claude", "instructions": (
        "Verify, don't fix. Use the repo's verify skill if present. Write the "
        "smallest check that proves the claimed behavior, plus one negative check "
        "that must fail. Report pass/fail/unverifiable per claim with evidence "
        "paths. On a real failure end with @dev and the failing step; never edit "
        "app code.")},
    {"name": "reviewer", "agent": "codex", "instructions": (
        "Review the diff on the given branch only. Findings ranked by severity with "
        "file:line and a concrete fix; say 'no findings' plainly when clean. No "
        "style nits unless they hide a bug. Do not edit files.")},
]

DEFAULTS: Dict[str, Any] = {
    # Describe YOUR project in config.yaml; it fills {project_description} in the
    # default agent prompt. Keep it generic here (no company/product names).
    "project": {"name": "", "description": "this project"},
    "backend": "ax",                  # ax (macOS) | uia (Windows) | vision
    "poll_interval_sec": 60,
    "data_dir": "./data",             # state.json, logs/, outputs/
    "dry_run": False,                 # True: never run agents, never type into Teams
    "self_chat": {
        # Regex that MUST match the window title (+ chat header text if a header
        # selector is configured) before anything is read or sent.
        "title_regex": r"\(You\)|\(나\)",
    },
    "bot_prefix": "(b) ",
    # Any message whose text starts with one of these is bot output and is
    # never treated as a command. Add what Teams actually renders if it
    # converts "(b)" into an emoji.
    "bot_prefixes_detect": ["(b)", "[b]", "🍺"],
    "reply": {
        "max_chars": 1500,
        "ack_on_start": True,
        "announce_start": True,
        "report_body_chars": 1200,        # final report: agent body clip (chars)
        "report_body_lines": 20,          # final report: agent body clip (lines)
        "resync_min_interval_sec": 600,   # at most one "위치를 다시 맞췄어요" notice per 10 min
    },
    "safety": {
        "dangerous_patterns": [
            r"\brm\b", r"\brmdir\b", r"\bdel\b", r"\bdelete\b", r"\bremove-item\b",
            r"\bdrop\b", r"\btruncate\b", r"\bpush\b", r"\bdeploy\b", r"\brelease\b",
            r"\bpublish\b", r"\bmerge\b", r"\brebase\b", r"\breset\s+--hard\b",
            r"--force\b", r"\bforce[- ]push\b", r"\bprod\b", r"\bproduction\b",
            r"\bmigrat",
            "배포", "삭제", "지워", "지우", "제거", "푸시", "푸쉬", "머지", "병합",
            "초기화", "운영", "프로덕션", "릴리스", "릴리즈", "마이그레이션",
        ],
        "approval_words": ["ㅇㅇ", "ㅇㅋ", "yes", "y", "ok", "실행", "승인", "진행"],
        "cancel_words": ["취소", "ㄴㄴ", "no", "n", "cancel", "아니", "중지"],
        "approval_timeout_sec": 300,   # pending approvals expire after 5 min
        "ts_slack_sec": 60,            # time gate: ignore messages older than start-this
        "max_commands_per_hour": 20,
        "repeat_limit": 3,            # same command N times ...
        "repeat_window_sec": 600,     # ... within this window -> auto pause
        "max_queue": 5,
        "loop_guard": {
            # Defence against a self-echo loop (bridge re-reading its own replies).
            # A run is "suspicious" ONLY if its triggering message arrived within
            # near_send_sec of a bot send AND overlaps a recent bot message (so a
            # fast human never trips it; a mangled echo does). Handoff tasks are
            # exempt from the suspicious rule (they have hop/chain limits).
            "near_send_sec": 20,       # (a) command within this long after a bot send
            "overlap_substr_len": 12,  # (b) a shared normalized substring this long -> overlap
            "overlap_token_ratio": 0.30,  # (b) OR this much token overlap -> overlap
            "window_min": 5,           # look-back for the suspicious counter
            "max_near_runs": 3,        # >= this many suspicious agent runs in window_min -> pause
            "global_window_min": 10,   # look-back for the absolute cap
            "max_runs_per_global": 12, # > this many agent runs in global_window_min -> pause
            "resync_window_min": 5,    # look-back for bot-only resync notices
            "max_resync_notices": 3,   # > this many bot-only resyncs in window -> stop notifying
        },
    },
    "agent": {
        "type": "claude",             # claude | codex | pi | custom (active backend)
        "default": "claude",          # fallback agent when 'type' is unset
        "workdir": "",                # REQUIRED: project directory the agent may work in
        "allowed_roots": [],          # workdir must be inside one of these (if non-empty)
        "timeout_sec": 1800,
        "prompt_via_stdin": True,     # safer on Windows (.cmd shims + cmd.exe quoting)
        "default_prompt": DEFAULT_PSTACK_PROMPT,  # prepended to every role + plain task
        "network_allowlist": [],      # hosts curl/wget/ssh/scp may reach (guard hook); empty = none
        "hook": {"enabled": True},    # install the PreToolUse guard (claude --settings / codex -c)
        "guard_config": "",           # optional path to a guard.yaml/json ruleset (AGENT_GUARD_CONFIG)
        "worktrees": {
            "enabled": True,
            "base_dir": "~/teams-agent-bridge/worktrees",
            "clean_older_than_days": 7,
        },
        "claude": {
            "executable": "claude",
            "permission_mode": "acceptEdits",
            "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
            "disallowed_tools": [],
            "max_turns": 40,
            "append_system_prompt": "",
            "extra_args": [],
        },
        "codex": {
            "executable": "codex",
            "sandbox": "workspace-write",
            "network_access": False,  # -c sandbox_workspace_write.network_access=false
            "hooks": True,            # inject a PreToolUse guard hook via -c (codex 0.154+)
            "skip_git_repo_check": False,
            "ephemeral": False,
            "extra_args": [],
        },
        "pi": {   # Mario Zechner's pi coding agent (@earendil-works/pi-coding-agent)
            "executable": "pi",       # ABSOLUTE path recommended (nvm: ~/.nvm/.../bin/pi)
            "mode": "json",           # json (parsed) | print (-p plain text)
            "provider": "",           # empty = inherit ~/.pi/agent/settings.json defaultProvider
            "model": "",              # empty = inherit defaultModel (do NOT override by default)
            "thinking": "",           # empty = inherit defaultThinkingLevel
            "tools": [],              # empty = pi defaults (read,bash,edit,write) + user config
            "exclude_tools": [],      # e.g. ["web_*"] to drop pi-web-access network tools
            "skills": [],             # extra --skill paths (e.g. pstack-claude skills dir)
            "block_tool_patterns": [],# regexes of custom tool NAMES the guard must deny
            "allow_unguarded": False, # refuse write-capable pi runs when the guard is off
            "append_system_prompt": "",
            "extra_args": [],
        },
        "custom": {
            # argv list; "{prompt}" and "{workdir}" are substituted. Used by tests.
            "command": [],
        },
    },
    "ax": {   # macOS Accessibility backend
        "bundle_id": "com.microsoft.teams2",   # new Teams; classic Teams = com.microsoft.teams
        "enable_manual_accessibility": True,   # ask Electron/WebView to build its web AX tree
        "activate_for_read": False,            # bring Teams to front only when typing
        "activate_delay_sec": 0.6,
        "messaging_timeout_sec": 2.0,
        "window": None,                        # selector; None = focused/main window
        "chat_header": None,                   # selector for the chat title element
        # Built-in selectors match BOTH Korean and English Teams UI. Override in
        # config.yaml if your Teams build/locale differs (see --dump-tree).
        "message_list": {"role": "AXGroup",
                         "description_re": r"^(메시지 목록|Message list|Messages|Chat messages)$"},
        "message_strategy": "timegroup",       # timegroup (new Teams) | generic
        # --- timegroup strategy (new Teams message rows) ---
        "app_group_subrole": "AXApplicationGroup",   # per-message anchor
        "time_group_subrole": "AXTimeGroup",         # holds the full timestamp in its AXDescription
        "status_words": ["편집됨", "전송됨", "전송 안 됨",       # Korean
                         "Edited", "Sent", "Not sent", "Failed to send"],  # English
        "body_join": "",                       # separator joining body AXStaticText tokens
        # --- generic strategy (fallback) ---
        "message_item": {"role": "AXGroup"},   # selector for one message row
        "item_text_source": "children",        # self | children | all
        "message_regex": None,
        "compose_box": {"role": "AXTextArea",
                        "description_re": r"(메시지\S*\s*입력|[Tt]ype a (new )?message|Reply)"},
        "click_compose_box": False,            # also click it (fallback if AXFocused fails)
        "max_messages": 30,
        "max_depth": 60,
        "max_nodes": 20000,
        "paste_delay_sec": 0.4,
        "restore_clipboard": True,
        "clear_compose_before_paste": True,
    },
    "uia": {
        "window": {"title_re": r".*Microsoft Teams.*", "control_type": "Window"},
        "chat_header": None,          # selector dict, optional
        "message_list": None,         # selector dict, optional (None = whole window)
        "message_item": {"control_type": "ListItem"},
        "item_text_source": "name",   # name | children
        "message_regex": None,        # regex with (?P<text>...) and optional (?P<ts>...)
        "compose_box": {"control_type": "Edit", "title_re": r"(?i).*(type a message|메시지 입력|메시지를 입력).*"},
        "max_messages": 30,
        "paste_delay_sec": 0.4,
        "restore_clipboard": True,
        "clear_compose_before_paste": True,
    },
    "vision": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_keychain_service": "",   # optional macOS Keychain service name (security find-generic-password -s)
        "api_keychain_account": "",
        "model": "",                  # REQUIRED for vision backend; set to a model your company allows
        "max_tokens": 2000,
        "max_image_width": 1400,
        "bundle_id": "com.microsoft.teams2",   # macOS: used to find/activate the window
        "window_title_regex": r".*Microsoft Teams.*",  # Windows fallback
        "compose_box_relative": [0.55, 0.93],  # fallback click point (fraction of window)
        "cache_sec": 5,
        "paste_delay_sec": 0.4,
    },
    "roles": {
        "enabled": True,
        "max_hops": 4,                 # handoffs allowed in one chain
        "chain_timeout_sec": 3600,     # whole @role -> @role chain budget
        "progress_after_sec": 120,     # (legacy) kept for config compat
        "progress_interval_sec": 120,  # (legacy) kept for config compat
        "silence_ping_sec": 600,       # one progress ping after this long with no phase change
        "handoff_summary_chars": 400,  # length of sender summary passed to next role
    },
    "handoff": {
        # Only these ordered role pairs may hand off. Anything else is reported
        # and the chain stops. A pair that bounces twice also stops the chain.
        "allowlist": ["dev->qa", "dev->reviewer", "qa->dev"],
    },
    "log": {"max_bytes": 5_000_000, "backup_count": 10},
}


# AX/UIA selector dicts are ATOMIC: a user's selector REPLACES the built-in one
# instead of being deep-merged into it. Otherwise a built-in key (e.g. the
# bilingual description_re) would leak into a user selector that matches by a
# different key (e.g. identifier) and silently break matching.
_ATOMIC_KEYS = {"window", "chat_header", "message_list", "message_item", "compose_box"}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in _ATOMIC_KEYS:
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULTS, data)
    base_dir = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    if not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.normpath(os.path.join(base_dir, cfg["data_dir"]))
    _reject_inline_secrets(cfg)
    return cfg


_SECRET_KEYS = ("api_key", "token", "password", "secret")


def _reject_inline_secrets(node: Any, path: str = "") -> None:
    """Fail loudly if someone pastes a secret into the YAML file."""
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if (isinstance(v, str) and v and not kl.endswith("_env")
                    and any(s in kl for s in _SECRET_KEYS)):
                raise ValueError(
                    f"Config key '{path}{k}' looks like a secret. Put secrets in "
                    f"environment variables and reference them with '*_env' keys."
                )
            _reject_inline_secrets(v, f"{path}{k}.")
