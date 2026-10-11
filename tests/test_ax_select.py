"""Tests for the pure AX-tree selector logic using a fake tree (no pyobjc)."""
import ax_select


class Node:
    def __init__(self, role="", title="", desc="", value="", subrole="", ident="",
                 roledesc="", children=None):
        self.attrs = {"AXRole": role, "AXTitle": title, "AXDescription": desc,
                      "AXValue": value, "AXSubrole": subrole, "AXIdentifier": ident,
                      "AXRoleDescription": roledesc}
        self.kids = children or []


class FakeAX:
    def attr(self, el, name):
        return el.attrs.get(name)

    def children(self, el):
        return el.kids


def sample():
    return Node(role="AXWindow", title="Microsoft Teams", children=[
        Node(role="AXStaticText", value="Alex Kim (You)"),               # chat header
        Node(role="AXList", ident="message-pane", children=[
            Node(role="AXGroup", children=[Node(role="AXStaticText", value="안녕")]),
            Node(role="AXGroup", children=[
                Node(role="AXStaticText", value="(b) 완료 (claude)"),
            ]),
            Node(role="AXGroup", children=[
                Node(role="AXStaticText", value="테스트"),
                Node(role="AXStaticText", value="돌려줘"),
            ]),
        ]),
        Node(role="AXTextArea", desc="메시지 입력"),
    ])


AX = FakeAX()


def test_find_one_by_role_and_identifier():
    root = sample()
    lst = ax_select.find_one(AX, root, {"role": "AXList", "identifier": "message-pane"})
    assert lst is not None and lst.attrs["AXIdentifier"] == "message-pane"


def test_find_all_messages_and_text_children():
    root = sample()
    lst = ax_select.find_one(AX, root, {"role": "AXList"})
    items = ax_select.find_all(AX, lst, {"role": "AXGroup", "depth": 1})
    assert len(items) == 3
    texts = [ax_select.element_text(AX, it, "children") for it in items]
    assert texts[0] == "안녕"
    assert texts[1].startswith("(b)")
    assert texts[2] == "테스트\n돌려줘"


def test_regex_selectors():
    root = sample()
    hdr = ax_select.find_one(AX, root, {"role": "AXStaticText", "value_re": r"\(You\)"})
    assert "(You)" in hdr.attrs["AXValue"]
    box = ax_select.find_one(AX, root, {"role": "AXTextArea", "description_re": "(?i)메시지"})
    assert box is not None
    assert ax_select.find_one(AX, root, {"text_re": "돌려줘"}) is not None


def test_depth_and_index():
    root = sample()
    direct = ax_select.find_all(AX, root, {"role": "AXStaticText", "depth": 1})
    assert len(direct) == 1 and direct[0].attrs["AXValue"].endswith("(You)")
    last_group = ax_select.find_one(AX, root, {"role": "AXGroup", "index": -1})
    assert ax_select.element_text(AX, last_group) == "테스트\n돌려줘"


def test_no_match_returns_none():
    assert ax_select.find_one(AX, sample(), {"role": "AXNope"}) is None
    assert ax_select.find_one(AX, sample(), None) is None


def test_matches_requires_all_conditions():
    info = {"AXRole": "AXGroup", "AXTitle": "hello world", "AXDescription": "", "AXValue": ""}
    assert ax_select.matches(info, {"role": "AXGroup", "title_re": "hello"})
    assert not ax_select.matches(info, {"role": "AXGroup", "title_re": "bye"})
    assert not ax_select.matches(info, {"role": "AXButton"})


def test_validate_selector_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        ax_select.validate_selector({"rolee": "AXGroup"})
    ax_select.validate_selector({"role": "AXGroup", "text_re": "x", "depth": 2, "index": 0})


def test_as_text_coerces():
    assert ax_select.as_text(None) == ""
    assert ax_select.as_text(True) == ""         # AXFocused booleans are not text
    assert ax_select.as_text(3) == "3"
    assert ax_select.as_text("hi") == "hi"
    assert ax_select.as_text(Node()) == ""        # elements/structs -> empty


def test_walk_respects_max_nodes():
    deep = Node(role="A")
    cur = deep
    for _ in range(50):
        nxt = Node(role="A")
        cur.kids = [nxt]
        cur = nxt
    seen = list(ax_select.walk(AX, deep, max_depth=100, max_nodes=10))
    assert len(seen) == 10


def test_dump_tree_shape():
    out = ax_select.dump_tree(AX, sample())
    assert "AXWindow" in out and 'value="Alex Kim (You)"' in out
    assert "  AXList" in out  # indentation shows depth
