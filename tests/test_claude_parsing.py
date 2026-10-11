"""BUG fix: tolerate array / single-object / JSONL claude output and never dump init."""
from agent_runner import parse_claude_output, iter_json_values
from tests.fixtures.claude_json import (ARRAY_OUTPUT, SINGLE_OBJECT, JSONL_OUTPUT,
                                        ERROR_OUTPUT, NO_RESULT)


def test_array_output_picks_last_result():
    p = parse_claude_output(ARRAY_OUTPUT)
    assert p is not None
    assert p["result"] == "테스트 3개 통과했어요."
    assert p["is_error"] is False
    assert p["num_turns"] == 5
    assert abs(p["total_cost_usd"] - 0.1234) < 1e-9
    assert p["session_id"] == "sess-abc-123"
    # the init/tools dump is never surfaced
    assert "tools" not in p and "init" not in str(p)


def test_single_object():
    p = parse_claude_output(SINGLE_OBJECT)
    assert p["result"] == "안녕하세요." and p["session_id"] == "sess-9" and not p["is_error"]


def test_jsonl_with_control_char():
    p = parse_claude_output(JSONL_OUTPUT)
    assert p["result"] == "줄단위 OK" and p["session_id"] == "s7" and p["num_turns"] == 2


def test_error_result():
    p = parse_claude_output(ERROR_OUTPUT)
    assert p is not None and p["is_error"] is True and p["session_id"] == "s-err"


def test_no_result_returns_none():
    assert parse_claude_output(NO_RESULT) is None
    assert parse_claude_output("") is None
    assert parse_claude_output("not json at all") is None


def test_iter_handles_array_and_jsonl():
    assert len(iter_json_values(ARRAY_OUTPUT)) == 1          # one array value
    assert len(iter_json_values(JSONL_OUTPUT)) == 3          # three line values
