"""Pure role logic: parsing, persistence, allow sanitising, handoff detection."""
import pytest

from roles import (Role, RoleStore, parse_role_add, sanitize_allowed, find_handoffs)


def test_parse_role_add_defaults_claude():
    name, agent, instr = parse_role_add("dev 코드 수정 담당, 테스트는 하지 마")
    assert name == "dev" and agent == "claude" and instr.startswith("코드 수정")


def test_parse_role_add_explicit_agent():
    name, agent, instr = parse_role_add("qa agent=codex 테스트만 돌리고 코드는 고치지 마")
    assert name == "qa" and agent == "codex" and instr == "테스트만 돌리고 코드는 고치지 마"


def test_parse_role_add_rejects_bad():
    with pytest.raises(ValueError):
        parse_role_add("")
    with pytest.raises(ValueError):
        parse_role_add("onlyname")          # no instructions
    with pytest.raises(ValueError):
        parse_role_add("a!b 지침")          # '!' not allowed in a role name


def test_sanitize_allowed_never_looser_than_disallow():
    allowed = ["Read", "Edit", "Bash", "Bash(git push:*)"]
    out = sanitize_allowed(allowed, ["Bash"])
    assert "Read" in out and "Edit" in out
    assert "Bash" not in out and "Bash(git push:*)" not in out  # head collides with disallow


def test_find_handoffs():
    text = "작업 끝났어요.\n@qa 테스트 부탁해\n@ghost 안녕"
    hs = find_handoffs(text, ["dev", "qa"])
    assert ("qa", "테스트 부탁해", True) in hs
    assert ("ghost", "안녕", False) in hs
    assert find_handoffs("@qa", ["qa"]) == []     # no message after name


def test_rolestore_roundtrip(tmp_path):
    p = str(tmp_path / "roles.json")
    st = RoleStore(p)
    st.add(Role(name="dev", instructions="코드 수정", agent="claude"))
    st.add(Role(name="qa", instructions="테스트", agent="codex"))
    assert st.set_default("dev")
    st.set_session("dev", "sess-1")

    st2 = RoleStore(p)                 # reload from disk
    assert st2.default_role == "dev"
    assert st2.get("DEV").session_id == "sess-1"   # case-insensitive lookup
    assert set(st2.names()) == {"dev", "qa"}
    assert st2.remove("dev") and not st2.exists("dev")
    assert st2.default_role == ""      # default cleared when its role is removed
