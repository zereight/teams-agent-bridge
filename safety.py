"""Pure-python safety rules (no Windows deps, fully unit tested).

* is_bot_message      - never treat bot output as a command (loop prevention)
* parse_builtin       - /status /stop /start /help
* is_dangerous        - keyword/regex gate that requires explicit approval
* is_approval/cancel  - approval vocabulary
* RateLimiter         - circuit breaker against runaway loops
* check_workdir       - the agent may only run inside allowed roots
* truncate/redact     - keep chat replies short and free of obvious secrets
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from collections import deque
from typing import Callable, Deque, Iterable, List, Optional, Sequence, Tuple

from state import normalize_text

BUILTINS = ("/status", "/stop", "/start", "/help")


# ---------------------------------------------------------------- bot output
def _is_wordish_prefix(p: str) -> bool:
    """ASCII bracket-style prefix like "(b)" / "[b]" (vs an emoji token)."""
    return bool(re.search(r"[A-Za-z0-9()\[\]]", p))


def has_bot_prefix(text: str, prefixes: Sequence[str]) -> bool:
    """True if the message is bot output, INDEPENDENT of where the prefix sits.

    Teams AX can return a multi-paragraph message with its lines reversed and
    concatenated with no separator, so the "(b)" prefix may end up in the middle.
    We treat it as bot output if ANY line starts with the prefix, or the prefix
    token appears anywhere on a boundary (start / whitespace / ) ] `)."""
    nfc = unicodedata.normalize("NFC", text or "")
    if not nfc.strip():
        return False
    collapsed = normalize_text(nfc)
    norm_prefixes = [normalize_text(p) for p in prefixes if p]
    # 1) any physical line starts with a prefix
    for line in re.split(r"[\r\n]+", nfc):
        ln = normalize_text(line)
        for p in norm_prefixes:
            if p and ln.startswith(p):
                return True
    # 2) the prefix token appears anywhere, anchored on a boundary
    for p in norm_prefixes:
        if not p:
            continue
        if _is_wordish_prefix(p):
            # the token anywhere, as long as it is not glued to a word character
            # (so "(b)" is caught even when preceded by '.', '/', ')' ... after a
            # multi-paragraph message was shuffled into one line)
            pat = r"(?<![0-9A-Za-z\uac00-\ud7a3])" + re.escape(p) + r"(?![0-9A-Za-z\uac00-\ud7a3])"
            if re.search(pat, collapsed):
                return True
        else:                     # emoji / symbol prefix: substring is enough
            if p in collapsed:
                return True
    return False


def is_bot_message(text: str, prefixes: Sequence[str],
                   recent_sent: Iterable[str] = ()) -> bool:
    if has_bot_prefix(text, prefixes):
        return True
    t = normalize_text(text)
    if not t:
        return False
    # second line of defence: text we typed ourselves recently, even if Teams
    # mangled/stripped the prefix (full memory lives in state.StateStore.is_echo)
    for snip in recent_sent:
        if snip and len(snip) >= 8 and snip in t:
            return True
    return False


# ---------------------------------------------------------------- builtins
def parse_builtin(text: str) -> Optional[str]:
    t = normalize_text(text).lower()
    if not t.startswith("/"):
        return None
    word = t.split()[0]
    return word if word in BUILTINS else None


# ---------------------------------------------------------------- danger gate
def compile_patterns(patterns: Sequence[str]) -> List[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def dangerous_matches(text: str, patterns: Sequence[re.Pattern]) -> List[str]:
    t = normalize_text(text)
    hits = []
    for p in patterns:
        m = p.search(t)
        if m:
            hits.append(m.group(0))
    return hits


def is_dangerous(text: str, patterns: Sequence[re.Pattern]) -> bool:
    return bool(dangerous_matches(text, patterns))


_TRAIL = re.compile(r"[\s.!?~,。！？]+$")


def _word(text: str) -> str:
    return _TRAIL.sub("", normalize_text(text).lower())


def is_approval(text: str, words: Sequence[str]) -> bool:
    return _word(text) in {w.lower() for w in words}


def is_cancel(text: str, words: Sequence[str]) -> bool:
    return _word(text) in {w.lower() for w in words}


# ---------------------------------------------------------------- loop breaker
class RateLimiter:
    """Trips when too many commands arrive or the same one repeats.

    check() returns (ok, reason). The bridge pauses itself when not ok.
    """

    def __init__(self, max_per_hour: int, repeat_limit: int, repeat_window_sec: int,
                 clock: Callable[[], float] = time.time):
        self.max_per_hour = max_per_hour
        self.repeat_limit = repeat_limit
        self.repeat_window = repeat_window_sec
        self.clock = clock
        self.events: Deque[Tuple[float, str]] = deque()

    def check(self, text: str) -> Tuple[bool, str]:
        now = self.clock()
        while self.events and now - self.events[0][0] > 3600:
            self.events.popleft()
        key = normalize_text(text).lower()
        if self.max_per_hour and len(self.events) >= self.max_per_hour:
            return False, f"1시간에 {self.max_per_hour}개 넘는 명령이 들어왔어요"
        same = sum(1 for ts, k in self.events if k == key and now - ts <= self.repeat_window)
        if self.repeat_limit and same >= self.repeat_limit:
            return False, f"같은 명령이 {self.repeat_window // 60}분 안에 {same + 1}번 들어왔어요"
        self.events.append((now, key))
        return True, ""


# ---------------------------------------------------------------- workdir
def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(p))))


def check_workdir(workdir: str, allowed_roots: Sequence[str]) -> str:
    """Return the normalised workdir or raise ValueError."""
    if not workdir:
        raise ValueError("agent.workdir 가 설정되지 않았어요")
    wd = _norm_path(workdir)
    if not os.path.isdir(wd):
        raise ValueError(f"agent.workdir 폴더가 없어요: {workdir}")
    if allowed_roots:
        for root in allowed_roots:
            r = _norm_path(root)
            if wd == r or wd.startswith(r.rstrip(os.sep) + os.sep):
                return wd
        raise ValueError(f"agent.workdir 가 allowed_roots 밖에 있어요: {workdir}")
    return wd


# ---------------------------------------------------------------- output
def truncate(text: str, max_chars: int) -> Tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text, False
    return text[: max(0, max_chars - 1)].rstrip() + "…", True


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                 # OpenAI / Anthropic style keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),             # GitHub PAT / OAuth / app tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),           # GitHub fine-grained PAT
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),          # Slack token
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key id
    re.compile(r"ASIA[0-9A-Z]{16}"),                       # AWS temporary access key id
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),    # Authorization: Bearer <token>
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
    # generic <name containing key/secret/token/password> = <long high-entropy value>
    re.compile("(?i)[A-Za-z0-9_\\-]*(?:key|secret|token|password)[A-Za-z0-9_\\-]*\\s*[:=]\\s*[\"']?[A-Za-z0-9+/_\\-]{24,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: str) -> str:
    """Best-effort masking of obvious secrets before text goes into Teams."""
    out = text or ""
    for p in _SECRET_PATTERNS:
        out = p.sub("[REDACTED]", out)
    return out
