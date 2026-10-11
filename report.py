"""Phone-sized final report formatter.

Layout (compact header first, then the agent's own result body):
  line1   🤖 ✅|❌|⚠️ <role> · <duration> · <branch>   (🤖 added by Bridge.reply)
  bullets • files changed summary (git diff --stat), '검증 못함: ...'
          확인 필요: ...     (only when truthy)
          → @qa 로 넘김       (only on a handoff)
  body    the agent's result text, markdown lightly cleaned (** and headings
          stripped; bullets / numbered lists / `code spans` kept), clipped to a
          configurable limit at a line/paragraph boundary.
  footer  (로그: <path>)  or, when the body was clipped,  …(전체: <path>)
"""
from __future__ import annotations

import re
from typing import List, Optional

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}초"
    return f"{sec // 60}분 {sec % 60}초"


def _diffstat_summary(diffstat_lines: List[str]) -> Optional[str]:
    """Collapse `git diff --stat` output to one line, e.g. '3 files, +40 -5'."""
    if not diffstat_lines:
        return None
    last = diffstat_lines[-1].strip()
    if "changed" in last or "insertion" in last or "deletion" in last:
        return last.replace("file changed", "파일 변경").replace("files changed", "파일 변경")
    return f"변경 파일 {len(diffstat_lines)}개"


def clean_markdown(text: str) -> str:
    """Lightly de-markdown agent output for a chat bubble: drop ** bold and ATX
    headings, keep bullets / numbered lists / `code spans` as-is."""
    out = []
    for ln in (text or "").splitlines():
        s = _HEADING_RE.sub("", ln)         # "### Title" -> "Title"
        s = _BOLD_RE.sub(r"\1", s)          # **x** -> x
        s = s.replace("**", "")             # stray/unbalanced markers
        out.append(s.rstrip())
    # collapse 3+ blank lines to a single blank line
    joined = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def clip_body(text: str, max_chars: int, max_lines: int):
    """Return (clipped_text, truncated). Cuts at a line boundary when possible."""
    t = text or ""
    truncated = False
    lines = t.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    body = "\n".join(lines)
    if len(body) > max_chars:
        cut = body[:max_chars]
        nl = cut.rfind("\n")
        if nl > max_chars * 0.5:            # prefer a line boundary near the end
            cut = cut[:nl]
        body = cut
        truncated = True
    return body.rstrip(), truncated


def format_report(*, role: str, ok: bool, duration_sec: float, branch: str,
                  summary: str, diffstat_lines: Optional[List[str]] = None,
                  not_verified: Optional[List[str]] = None,
                  needs_you: str = "", handoff_to: str = "",
                  log_path: str = "", warn: bool = False,
                  body_chars: int = 1200, body_lines: int = 20,
                  summary_limit: Optional[int] = None) -> str:
    if summary_limit is not None:            # back-compat alias
        body_chars = summary_limit
    sym = "✅" if ok else "❌"
    if ok and (warn or not_verified):
        sym = "⚠️"
    role_tag = role or "작업"
    br = branch or "(워크트리 없음)"
    lines = [f"{sym} {role_tag} · {_fmt_duration(duration_sec)} · {br}"]

    # compact metadata bullets (glanceable, before the fuller body)
    ds = _diffstat_summary(diffstat_lines or [])
    if ds:
        lines.append(f"• {ds}")
    for nv in (not_verified or []):
        if nv:
            lines.append(f"• 검증 못함: {nv}")
    if needs_you:
        lines.append(f"확인 필요: {needs_you}")
    if handoff_to:
        lines.append(f"→ @{handoff_to} 로 넘김")

    # the agent's own result body
    cleaned = clean_markdown(summary)
    if not cleaned:
        cleaned = "(결과 텍스트 없음)"
    body, truncated = clip_body(cleaned, body_chars, body_lines)
    lines.append(body)

    if log_path:
        lines.append(f"…(전체: {log_path})" if truncated else f"(로그: {log_path})")
    return "\n".join(lines)
