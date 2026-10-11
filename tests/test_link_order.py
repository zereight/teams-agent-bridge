"""AXLink inline ordering + no-glue / no-duplicate (patch9)."""
import ax_select as A
from tests.fixtures.mac_axtree import (
    FakeMacAX, link_paragraph, glued_link_paragraph,
)

EXPECTED = "1) curl -sI https://example.com 2) cat x 3) git push --dry-run"


def test_link_inline_with_frames():
    ax = FakeMacAX()
    body = A.order_body_text(ax, link_paragraph(with_frames=True), body_join="")
    assert body == EXPECTED, body
    # link title must not be duplicated into the body
    assert "링크" not in body


def test_link_inline_without_frames():
    ax = FakeMacAX()
    body = A.order_body_text(ax, link_paragraph(with_frames=False), body_join="")
    assert body == EXPECTED, body
    assert "링크" not in body


def test_link_not_glued_to_neighbor():
    ax = FakeMacAX()
    body = A.order_body_text(ax, glued_link_paragraph(), body_join="")
    assert body == "3) git push --dry-run https://example.com", body
    assert "dry-runhttps" not in body


def test_bot_echo_still_matches_after_link_spacing():
    # The extra display space must not break echo detection, which normalizes
    # whitespace away. A bot line and its read-back must still compare equal.
    import state
    ax = FakeMacAX()
    read_back = A.order_body_text(ax, link_paragraph(with_frames=True), body_join="")
    assert state.normalize_text(read_back) == state.normalize_text(EXPECTED)
