"""Persistent state + message dedupe.

Dedupe strategy (why not just "hash of text"):
  The chat can legitimately contain the same text twice ("ㅇㅇ", "테스트 돌려줘"),
  and the UI often has no precise timestamp. So we use a *tail anchor*:

  * Every read yields an ordered list of base fingerprints
    fp = sha256(normalised text + timestamp).
  * We persist the tail of the previously seen list (`seen_tail`).
  * New messages are those AFTER the point where the current list lines up
    with the end of `seen_tail`.
  * If the lists cannot be lined up at all (user scrolled, UI re-rendered,
    a message was edited...) we RESYNC: mark everything visible as seen and
    execute nothing. Missing a command is acceptable, running one twice or
    running old history is not.
  * Additionally every handled message's occurrence fingerprint is stored in
    `processed`; if the UI exposes timestamps, a message whose exact
    (text, timestamp, occurrence) was processed before is never run again.

On first start (no state file) everything visible is marked as seen.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from typing import Dict, List, Optional, Sequence

from backend_base import ChatMessage

STATE_VERSION = 1
FP_VERSION = 2        # bump when the text->fingerprint scheme changes (e.g. visual-order bodies)
MAX_TAIL = 200
MAX_PROCESSED = 3000
MAX_RECENT_SENT = 50
MAX_SENT = 200
MAX_SENT_LINES = 400
MIN_LINE_LEN = 6
ECHO_OVERLAP = 0.80
ECHO_MIN_CONTAIN = 16
ECHO_COVERAGE = 0.70   # an incoming msg is an echo only if >= this fraction of it is bot text
ECHO_MIN_LEN = 12      # below this length only an EXACT whole-message match counts


def normalize_text(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "")
    t = t.replace("\u200b", "").replace("\xa0", " ")
    return " ".join(t.split())


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:20]


def base_fingerprint(text: str, timestamp: str = "") -> str:
    return _h(f"v{FP_VERSION}\x1f{normalize_text(timestamp)}\x1f{normalize_text(text)}")


def stable_fingerprint(text: str, timestamp: str = "") -> str:
    """Version-INDEPENDENT identity of a message (text + timestamp), used for the
    startup baseline and 'seen this process' set so a fp_version migration can
    never resurface a message that was visible at startup."""
    return _h(f"{normalize_text(timestamp)}\x1f{normalize_text(text)}")


def assign_fingerprints(messages: Sequence[ChatMessage]) -> List[ChatMessage]:
    counts: Dict[str, int] = {}
    for m in messages:
        m.fp = base_fingerprint(m.text, m.timestamp)
        m.sfp = stable_fingerprint(m.text, m.timestamp)
        n = counts.get(m.fp, 0)
        counts[m.fp] = n + 1
        m.occ_fp = _h(f"{m.fp}\x1f{n}")
    return list(messages)


def find_new_start(prev_tail: Sequence[str], cur: Sequence[str]) -> Optional[int]:
    """Index in `cur` where new messages start, or None if no alignment.

    Picks the largest j such that cur[j-m+1 .. j] == prev_tail[-m:] where
    m = min(len(prev_tail), j+1) (i.e. the overlap is as long as possible).
    """
    if not prev_tail:
        return None
    if not cur:
        return 0
    for j in range(len(cur) - 1, -1, -1):
        m = min(len(prev_tail), j + 1)
        if list(cur[j - m + 1: j + 1]) == list(prev_tail[-m:]):
            return j + 1
    return None


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.data = {
            "version": STATE_VERSION,
            "fp_version": FP_VERSION,
            "initialized": False,
            "seen_tail": [],
            "processed": [],
            "paused": False,
            "pause_reason": "",
            "default_agent": "",     # per-user default agent (set via /agent), overrides config
            "recent_sent": [],
            "sent_norm": [],      # whitespace-removed, prefix-stripped, NFC full messages
            "sent_lines": [],     # per-line fingerprints (len>=MIN_LINE_LEN), same normalisation
        }
        self._processed_set: set = set()
        self.baseline: set = set()        # stable fps visible at process startup
        self.seen_ever: set = set()       # stable fps seen at any point this process
        self.last_event = ""   # "init" | "resync" | "ok" | "baseline" (for audit)
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self) -> None:
        incoming_fp = FP_VERSION
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            incoming_fp = loaded.get("fp_version")   # missing in old files -> None
            self.data.update(loaded)
        # If the fingerprint scheme changed since this file was written, the old
        # anchors/processed hashes no longer line up with freshly-read text and
        # would resync-spam forever. Drop them and re-init SILENTLY (keep paused
        # flag + sent-message memory). Never replay old history.
        if incoming_fp != FP_VERSION:
            self.data["seen_tail"] = []
            self.data["processed"] = []
            self.data["initialized"] = False
            self.data["fp_version"] = FP_VERSION
        self._processed_set = set(self.data["processed"])

    def save(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        self.data["seen_tail"] = self.data["seen_tail"][-MAX_TAIL:]
        self.data["processed"] = self.data["processed"][-MAX_PROCESSED:]
        self.data["recent_sent"] = self.data["recent_sent"][-MAX_RECENT_SENT:]
        self.data["sent_norm"] = self.data["sent_norm"][-MAX_SENT:]
        self.data["sent_lines"] = self.data["sent_lines"][-MAX_SENT_LINES:]
        self._processed_set = set(self.data["processed"])
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---- flags -------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return bool(self.data.get("initialized"))

    @property
    def paused(self) -> bool:
        return bool(self.data.get("paused"))

    @paused.setter
    def paused(self, v: bool) -> None:
        self.data["paused"] = bool(v)
        if not v:
            self.data["pause_reason"] = ""
        self.save()

    @property
    def pause_reason(self) -> str:
        return self.data.get("pause_reason", "") or ""

    @pause_reason.setter
    def pause_reason(self, reason: str) -> None:
        self.data["pause_reason"] = reason or ""
        self.save()

    @property
    def default_agent(self) -> str:
        return self.data.get("default_agent", "") or ""

    @default_agent.setter
    def default_agent(self, name: str) -> None:
        self.data["default_agent"] = name or ""
        self.save()

    # ---- dedupe ------------------------------------------------------
    def is_processed(self, m: ChatMessage) -> bool:
        return m.occ_fp in self._processed_set

    def mark_processed(self, m: ChatMessage) -> None:
        if m.occ_fp not in self._processed_set:
            self.data["processed"].append(m.occ_fp)
            self._processed_set.add(m.occ_fp)

    def _mark_all_seen(self, msgs: Sequence[ChatMessage]) -> None:
        for m in msgs:
            self.mark_processed(m)
        self.data["seen_tail"] = [m.fp for m in msgs][-MAX_TAIL:]

    def establish_baseline(self, visible: Sequence[ChatMessage]) -> int:
        """Snapshot EVERY currently-visible message as 'seen at startup' and mark
        it processed, regardless of initialized flag or fp_version migration, so
        nothing on screen at start is ever executed. Returns the count."""
        msgs = assign_fingerprints(list(visible))
        self._mark_all_seen(msgs)
        self.data["initialized"] = True
        self.baseline = {m.sfp for m in msgs}
        self.seen_ever = set(self.baseline)
        self.last_event = "baseline"
        self.save()
        return len(msgs)

    def filter_new(self, visible: Sequence[ChatMessage]) -> Optional[List[ChatMessage]]:
        """Return new messages (oldest first), or None if (re)initialised.

        State is saved before returning, so a crash while handling a command
        never causes that command to run again (at-most-once).
        """
        msgs = assign_fingerprints(list(visible))
        if not msgs:
            self.last_event = "empty"
            return []
        if not self.initialized or not self.data["seen_tail"]:
            self._mark_all_seen(msgs)
            self.data["initialized"] = True
            self.baseline = {m.sfp for m in msgs}
            self.seen_ever.update(self.baseline)
            self.last_event = "init"
            self.save()
            return None
        start = find_new_start(self.data["seen_tail"], [m.fp for m in msgs])
        if start is None:
            # Anchor lost (scrolled, edited, virtualized list, or migrated state).
            # Re-anchor to the current view SILENTLY and keep going. Still surface
            # genuinely-new messages so a command typed in the same poll is not
            # swallowed - but only the CONTIGUOUS TAIL of not-yet-processed,
            # timestamped messages. New messages always arrive at the bottom, so
            # this picks up a just-typed command while old history that scrolled
            # into view (above an already-processed message) is never replayed.
            fresh = []
            for m in reversed(msgs):
                if m.timestamp and not self.is_processed(m) and m.sfp not in self.seen_ever:
                    fresh.append(m)
                else:
                    break
            fresh.reverse()
            self._mark_all_seen(msgs)
            self.seen_ever.update(m.sfp for m in msgs)
            self.last_event = "resync"
            self.save()
            return fresh
        new = []
        for m in msgs[start:]:
            # strong guard: exact same (text, timestamp, occurrence) seen before
            if m.timestamp and self.is_processed(m):
                continue
            new.append(m)
        for m in new:
            self.mark_processed(m)
        tail = self.data["seen_tail"] + [m.fp for m in msgs[start:]]
        self.data["seen_tail"] = tail[-MAX_TAIL:]
        self.seen_ever.update(m.sfp for m in msgs)
        self.last_event = "ok"
        self.save()
        return new

    # ---- loop protection -------------------------------------------
    def remember_sent(self, text: str) -> None:
        self.data["recent_sent"].append(sent_snippet(text))
        # Bot replies routinely QUOTE the user's own text (the danger prompt's
        # "> <command>" line, handoff lines, agent results). If we remembered
        # those spans, a user resending that same text would be mis-flagged as an
        # echo. So we drop user-derived lines before building the echo corpus.
        kept = [ln for ln in text.splitlines() if not _is_user_quote_line(ln)]
        full = norm_nospace("\n".join(kept))
        if full:
            self.data["sent_norm"].append(full)
        for ln in kept:
            n = norm_nospace(ln)
            if len(n) >= MIN_LINE_LEN:
                self.data["sent_lines"].append(n)
        # keep in-memory buffers bounded even before the next save()
        self.data["recent_sent"] = self.data["recent_sent"][-MAX_RECENT_SENT:]
        self.data["sent_norm"] = self.data["sent_norm"][-MAX_SENT:]
        self.data["sent_lines"] = self.data["sent_lines"][-MAX_SENT_LINES:]
        self.save()

    @property
    def recent_sent(self) -> List[str]:
        return list(self.data["recent_sent"])

    def is_echo(self, text: str) -> bool:
        """True if `text` is (a mangled re-read of) something the bridge sent.

        Teams AX can hand our own reply back with its lines reversed/shuffled and
        no separators, so position-based checks are useless. We judge by COVERAGE
        on a whitespace-removed, prefix-stripped, NFC normalisation: a message is
        an echo only if it is (near-)entirely made of bot text.

        * an EXACT whole-message match (any length) is an echo, OR
        * for a message of at least ECHO_MIN_LEN chars, if sent full-messages and
          sent lines (each >= MIN_LINE_LEN) cover >= ECHO_COVERAGE of it.

        Crucially this is COVERAGE, not "contains any sent line": a real user
        command that merely QUOTES one bot/handoff line (e.g. '@dev ... 마지막 줄에
        정확히 @qa ...') is only partly covered, so it is NOT dropped. User-derived
        spans were already excluded from the corpus in remember_sent()."""
        t = norm_nospace(text)
        if not t:
            return False
        sent_norms = self.data.get("sent_norm", [])
        if t in sent_norms:                 # exact whole-message echo
            return True
        if len(t) < ECHO_MIN_LEN:           # too short to judge; never swallow
            return False
        pieces = list(sent_norms) + list(self.data.get("sent_lines", []))
        if _coverage(t, pieces, MIN_LINE_LEN) >= ECHO_COVERAGE:
            return True
        # a reshuffled/mangled copy of a single sent message keeps the same
        # characters, so a high character-multiset overlap with one sent message
        # is also an echo (the @dev-quotes-one-line case stays well below this).
        for snorm in sent_norms:
            if snorm and _overlap(t, snorm) >= ECHO_OVERLAP:
                return True
        return False


def sent_snippet(text: str, n: int = 40) -> str:
    """Normalised start of a bot message body (prefix removed) for loop detection."""
    t = normalize_text(text)
    for p in ("(b)", "[b]"):
        if t.startswith(p):
            t = t[len(p):].strip()
    return t[:n]


def norm_nospace(text: str) -> str:
    """NFC, strip a leading bot prefix, remove ALL whitespace. Order-insensitive key."""
    t = unicodedata.normalize("NFC", text or "")
    t = t.replace("\u200b", "")
    for p in ("(b)", "[b]", "(b) ", "[b] "):
        if t.lstrip().startswith(p):
            t = t.lstrip()[len(p):]
            break
    return re.sub(r"\s+", "", t)


def _overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    return inter / max(len(a), len(b))


def _is_user_quote_line(line: str) -> bool:
    """A sent line that merely quotes the user's own input (so it must NOT seed
    the echo corpus). Today that is the danger-approval prompt's '> <command>'."""
    return line.lstrip().startswith(">")


def _coverage(t: str, pieces, min_piece: int) -> float:
    """Fraction of characters of `t` covered by any `piece` (>= min_piece chars)
    occurring as a substring of `t`. Order-independent, so a reversed/shuffled
    concatenation of our own lines still scores ~1.0 while a user command that
    only quotes one short bot line scores low."""
    if not t:
        return 0.0
    covered = bytearray(len(t))
    for s in pieces:
        if not s or len(s) < min_piece:
            continue
        start = 0
        while True:
            i = t.find(s, start)
            if i < 0:
                break
            for j in range(i, i + len(s)):
                covered[j] = 1
            start = i + 1
    return sum(covered) / len(t)
