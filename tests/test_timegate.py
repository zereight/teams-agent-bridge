"""Unit tests for Teams timestamp parsing (the history-replay time gate)."""
from datetime import datetime

import pytest

from timegate import parse_teams_timestamp as P

NOW = datetime(2026, 10, 11, 7, 0, 51)   # Sunday


@pytest.mark.parametrize("raw,expected", [
    ("어제 16:14.", datetime(2026, 10, 10, 16, 14)),
    ("오늘 06:30", datetime(2026, 10, 11, 6, 30)),
    ("오늘 오후 4:14", datetime(2026, 10, 11, 16, 14)),
    ("오전 9:05", datetime(2026, 10, 11, 9, 5)),
    ("2026. 10. 9.", datetime(2026, 10, 9, 0, 0)),
    ("10월 9일 16:25", datetime(2026, 10, 9, 16, 25)),
    ("금요일 16:14", datetime(2026, 10, 9, 16, 14)),
    ("Yesterday 4:14 PM", datetime(2026, 10, 10, 16, 14)),
    ("Today 7:05 AM", datetime(2026, 10, 11, 7, 5)),
    ("Mon 8:00 AM", datetime(2026, 10, 5, 8, 0)),
    ("16:20", datetime(2026, 10, 11, 16, 20)),
])
def test_parse_known_formats(raw, expected):
    assert P(raw, NOW) == expected


@pytest.mark.parametrize("raw", ["", "뭐라고", "nonsense label", "방금"])
def test_unparseable_returns_none(raw):
    assert P(raw, NOW) is None


def test_date_only_fails_closed_to_midnight():
    # a bare month/day resolves to 00:00 so a same-day date-only label looks old
    assert P("10월 11일", NOW) == datetime(2026, 10, 11, 0, 0)
