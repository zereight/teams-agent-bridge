"""Teams self-chat <-> local coding agent bridge (computer-use edition).

Run:  python bridge.py --config config.yaml
Debug UI tree:      python bridge.py --config config.yaml --dump-tree
Show parsed msgs:   python bridge.py --config config.yaml --show-messages
One cycle only:     python bridge.py --config config.yaml --once
"""
from __future__ import annotations

import argparse
from datetime import datetime
import logging
import os
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import handoff as handoffmod
import report as reportmod
import roles as rolemod
import safety
import worktrees as wtmod
from agent_runner import AgentResult, AgentRunner, RunSpec
from audit import Audit
from backend_base import BackendError, ChatBackend, ChatGuardError, ChatMessage
from config import load_config
from platform_utils import enable_dpi_awareness, is_workstation_locked
from roles import Role, RoleStore
import timegate
from state import StateStore, normalize_text, norm_nospace

HELP_TEXT = (
    "명령어 안내\n"
    "- 그냥 할 일을 쓰면 에이전트가 실행해요 (예: 로그인 테스트 돌리고 결과 알려줘)\n"
    "- @역할이름 할일 : 특정 역할(봇)에게 작업 지시 (예: @dev 로그인 버그 고쳐줘)\n"
    "- /role add <이름> [agent=claude|codex|pi] <지침> : 역할 추가\n"
    "- /role list : 역할 목록 / /role show <이름> : 역할 상세\n"
    "- /role use <이름> : 기본 역할 지정(그냥 쓴 메시지가 이 역할로 감)\n"
    "- /role rm <이름> : 역할 삭제(확인 필요)\n"
    "- /role set <이름> agent=<claude|pi|codex> : 역할의 에이전트 변경\n"
    "- /role preset pstack [agent] : dev/qa/reviewer 역할을 한 번에 생성(에이전트 지정 가능)\n"
    "- /claude <할 일> · /pi <할 일> · /codex <할 일> : 이번 한 번만 그 에이전트로 실행\n"
    "- /agent : 기본 에이전트와 설치 상태/버전 보기  /agent <claude|pi|codex> : 기본 에이전트 변경\n"
    "- /dry <할 일> : 쓰기 없이 계획/읽기 전용으로 실행\n"
    "- /branches : 브리지가 만든 워크트리 목록  /clean : 병합/오래된 워크트리 정리(확인)\n"
    "- /status : 지금 상태  /stop : 중단(프로세스 그룹)+일시정지  /start : 재개  /help : 안내\n"
    "역할끼리 넘기기: 결과 마지막 줄에 \"@다른역할 메시지\"를 적으면 그 역할에게 이어서 작업을 보내요 "
    "(허용된 쌍: dev→qa, dev→reviewer, qa→dev). 작업마다 bridge/ 브랜치 워크트리에서 돌고, 브리지는 절대 푸시하지 않아요.\n"
    "위험한 단어(삭제, 푸시, 배포 등)와 역할 삭제/정리는 먼저 물어봐요. \"ㅇㅇ\" 또는 \"취소\"로 답해 주세요."
)

AT_RE = re.compile(r"^@([A-Za-z0-9_\-가-힣]{1,32})\s+(.*)$", re.S)


def _shares_long_substring(a: str, b: str, n: int) -> bool:
    """True if `a` and `b` share any common substring of length >= n (both are
    already whitespace-removed normalisations)."""
    if len(a) < n or len(b) < n:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    for i in range(len(short) - n + 1):
        if short[i:i + n] in long:
            return True
    return False


@dataclass
class Task:
    prompt: str
    role: str = ""                 # "" => global/default agent
    meta: Dict[str, Any] = field(default_factory=dict)


class Bridge:
    def __init__(self, cfg: Dict[str, Any], backend: ChatBackend, runner: AgentRunner,
                 state: StateStore, audit: Audit, clock=time.time,
                 lock_check=is_workstation_locked, roles: Optional[RoleStore] = None,
                 start_time: Optional[datetime] = None, now_dt=None):
        self.cfg = cfg
        self.backend = backend
        self.runner = runner
        self.state = state
        self.audit = audit
        self.clock = clock
        self.lock_check = lock_check
        s = cfg["safety"]
        self.patterns = safety.compile_patterns(s["dangerous_patterns"])
        self.limiter = safety.RateLimiter(s["max_commands_per_hour"], s["repeat_limit"],
                                          s["repeat_window_sec"], clock=clock)
        self.queue: Deque[Task] = deque()
        self.pending: Optional[Dict[str, Any]] = None  # {"kind","text","role","at",...}
        self.prefix = cfg["bot_prefix"]
        self.dry_run = bool(cfg.get("dry_run"))
        self.rcfg = cfg.get("roles", {})
        self.roles = roles if roles is not None else RoleStore(
            os.path.join(cfg["data_dir"], "roles.json"))
        self._prog_task_id: Optional[str] = None
        self._prog_last_elapsed: float = 0.0
        self.lg = s.get("loop_guard", {})
        self._last_send_at: float = 0.0           # when the bridge last sent a reply
        self._run_log: Deque[tuple] = deque()     # (started_at, suspicious) of agent runs
        self.resync_min_interval = float(cfg["reply"].get("resync_min_interval_sec", 600))
        self.resync_window = float(self.lg.get("resync_window_min", 5)) * 60
        self.resync_max_notices = int(self.lg.get("max_resync_notices", 3))
        self._last_resync_at: float = 0.0
        self._resync_events: Deque[float] = deque()
        self._recent_bot: Deque[dict] = deque(maxlen=30)   # recent sent bot msgs (for overlap)
        self.now_dt = now_dt or (lambda: datetime.now())
        self.start_time: datetime = start_time or self.now_dt()
        self.ts_slack_sec = float(s.get("ts_slack_sec", 60))
        self._baseline_done = False
        acfg = cfg.get("agent", {})
        self.wtcfg = acfg.get("worktrees", {}) or {}
        self.hcfg = cfg.get("handoff", {}) or {}
        desc = (cfg.get("project", {}) or {}).get("description") or "this project"
        self.default_prompt = (acfg.get("default_prompt", "") or "").replace(
            "{project_description}", desc)
        # active default agent: a /agent choice (persisted in state) wins over config
        self.agent_name = (state.default_agent or acfg.get("type")
                           or acfg.get("default") or "claude")
        self._pi_version_cache: Optional[str] = None
        self._repo_root_cache: Optional[str] = None
        self._phase_task_id: Optional[str] = None
        self._silence_posted = False
        backend.attach_state(state)

    # ------------------------------------------------------------ output
    def reply(self, body: str, role: str = "") -> None:
        tag = f"[{role}] " if role else ""
        text = self.prefix + tag + safety.redact(body)
        self.audit.event("reply", text=text, role=role, dry_run=self.dry_run)
        self.state.remember_sent(text)   # remember BEFORE sending (never loop)
        self._recent_bot.append({"ns": norm_nospace(text),
                                 "tok": set(normalize_text(text).split())})
        self._last_send_at = self.clock()
        if self.dry_run:
            return
        self.backend.send_message(text)

    def _announce_startup(self) -> None:
        if not self.cfg["reply"].get("announce_start"):
            return
        if self.state.paused:
            reason = self.state.pause_reason or "알 수 없음"
            self.reply(f"브리지가 켜졌어요. 현재 일시정지 상태예요 (이유: {reason}). /start 로 다시 켜세요.")
        else:
            self.reply("브리지가 켜졌어요. 이전 메시지는 실행하지 않아요. /help 로 사용법을 볼 수 있어요.")

    # ------------------------------------------------------- worktrees / env
    def _repo_root(self) -> Optional[str]:
        if self._repo_root_cache is None:
            self._repo_root_cache = wtmod.repo_root(self.runner.workdir) or ""
        return self._repo_root_cache or None

    def _wt_base_dir(self) -> str:
        return os.path.expanduser(self.wtcfg.get("base_dir", "~/teams-agent-bridge/worktrees"))

    def _ensure_worktree(self, task: "Task") -> None:
        """Create a per-chain git worktree the first time a chain needs one.
        Handoffs reuse the chain's worktree (meta already carries workdir/branch)."""
        if not self.wtcfg.get("enabled", True):
            return
        meta = task.meta
        if meta.get("workdir"):
            return
        repo = self._repo_root()
        if not repo:
            return
        try:
            w = wtmod.create(repo, self._wt_base_dir(), task.role or "task", now=self.clock())
        except Exception as e:
            self.audit.event("worktree_error", error=str(e))
            return
        meta["workdir"] = w.path
        meta["branch"] = w.branch
        meta["repo"] = repo
        self.audit.event("worktree_create", branch=w.branch, path=w.path, role=task.role)

    def _pstack_installed(self) -> bool:
        p = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = f.read()
            return "pstack" in data
        except OSError:
            return False

    # ------------------------------------------------------------ cycle
    def cycle(self) -> str:
        if self.lock_check():
            self.audit.event("skip_locked")
            return "locked"
        try:
            title = self.backend.focus_self_chat()
            # HARD startup baseline: on the first cycle of THIS process, snapshot
            # everything visible as "seen at startup" and run nothing - no matter
            # what the persisted state or fp_version says. This is what stops
            # yesterday's messages from being replayed after a restart.
            if not self._baseline_done:
                visible = self.backend.read_visible_messages()
                n = self.state.establish_baseline(visible)
                self._baseline_done = True
                self.audit.event("startup_baseline", title=title, n=n)
                self._announce_startup()
                self.drain_results()
                return "ok"
            new = self.backend.read_new_messages()
        except ChatGuardError as e:
            self.audit.event("guard_abort", reason=str(e))
            return "guard"
        except BackendError as e:
            self.audit.event("backend_error", error=str(e))
            return "backend_error"

        self.maybe_progress()
        if new is None:
            # fp_version migration mid-stream (rare): mark seen, run nothing.
            ev = self.state.last_event
            self.audit.event("dedupe_" + ev, title=title)
            if ev == "init" and self.cfg["reply"].get("announce_start"):
                self._announce_startup()
        else:
            ev = self.state.last_event
            self.audit.event("dedupe_" + ev, title=title, n=len(new))
            for m in new:
                self.handle(m)
            if ev == "resync":
                self._maybe_resync_notice(new)
        self.drain_results()
        return "ok"

    def _run_is_suspicious(self, text: str, now: float) -> bool:
        """A triggering message is suspicious only if it arrived <= near_send_sec
        after a bot send AND overlaps a recent bot message (long shared substring
        or significant token overlap). This catches mangled self-echoes while
        leaving a fast-typing human alone."""
        if not (self._last_send_at > 0
                and now - self._last_send_at <= float(self.lg.get("near_send_sec", 20))):
            return False
        nt = norm_nospace(text)
        toks = set(normalize_text(text).split())
        n = int(self.lg.get("overlap_substr_len", 12))
        ratio = float(self.lg.get("overlap_token_ratio", 0.30))
        for rec in self._recent_bot:
            if _shares_long_substring(nt, rec["ns"], n):
                return True
            if rec["tok"] and toks:
                inter = len(toks & rec["tok"])
                if inter / min(len(toks), len(rec["tok"])) >= ratio:
                    return True
        return False

    def _looks_like_bot(self, text: str) -> bool:
        t = normalize_text(text)
        return (safety.is_bot_message(t, self.cfg["bot_prefixes_detect"], self.state.recent_sent)
                or self.state.is_echo(t))

    def _maybe_resync_notice(self, new) -> None:
        """Post the "I lost my place" notice at most once per 10 min, never when
        the only change is our own bot messages, and stop entirely after too many
        bot-only resyncs in a row (loop guard for the notice itself)."""
        now = self.clock()
        if any(not self._looks_like_bot(m.text) for m in new):
            # a real user message surfaced this poll: it was handled, so there is
            # nothing to apologise for. Reset the notice loop guard.
            self._resync_events.clear()
            return
        # never apologise when the only thing that moved the view is our OWN
        # output: if the newest visible message is a bot/echo message, stay quiet
        # (this is the self-perpetuating case where our last reply shifts the list)
        try:
            visible = self.backend.read_visible_messages()
        except Exception:
            visible = []
        if visible and self._looks_like_bot(visible[-1].text):
            self.audit.event("resync_notice_skip_bot_tail")
            return
        self._resync_events.append(now)
        while self._resync_events and now - self._resync_events[0] > self.resync_window:
            self._resync_events.popleft()
        if len(self._resync_events) > self.resync_max_notices:
            self.audit.event("resync_notice_suppressed", count=len(self._resync_events))
            return
        if now - self._last_resync_at < self.resync_min_interval:
            self.audit.event("resync_notice_throttled")
            return
        self._last_resync_at = now
        self.audit.event("resync_notice")
        self.reply("화면이 바뀌어서 메시지 위치를 다시 맞췄어요. 방금 보낸 명령이 실행 안 됐으면 다시 보내 주세요.")

    # ------------------------------------------------------------ progress
    def maybe_progress(self) -> None:
        """Phase-change progress: 'started' and 'handed off' are posted elsewhere.
        Here we post ONE ping after `silence_ping_sec` of no phase change, so a
        long-running task shows a single heartbeat instead of fixed 120s spam."""
        if self.dry_run:
            return
        cur = self.runner.current
        if cur is None:
            self._phase_task_id = None
            self._silence_posted = False
            return
        if self._phase_task_id != cur.task_id:
            self._phase_task_id = cur.task_id
            self._silence_posted = False
        if self._silence_posted:
            return
        silence = float(self.rcfg.get("silence_ping_sec", 600))
        elapsed = self.clock() - cur.started_at
        if elapsed < silence:
            return
        self._silence_posted = True
        minutes = int(elapsed // 60)
        step = self.runner.step
        body = f"진행 중 ({minutes}분째, 조용함)" + (f" - {step}" if step else "")
        self.reply(body, role=cur.role)

    def _too_old(self, m: ChatMessage) -> bool:
        """HARD time gate against history replay. A message runs only if it is not
        older than start_time - ts_slack_sec. Teams timestamps are parsed; an
        unparseable label is treated as old UNLESS the message appeared after the
        startup baseline (i.e. it was not on screen at start)."""
        ts = timegate.parse_teams_timestamp(m.timestamp, self.now_dt())
        if ts is not None:
            from datetime import timedelta
            return ts < (self.start_time - timedelta(seconds=self.ts_slack_sec))
        # unparseable: old if it was part of the startup snapshot, else allow
        return bool(m.sfp) and m.sfp in self.state.baseline

    # ------------------------------------------------------------ handling
    def handle(self, m: ChatMessage) -> None:
        text = normalize_text(m.text)
        if not text:
            return
        if (safety.is_bot_message(text, self.cfg["bot_prefixes_detect"], self.state.recent_sent)
                or self.state.is_echo(text)):
            self.audit.event("ignore_bot", text=text[:200])
            return
        if self._too_old(m):
            self.audit.event("ignore_old", text=text[:200], ts=m.timestamp)
            return
        self.audit.event("command_in", text=text, ts=m.timestamp, fp=m.occ_fp)

        builtin = safety.parse_builtin(text)
        if builtin:
            self.handle_builtin(builtin)
            return
        if text.lower().startswith("/role"):
            self.handle_role_cmd(text)
            return
        low = text.lower()
        if low == "/branches":
            self.handle_branches()
            return
        if low == "/clean":
            self.handle_clean()
            return
        if low.startswith("/dry"):
            self.handle_dry(text[len("/dry"):].strip())
            return
        if low == "/agent" or low.startswith("/agent "):
            self.handle_agent(text[len("/agent"):].strip())
            return
        for ag in ("claude", "pi", "codex"):
            if low == "/" + ag or low.startswith("/" + ag + " "):
                self.handle_agent_run(ag, text[len("/" + ag) + 1:].strip())
                return
        if self.state.paused:
            self.audit.event("decision", action="ignored_paused", text=text)
            self.reply("일시정지 중이라 실행하지 않았어요. /start 로 다시 켜 주세요.")
            return

        s = self.cfg["safety"]
        if self.pending is not None:
            pending, self.pending = self.pending, None
            if self.clock() - pending["at"] > s["approval_timeout_sec"]:
                self.audit.event("decision", action="approval_expired", text=pending.get("text"))
                self.reply("승인 대기 시간이 지나서 이전 작업은 취소했어요.")
                # fall through: treat this message as a new command
            elif safety.is_approval(text, s["approval_words"]):
                self.audit.event("decision", action="approved", pending_kind=pending["kind"],
                                 text=pending.get("text"))
                self.run_pending(pending)
                return
            else:
                self.audit.event("decision", action="approval_declined", pending_kind=pending["kind"],
                                 text=pending.get("text"), reply=text)
                self.reply("취소했어요.")
                if safety.is_cancel(text, s["cancel_words"]):
                    return
                # anything else: cancel AND treat as a new command

        if safety.is_approval(text, s["approval_words"]) or safety.is_cancel(text, s["cancel_words"]):
            self.audit.event("decision", action="stray_approval", text=text)
            self.reply("승인하거나 취소할 작업이 없어요.")
            return

        # resolve @role target (explicit) or default role (plain message)
        role, cmd, explicit = self.resolve_target(text)
        if explicit and role and not self.roles.exists(role):
            self.audit.event("decision", action="unknown_role", role=role)
            self.reply(f"'{role}' 역할을 몰라요. /role list 로 확인하거나 /role add 로 추가해 주세요.")
            return
        if not cmd:
            return

        ok, why = self.limiter.check(cmd)
        if not ok:
            self.state.paused = True
            self.state.pause_reason = f"안전장치({why})"
            self.audit.event("decision", action="circuit_breaker_pause", reason=why)
            self.reply(f"안전장치: {why}. 일시정지했어요. 확인 후 /start 로 다시 켜 주세요.")
            return

        hits = safety.dangerous_matches(cmd, self.patterns)
        if hits:
            self.pending = {"kind": "danger", "text": cmd, "role": role, "at": self.clock()}
            self.audit.event("decision", action="ask_approval", text=cmd, matched=hits)
            self.reply(f"위험할 수 있는 작업이에요({', '.join(sorted(set(hits)))}). "
                       f"실행할까요? \"ㅇㅇ\" 또는 \"취소\"\n> {cmd[:200]}")
            return
        self.submit(cmd, role=role)

    def resolve_target(self, text: str):
        m = AT_RE.match(text)
        if m:
            return m.group(1), m.group(2).strip(), True
        return (self.roles.default_role or ""), text, False

    def run_pending(self, pending: Dict[str, Any]) -> None:
        kind = pending["kind"]
        if kind == "role_rm":
            name = pending["role"]
            if self.roles.remove(name):
                self.reply(f"'{name}' 역할을 삭제했어요.")
            else:
                self.reply(f"'{name}' 역할이 이미 없어요.")
            return
        if kind == "clean":
            self._do_clean(pending.get("candidates") or [])
            return
        if kind == "handoff":
            self.submit(pending["text"], role=pending.get("role", ""), approved=True,
                        meta=pending.get("meta"))
            return
        # danger
        self.submit(pending["text"], role=pending.get("role", ""), approved=True,
                    meta=pending.get("meta"))

    # ------------------------------------------------------------ roles cmds
    def handle_role_cmd(self, text: str) -> None:
        if not self.rcfg.get("enabled", True):
            self.reply("역할 기능이 꺼져 있어요.")
            return
        rest = text[len("/role"):].strip()
        parts = rest.split(None, 1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        self.audit.event("role_cmd", sub=sub, arg=arg[:200])
        if sub in ("", "help"):
            self.reply("역할 명령: /role add <이름> [agent=claude|codex|pi] <지침> | list | "
                       "show <이름> | use <이름> | rm <이름> | set <이름> agent=<x> | "
                       "preset pstack [agent]")
        elif sub == "add":
            try:
                name, agent, instr = rolemod.parse_role_add(arg)
            except ValueError as e:
                self.reply(f"추가 실패: {e}")
                return
            allowed = rolemod.sanitize_allowed(
                None, self.cfg["agent"]["claude"].get("disallowed_tools", []))
            role = Role(name=name, instructions=instr, agent=agent, allowed_tools=allowed,
                        created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            existed = self.roles.exists(name)
            self.roles.add(role)
            self.reply(f"역할 '{name}' ({agent}) {'수정' if existed else '추가'}했어요.")
        elif sub == "set":
            m = re.match(r"(\S+)\s+agent\s*=\s*(claude|codex|pi)\s*$", arg, re.I)
            if not m:
                self.reply("사용법: /role set <이름> agent=<claude|pi|codex>")
                return
            name, newagent = m.group(1), m.group(2).lower()
            r = self.roles.get(name)
            if r is None:
                self.reply(f"'{name}' 역할을 몰라요. /role list 로 확인하세요.")
                return
            if not self._agent_available(newagent):
                self.reply(f"'{newagent}' 를 쓸 수 없어요 (설치/경로를 확인하세요).")
                return
            old_agent = r.agent
            r.agent = newagent
            if old_agent != newagent:
                r.session_id = None          # sessions don't carry across agents
            self.roles.add(r)
            self.audit.event("role_set_agent", role=name, agent=newagent, prev=old_agent)
            self.reply(f"역할 '{name}' 의 에이전트를 {old_agent} → {newagent} 로 바꿨어요.")
        elif sub == "preset":
            toks = arg.split()
            which = (toks[0].lower() if toks else "")
            if which not in ("", "pstack"):
                self.reply("알 수 없는 프리셋이에요. 지금은 'pstack' 만 있어요.")
                return
            forced = toks[1].lower() if len(toks) > 1 else ""
            if forced and forced not in ("claude", "pi", "codex"):
                self.reply("프리셋 에이전트는 claude|pi|codex 중 하나예요: /role preset pstack [agent]")
                return
            if forced and not self._agent_available(forced):
                self.reply(f"'{forced}' 를 쓸 수 없어요 (설치/경로를 확인하세요).")
                return
            import config as _cfg
            created = []
            base_agent = self.agent_name if self._agent_available(self.agent_name) else "claude"
            for d in _cfg.PSTACK_PRESET_ROLES:
                agent = d["agent"]
                note = ""
                if forced:
                    agent = forced                 # /role preset pstack <agent>: all roles
                # dev/qa (preset agent 'claude') follow the bridge's active agent
                elif agent == "claude" and base_agent != "claude":
                    agent = base_agent
                if not self._agent_available(agent):
                    agent = base_agent if self._agent_available(base_agent) else "claude"
                    note = f" ({d['agent']} 미사용 → {agent})"
                allowed = rolemod.sanitize_allowed(
                    None, self.cfg["agent"]["claude"].get("disallowed_tools", []))
                self.roles.add(Role(name=d["name"], instructions=d["instructions"],
                                    agent=agent, allowed_tools=allowed,
                                    created_at=time.strftime("%Y-%m-%d %H:%M:%S")))
                created.append(d["name"] + note)
            self.audit.event("role_preset", which="pstack", created=created)
            self.reply("pstack 프리셋 역할을 만들었어요: " + ", ".join(created))
        elif sub == "list":
            if not self.roles.names():
                self.reply("등록된 역할이 없어요. /role add 로 추가해 보세요.")
                return
            lines = ["역할 목록:"]
            for r in self.roles.roles.values():
                star = " (기본)" if self.roles.default_role.lower() == r.name.lower() else ""
                lines.append(f"- {r.name} [{r.agent}]{star}: {r.instructions[:60]}")
            self.reply("\n".join(lines))
        elif sub == "show":
            r = self.roles.get(arg.split()[0]) if arg else None
            if not r:
                self.reply("그 이름의 역할이 없어요.")
                return
            self.reply(f"[{r.name}] agent={r.agent}\n지침: {r.instructions}\n"
                       f"session: {r.session_id or '(없음)'}  생성: {r.created_at or '?'}")
        elif sub == "use":
            name = arg.split()[0] if arg else ""
            if self.roles.set_default(name):
                self.reply(f"기본 역할을 '{self.roles.default_role}' 로 지정했어요. "
                           f"이제 그냥 쓴 메시지는 이 역할이 처리해요.")
            else:
                self.reply(f"'{name}' 역할이 없어요.")
        elif sub == "rm":
            name = arg.split()[0] if arg else ""
            if not self.roles.exists(name):
                self.reply(f"'{name}' 역할이 없어요.")
                return
            self.pending = {"kind": "role_rm", "role": self.roles.get(name).name,
                            "at": self.clock()}
            self.reply(f"역할 '{name}' 을(를) 삭제할까요? \"ㅇㅇ\" 또는 \"취소\"")
        else:
            self.reply(f"모르는 /role 명령이에요: {sub}")

    # ------------------------------------------------------- worktree cmds
    def handle_branches(self) -> None:
        repo = self._repo_root()
        if not repo:
            self.reply("작업 폴더가 git 저장소가 아니라 워크트리가 없어요.")
            return
        wts = wtmod.bridge_worktrees(repo, self._wt_base_dir())
        if not wts:
            self.reply("브리지가 만든 워크트리가 없어요.")
            return
        lines = [f"브리지 워크트리 {len(wts)}개:"]
        for w in wts[:15]:
            lines.append(f"- {w.branch}  ({w.path})")
        self.reply("\n".join(lines))

    def handle_clean(self) -> None:
        repo = self._repo_root()
        if not repo:
            self.reply("작업 폴더가 git 저장소가 아니에요.")
            return
        days = float(self.wtcfg.get("clean_older_than_days", 7))
        cands = wtmod.clean_candidates(repo, self._wt_base_dir(), days)
        if not cands:
            self.reply("정리할(병합됐거나 오래된) 워크트리가 없어요.")
            return
        self.pending = {"kind": "clean", "at": self.clock(),
                        "candidates": [{"path": w.path, "branch": w.branch} for w in cands]}
        listed = "\n".join(f"- {w.branch}" for w in cands[:15])
        self.reply(f"병합됐거나 오래된 워크트리 {len(cands)}개를 삭제할까요? "
                   f"\"ㅇㅇ\" 또는 \"취소\"\n{listed}")

    def handle_dry(self, task_text: str) -> None:
        if not task_text:
            self.reply("사용법: /dry <할 일>  (쓰기 없이 계획/읽기 전용으로 실행해요)")
            return
        if self.state.paused:
            self.reply("일시정지 중이라 실행하지 않았어요. /start 로 다시 켜 주세요.")
            return
        role, cmd, explicit = self.resolve_target(task_text)
        if explicit and role and not self.roles.exists(role):
            self.reply(f"'{role}' 역할을 몰라요.")
            return
        if not cmd:
            return
        meta = {"chain_id": uuid.uuid4().hex[:8], "hops": 0,
                "deadline": self.clock() + float(self.rcfg.get("chain_timeout_sec", 3600)),
                "origin_role": "", "dry": True}
        self.audit.event("dry", text=cmd, role=role)
        self.submit(cmd, role=role, meta=meta)

    def _oneshot_meta(self, agent_override: str = "") -> Dict[str, Any]:
        meta = {"chain_id": uuid.uuid4().hex[:8], "hops": 0,
                "deadline": self.clock() + float(self.rcfg.get("chain_timeout_sec", 3600)),
                "origin_role": ""}
        if agent_override:
            meta["agent_override"] = agent_override
        return meta

    def _agent_version(self, agent: str) -> str:
        if agent == "pi":
            return self._pi_version()
        if not self._agent_available(agent):
            return "없음(미설치)"
        import shutil, subprocess
        exe = (self.cfg["agent"].get(agent, {}) or {}).get("executable", agent)
        resolved = exe if (os.path.isabs(exe) and os.path.exists(exe)) else shutil.which(exe)
        if not resolved:
            return "없음(미설치)"
        try:
            out = subprocess.run([resolved, "--version"], capture_output=True,
                                 text=True, timeout=10)
            line = (out.stdout or out.stderr or "").strip().splitlines()
            return line[0] if line else "설치됨"
        except Exception:
            return "설치됨"

    def handle_agent(self, arg: str) -> None:
        a = (arg or "").strip().lower()
        if a in ("claude", "pi", "codex"):
            if not self._agent_available(a):
                self.reply(f"'{a}' 를 쓸 수 없어요 (설치/경로를 확인하세요). /agent 로 상태 확인.")
                return
            self.state.default_agent = a      # persisted in state.json (not config)
            self.agent_name = a
            self.audit.event("agent_default", agent=a)
            self.reply(f"기본 에이전트를 {a} 로 바꿨어요. (그냥 쓴 메시지가 이걸로 실행돼요)")
            return
        if a:
            self.reply("사용법: /agent  또는  /agent <claude|pi|codex>")
            return
        lines = [f"기본 에이전트: {self.agent_name}", "설치된 에이전트:"]
        for ag in ("claude", "pi", "codex"):
            mark = "✓" if self._agent_available(ag) else "✗"
            lines.append(f"- {mark} {ag}: {self._agent_version(ag)}")
        lines.append("바꾸려면 /agent <claude|pi|codex>, 한 번만 쓰려면 /claude·/pi·/codex <할 일>")
        self.reply("\n".join(lines))

    def handle_agent_run(self, agent: str, task_text: str) -> None:
        """/claude|/pi|/codex <task>: run ONE task with that agent. Same worktree,
        report, guard and danger-gating as a normal plain task."""
        if not task_text:
            self.reply(f"사용법: /{agent} <할 일>")
            return
        if not self._agent_available(agent):
            self.reply(f"'{agent}' 를 쓸 수 없어요 (설치/경로를 확인하세요). /agent 로 상태 확인.")
            return
        if self.state.paused:
            self.reply("일시정지 중이라 실행하지 않았어요. /start 로 다시 켜 주세요.")
            return
        s = self.cfg["safety"]
        ok, why = self.limiter.check(task_text)
        if not ok:
            self.state.paused = True
            self.state.pause_reason = f"안전장치({why})"
            self.audit.event("decision", action="circuit_breaker_pause", reason=why)
            self.reply(f"안전장치: {why}. 일시정지했어요. 확인 후 /start 로 다시 켜 주세요.")
            return
        hits = safety.dangerous_matches(task_text, self.patterns)
        if hits:
            self.pending = {"kind": "danger", "text": task_text, "role": "",
                            "at": self.clock(), "meta": self._oneshot_meta(agent)}
            self.audit.event("decision", action="ask_approval", text=task_text,
                             matched=hits, agent=agent)
            self.reply(f"위험할 수 있는 작업이에요({', '.join(sorted(set(hits)))}). "
                       f"실행할까요? \"ㅇㅇ\" 또는 \"취소\"\n> {task_text[:200]}")
            return
        self.audit.event("agent_run", agent=agent, text=task_text)
        self.submit(task_text, role="", meta=self._oneshot_meta(agent))

    # ------------------------------------------------------------ run
    def submit(self, text: str, role: str = "", approved: bool = False,
               meta: Optional[Dict[str, Any]] = None) -> None:
        if meta is None:
            meta = {"chain_id": uuid.uuid4().hex[:8], "hops": 0,
                    "deadline": self.clock() + float(self.rcfg.get("chain_timeout_sec", 3600)),
                    "origin_role": ""}
        task = Task(prompt=text, role=role, meta=meta)
        if self.runner.busy or self.queue:
            if len(self.queue) >= self.cfg["safety"]["max_queue"]:
                self.audit.event("decision", action="queue_full", text=text, role=role)
                self.reply("대기열이 꽉 찼어요. 지금 작업이 끝난 뒤 다시 보내 주세요.", role=role)
                return
            self.queue.append(task)
            self.audit.event("decision", action="queued", text=text, role=role,
                             position=len(self.queue))
            self.reply(f"지금 다른 작업 중이라 대기열에 넣었어요 ({len(self.queue)}번째).", role=role)
            return
        self._start(task, approved)

    def _build_spec(self, task: Task) -> RunSpec:
        meta = dict(task.meta)
        wd = meta.get("workdir")
        dry = bool(meta.get("dry"))
        role = self.roles.get(task.role) if task.role else None
        if role is None:
            agent = meta.get("agent_override") or self.agent_name
            # pi plain task: fresh session (handled by runner --no-session)
            spec = RunSpec(prompt=task.prompt, role="", agent_type=agent,
                           append_system_prompt=(self.default_prompt or None),
                           workdir=wd, dry=dry, meta=meta)
            spec.meta["agent"] = agent
            return spec
        instr = (self.default_prompt + "\n\n" + role.instructions
                 if self.default_prompt else role.instructions)
        others = [n for n in self.roles.names() if n.lower() != role.name.lower()]
        if others:
            instr += ("\n\n다른 역할에게 넘기려면 결과 맨 마지막 줄에 '@역할이름 메시지' 형식으로 적으세요. "
                      f"사용 가능한 역할: {', '.join(others)}.")
        agent = role.agent
        if not self._agent_available(agent):
            fallback = self.agent_name if self._agent_available(self.agent_name) else "claude"
            if fallback != agent:
                instr += f"\n\n(참고: {agent} 를 쓸 수 없어 {fallback} 로 대신 실행해요.)"
                agent = fallback
        allowed = rolemod.sanitize_allowed(
            role.allowed_tools, self.cfg["agent"]["claude"].get("disallowed_tools", []))
        # pi uses a stable per-role --session-id; claude/codex resume by stored id
        if agent == "pi":
            prefix = (self.cfg["agent"].get("pi", {}) or {}).get("session_prefix", "bridge-")
            resume = f"{prefix}{role.name}"
        else:
            resume = role.session_id
        spec = RunSpec(prompt=task.prompt, agent_type=agent, append_system_prompt=instr,
                       allowed_tools=allowed, resume_session_id=resume,
                       track_session=True, role=role.name, workdir=wd, dry=dry, meta=meta)
        spec.meta["agent"] = agent
        return spec

    def _codex_available(self) -> bool:
        import shutil
        exe = self.cfg["agent"]["codex"].get("executable", "codex")
        return shutil.which(exe) is not None

    def _pi_available(self) -> bool:
        import shutil
        exe = (self.cfg["agent"].get("pi", {}) or {}).get("executable", "pi")
        return bool(shutil.which(exe) or (os.path.isabs(exe) and os.path.exists(exe)))

    def _agent_available(self, agent: str) -> bool:
        if agent == "codex":
            return self._codex_available()
        if agent == "pi":
            return self._pi_available()
        return True   # claude / custom assumed present

    def _pi_version(self) -> str:
        if self._pi_version_cache is not None:
            return self._pi_version_cache
        import shutil, subprocess
        exe = (self.cfg["agent"].get("pi", {}) or {}).get("executable", "pi")
        resolved = exe if (os.path.isabs(exe) and os.path.exists(exe)) else shutil.which(exe)
        ver = ""
        if resolved:
            try:
                out = subprocess.run([resolved, "--version"], capture_output=True,
                                     text=True, timeout=10)
                ver = (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
            except Exception:
                ver = ""
        self._pi_version_cache = ver or ("설치됨" if resolved else "없음")
        return self._pi_version_cache

    def _loop_guard_trips(self, task: "Task") -> bool:
        """Hard stop for a self-echo loop the dedupe/limiter might miss.

        A run is "suspicious" only via _run_is_suspicious (near a bot send AND
        overlapping a recent bot message). Handoff tasks (hops>0) are governed by
        their own hop/chain limits, so they NEVER count as suspicious, and they
        count toward the global cap only when chain limits are disabled."""
        now = self.clock()
        meta = task.meta or {}
        is_handoff = int(meta.get("hops", 0)) > 0 or bool(meta.get("origin_role"))
        chain_limits_on = bool(self.rcfg.get("max_hops")) and bool(self.rcfg.get("chain_timeout_sec"))
        suspicious = (not is_handoff) and self._run_is_suspicious(task.prompt, now)
        count_global = (not is_handoff) or (not chain_limits_on)
        gwin = float(self.lg.get("global_window_min", 10)) * 60
        swin = float(self.lg.get("window_min", 5)) * 60
        while self._run_log and now - self._run_log[0][0] > max(gwin, swin):
            self._run_log.popleft()
        self._run_log.append((now, suspicious, count_global))
        gcount = sum(1 for ts, _, cg in self._run_log if cg and now - ts <= gwin)
        if gcount > int(self.lg.get("max_runs_per_global", 12)):
            return True
        scount = sum(1 for ts, sus, _ in self._run_log if sus and now - ts <= swin)
        if scount >= int(self.lg.get("max_near_runs", 3)):
            return True
        return False

    def _trip_loop_guard(self) -> None:
        self.state.paused = True
        self.state.pause_reason = "반복 감지"
        self.queue.clear()
        self.pending = None
        self.audit.event("loop_guard_pause", runs=len(self._run_log))
        self.reply("반복이 감지돼서 멈추었어요. /start 로 다시 켜세요.")

    def _start(self, task: Task, approved: bool = False) -> None:
        self.audit.event("decision", action="run", text=task.prompt, role=task.role,
                         approved=approved, dry_run=self.dry_run, meta=task.meta)
        if self.dry_run:
            self.reply(f"[dry-run] 실행했다고 치고 넘어갈게요: {task.prompt[:200]}", role=task.role)
            return
        if self._loop_guard_trips(task):
            self._trip_loop_guard()
            return
        if not task.meta.get("dry"):
            self._ensure_worktree(task)
        spec = self._build_spec(task)
        t = self.runner.start(spec)
        # phase-change progress: reset the silence timer for this task
        self._phase_task_id = t.task_id
        self._silence_posted = False
        if self.cfg["reply"].get("ack_on_start"):
            label = spec.meta.get("agent", self.agent_name)
            br = task.meta.get("branch")
            tail = f", {br}" if br else ""
            self.reply(f"시작했어요 ({label}, 작업 {t.task_id}{tail}).", role=task.role)

    def handle_builtin(self, cmd: str) -> None:
        self.audit.event("builtin", cmd=cmd)
        if cmd == "/help":
            self.reply(HELP_TEXT)
        elif cmd == "/status":
            cur = self.runner.current
            repo = self._repo_root()
            nwt = len(wtmod.bridge_worktrees(repo, self._wt_base_dir())) if repo else 0
            agent_line = f"에이전트: {self.agent_name} / 폴더: {self.runner.workdir}"
            if self.agent_name == "pi" or any(r.agent == "pi" for r in self.roles.all()):
                agent_line += f" / pi {self._pi_version()}"
            lines = [f"상태: {'일시정지' if self.state.paused else '동작 중'}",
                     agent_line,
                     f"기본 역할: {self.roles.default_role or '(없음)'} / 역할 {len(self.roles.names())}개",
                     f"pstack: {'설치됨' if self._pstack_installed() else '없음(원칙은 수동 적용)'}",
                     f"워크트리: {nwt}개 / 대기열: {len(self.queue)}개"]
            if cur:
                tag = f"[{cur.role}] " if cur.role else ""
                lines.append(f"실행 중: {tag}{cur.task_id} ({int(self.clock() - cur.started_at)}초째) "
                             f"- {cur.prompt[:80]}")
            if self.pending:
                lines.append(f"승인 대기({self.pending['kind']}): "
                             f"{(self.pending.get('text') or self.pending.get('role'))[:80]}")
            self.reply("\n".join(lines))
        elif cmd == "/stop":
            cur = self.runner.current
            killed = self.runner.stop()     # os.killpg of the whole process group
            dropped = len(self.queue)
            self.queue.clear()              # kills the whole handoff chain too
            self.pending = None
            self.state.paused = True
            self.state.pause_reason = "사용자 /stop"
            self.audit.event("kill_switch", killed=killed, dropped_queue=dropped,
                             task=cur.task_id if cur else None)
            repo = self._repo_root()
            wts = wtmod.bridge_worktrees(repo, self._wt_base_dir()) if repo else []
            what = (f"실행 중이던 작업({cur.task_id})을 프로세스 그룹째 중단했고, " if killed and cur
                    else ("실행 중인 작업을 중단했고, " if killed else ""))
            tail = ""
            if wts:
                tail = ("\n워크트리는 그대로 두었어요(" + f"{len(wts)}개): "
                        + ", ".join(w.branch for w in wts[:5])
                        + ("" if len(wts) <= 5 else " ...") + "\n/clean 으로 정리할 수 있어요.")
            self.reply(f"멈췄어요. {what}대기열 {dropped}개를 비웠어요. "
                       f"/start 전까지 명령을 실행하지 않아요.{tail}")
        elif cmd == "/start":
            self.state.paused = False
            self.reply("다시 켰어요. 명령을 보내 주세요.")

    # ------------------------------------------------------------ results
    def drain_results(self) -> None:
        while True:
            try:
                res: AgentResult = self.runner.results.get_nowait()
            except Exception:
                break
            if res.role and res.session_id:
                self.roles.set_session(res.role, res.session_id)
            self.reply(self.format_result(res), role="")
            if (self.rcfg.get("enabled", True) and not self.state.paused
                    and not res.stopped and not res.timed_out):
                self.process_handoffs(res)
        if not self.runner.busy and self.queue and not self.state.paused:
            self._start(self.queue.popleft())

    def process_handoffs(self, res: AgentResult) -> None:
        meta = res.meta or {}
        hops = int(meta.get("hops", 0))
        deadline = meta.get("deadline")
        pair_log = meta.get("pair_log") or []
        sender = res.role
        allowlist = self.hcfg.get("allowlist", handoffmod.DEFAULT_PAIRS)
        chars = int(self.rcfg.get("handoff_summary_chars", 400))
        for name, msg, known in rolemod.find_handoffs(res.summary, self.roles.names()):
            if not known:
                self.audit.event("handoff", status="unknown", target=name)
                self.reply(f"'{name}' 역할을 몰라서 넘기지 못했어요.", role=sender)
                continue
            if sender and name.lower() == sender.lower():
                self.audit.event("handoff", status="self", target=name)
                self.reply(f"자기 자신(@{name})에게는 넘길 수 없어요.", role=sender)
                continue
            if not handoffmod.is_allowed(sender, name, allowlist):
                self.audit.event("handoff", status="not_allowed", sender=sender, target=name)
                self.reply(f"허용되지 않은 핸드오프예요 ({sender or '사용자'}->{name}). "
                           f"허용된 쌍만 넘길 수 있어요. 연쇄를 멈췄어요.", role=sender)
                break
            if handoffmod.bounced_twice(pair_log, sender, name):
                self.audit.event("handoff", status="bounce", sender=sender, target=name)
                self.reply(f"같은 두 역할({sender}<->{name})이 반복돼서 연쇄를 멈췄어요.", role=sender)
                break
            if hops + 1 > int(self.rcfg.get("max_hops", 4)):
                self.audit.event("handoff", status="max_hops", target=name)
                self.reply(f"연쇄 작업 한도(최대 {self.rcfg.get('max_hops', 4)}단계)에 도달해서 "
                           f"@{name} 로는 넘기지 않았어요.", role=sender)
                break
            if deadline is not None and self.clock() > deadline:
                self.audit.event("handoff", status="timeout", target=name)
                self.reply("연쇄 작업 시간이 다 돼서 더 넘기지 않았어요.", role=sender)
                break
            payload = handoffmod.wrap_payload(sender, msg, res.summary, chars)
            new_meta = {"chain_id": meta.get("chain_id"), "hops": hops + 1,
                        "deadline": deadline, "origin_role": sender,
                        "pair_log": handoffmod.record_hop(pair_log, sender, name),
                        "workdir": meta.get("workdir"), "branch": meta.get("branch"),
                        "repo": meta.get("repo")}
            hits = safety.dangerous_matches(msg, self.patterns)
            if hits:
                self.pending = {"kind": "handoff", "text": payload, "role": name,
                                "at": self.clock(), "meta": new_meta, "hits": hits}
                self.audit.event("handoff", status="ask_approval", target=name, matched=hits)
                self.reply(f"@{name} 로 넘길 작업이 위험할 수 있어요({', '.join(sorted(set(hits)))}). "
                           f"실행할까요? \"ㅇㅇ\" 또는 \"취소\"\n> {msg[:200]}", role=sender)
                break
            self.audit.event("handoff", status="enqueue", target=name, hops=hops + 1)
            self.reply(f"@{name} 에게 이어서 작업을 넘겼어요.", role=sender)
            self.submit(payload, role=name, meta=new_meta)

    def format_result(self, r: AgentResult) -> str:
        """Phone-sized report (report.format_report). The bot prefix is added by
        reply(); line 1 therefore reads '🤖 ✅ <role> · <dur> · <branch>'."""
        meta = r.meta or {}
        label = meta.get("agent") or self.agent_name
        role_disp = r.role or label
        branch = meta.get("branch", "")
        wd = meta.get("workdir") or self.runner.workdir
        diff = wtmod.diffstat(wd) if wd else []
        allowlist = self.hcfg.get("allowlist", handoffmod.DEFAULT_PAIRS)
        handoff_to = ""
        for name, _msg, known in rolemod.find_handoffs(r.summary, self.roles.names()):
            if known and handoffmod.is_allowed(r.role, name, allowlist):
                handoff_to = name
                break
        needs = ""
        if r.is_error and not r.stopped and not r.timed_out:
            needs = "에이전트가 오류로 끝났어요. 로그를 확인하세요."
        elif r.timed_out:
            needs = "시간 초과로 중단됐어요."
        rc = self.cfg["reply"]
        return reportmod.format_report(
            role=role_disp, ok=r.ok, duration_sec=r.duration_sec, branch=branch,
            summary=r.summary, diffstat_lines=diff, needs_you=needs,
            handoff_to=handoff_to, log_path=r.output_path, warn=r.is_error,
            body_chars=int(rc.get("report_body_chars", 1200)),
            body_lines=int(rc.get("report_body_lines", 20)))

    def _do_clean(self, candidates: List[Dict[str, Any]]) -> None:
        repo = self._repo_root()
        if not repo or not candidates:
            self.reply("정리할 워크트리가 없어요.")
            return
        removed = []
        for c in candidates:
            w = wtmod.Worktree(path=c["path"], branch=c["branch"], repo=repo)
            if wtmod.remove_worktree(w):
                removed.append(c["branch"])
        self.audit.event("clean", removed=removed)
        self.reply(f"워크트리 {len(removed)}개를 정리했어요: {', '.join(removed) or '없음'}")


# ---------------------------------------------------------------- wiring
def make_backend(cfg: Dict[str, Any]) -> ChatBackend:
    b = cfg["backend"]
    if b == "ax":
        from teams_ax import TeamsAXBackend
        return TeamsAXBackend(cfg)
    if b == "uia":
        from teams_uia import TeamsUIABackend
        return TeamsUIABackend(cfg)
    if b == "vision":
        from teams_vision import TeamsVisionBackend
        return TeamsVisionBackend(cfg)
    raise ValueError(f"unknown backend: {b}")


def _acquire_single_instance_lock(data_dir: str):
    """Take an exclusive, non-blocking fcntl lock on data/bridge.lock and return
    the open fd (held for the process lifetime). Returns None if another live
    bridge already holds it. On platforms without fcntl, locking is skipped."""
    lock_path = os.path.join(data_dir, "bridge.lock")
    try:
        import fcntl
    except ImportError:
        return open(lock_path, "w")      # best effort on non-POSIX; no real lock
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Teams self-chat agent bridge")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="never run agents or type into Teams")
    ap.add_argument("--dump-tree", action="store_true", help="write the Teams UIA tree to data/uia_tree.txt")
    ap.add_argument("--show-messages", action="store_true", help="print parsed visible messages and exit")
    ap.add_argument("--resume", action="store_true",
                    help="clear a persisted pause (paused=true in state.json) at startup")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    data_dir = cfg["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    enable_dpi_awareness()
    backend = make_backend(cfg)

    if args.dump_tree:
        path = os.path.join(data_dir, "uia_tree.txt")
        backend.dump_debug(path)
        print(f"wrote {path}")
        return 0
    if args.show_messages:
        print("title:", backend.focus_self_chat())
        for m in backend.read_visible_messages():
            print(f"[{m.timestamp}] {m.text!r}   (raw={m.raw[:120]!r})")
        return 0

    # single-instance guard: refuse to start a second live bridge on the same
    # data dir (two bridges answering one chat caused duplicate replies).
    lock_fd = _acquire_single_instance_lock(data_dir)
    if lock_fd is None:
        print("이미 다른 브리지가 실행 중이에요 (data/bridge.lock). 중복 실행을 막았어요.")
        return 1

    workdir = safety.check_workdir(cfg["agent"]["workdir"], cfg["agent"]["allowed_roots"])
    audit = Audit(os.path.join(data_dir, "logs"), cfg["log"]["max_bytes"], cfg["log"]["backup_count"])
    state = StateStore(os.path.join(data_dir, "state.json"))
    if args.resume and state.paused:
        prev = state.pause_reason
        state.paused = False
        state.save()
        print(f"일시정지를 해제했어요 (이전 이유: {prev or '알 수 없음'}).")
    runner = AgentRunner(cfg["agent"], workdir, os.path.join(data_dir, "outputs"), audit)
    roles = RoleStore(os.path.join(data_dir, "roles.json"))
    bridge = Bridge(cfg, backend, runner, state, audit, roles=roles)
    audit.event("bridge_start", backend=cfg["backend"], agent=cfg["agent"].get("type") or cfg["agent"].get("default") or "claude",
                workdir=workdir, dry_run=bridge.dry_run, paused=state.paused,
                roles=roles.names())

    interval = max(5, int(cfg["poll_interval_sec"]))
    try:
        while True:
            try:
                bridge.cycle()
            except Exception as e:
                audit.event("cycle_error", error=f"{type(e).__name__}: {e}")
            if args.once:
                break
            deadline = time.time() + interval
            while time.time() < deadline:
                if not runner.results.empty():
                    break
                bridge.maybe_progress()
                time.sleep(1)
    except KeyboardInterrupt:
        audit.event("bridge_stop", reason="ctrl-c")
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
