"""Parse the loose, human timestamps Microsoft Teams shows next to messages, so
the bridge can REFUSE to execute anything older than its own start time.

This is a hard safety gate against history replay: a scrolled-in or migrated old
message (even an old 'ㅇㅇ' that could approve a push) must never run.

parse_teams_timestamp(raw, now) returns a naive *local* datetime, or None when
the label is unparseable. The design FAILS CLOSED: an ambiguous date-only label
resolves to 00:00 of that day (so a same-day date-only label looks old), and an
unparseable label returns None (the caller treats None as old unless the message
appeared after the startup baseline).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

_WD_KO = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_WD_EN = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


def _extract_time(s: str):
    """Return (hour, minute, remainder_without_time) or (None, None, s)."""
    # Korean 오전/오후 h:mm
    m = re.search(r"(오전|오후)\s*(\d{1,2}):(\d{2})", s)
    if m:
        h = int(m.group(2)) % 12
        if m.group(1) == "오후":
            h += 12
        return h, int(m.group(3)), s[:m.start()] + s[m.end():]
    # English h:mm AM/PM
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])", s)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3).lower() == "pm":
            h += 12
        return h, int(m.group(2)), s[:m.start()] + s[m.end():]
    # 24-hour h:mm
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m:
        return int(m.group(1)), int(m.group(2)), s[:m.start()] + s[m.end():]
    return None, None, s


def _extract_date(rem: str, now: datetime):
    """Return a date (datetime at 00:00) from the non-time remainder, or None."""
    low = rem.lower()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # YYYY. M. D.
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", rem)
    if m:
        try:
            return today.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)))
        except ValueError:
            return None
    # M월 D일  (check before the single-char weekday test so '10월' isn't a weekday)
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", rem)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = today.replace(month=month, day=day)
        except ValueError:
            return None
        if d > today:                      # no year shown -> assume the most recent past
            d = d.replace(year=d.year - 1)
        return d
    # M/D or M-D (numeric, no year)
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})\b", rem)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = today.replace(month=month, day=day)
        except ValueError:
            return None
        if d > today:
            d = d.replace(year=d.year - 1)
        return d
    # relative words
    if "오늘" in rem or "today" in low:
        return today
    if "어제" in rem or "yesterday" in low:
        return today - timedelta(days=1)
    if "그저께" in rem or "그제" in rem:
        return today - timedelta(days=2)
    # weekday names (KO requires the '요일' suffix to avoid matching '월' in '10월')
    wd = None
    for k, v in _WD_KO.items():
        if k + "요일" in rem:
            wd = v
            break
    if wd is None:
        for token in re.findall(r"[A-Za-z]+", low):
            if token in _WD_EN:
                wd = _WD_EN[token]
                break
    if wd is not None:
        delta = (today.weekday() - wd) % 7      # most recent past occurrence (today if same)
        return today - timedelta(days=delta)
    return None


def parse_teams_timestamp(raw: str, now: Optional[datetime] = None) -> Optional[datetime]:
    if now is None:
        now = datetime.now()
    if not raw:
        return None
    s = raw.strip()
    # drop a single trailing period Teams likes to add ('어제 16:14.')
    s = s.rstrip(".").strip()
    if not s:
        return None
    hour, minute, rem = _extract_time(s)
    date = _extract_date(rem, now)
    if date is None and hour is None:
        return None                              # unparseable
    if hour is None:
        # date only -> 00:00 (fail closed: a same-day date-only label looks old)
        return date
    if date is None:
        date = now.replace(hour=0, minute=0, second=0, microsecond=0)   # time only -> today
    return date.replace(hour=hour, minute=minute, second=0, microsecond=0)
