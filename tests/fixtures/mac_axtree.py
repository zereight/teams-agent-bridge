"""A SANITIZED fake macOS Teams AX tree.

The structure (roles, subroles, nesting, date separators, both timestamp layout
variants, a link-preview card, an image-only message, '편집됨'/'전송됨' markers)
mirrors a real `bridge.py --dump-tree` of the self chat. All message BODIES are
fake placeholder text — no real company data is reproduced here.
"""
from typing import List, Optional


class Node:
    def __init__(self, role="", subrole="", title="", desc="", value="", ident="",
                 children=None, pos=None, size=None):
        self.a = {"AXRole": role, "AXSubrole": subrole, "AXTitle": title,
                  "AXDescription": desc, "AXValue": value, "AXIdentifier": ident,
                  "AXRoleDescription": ""}
        self.kids = children or []
        self.pos = pos      # (x, y) or None
        self.size = size    # (w, h) or None


class FakeMacAX:
    def attr(self, el, name):
        return el.a.get(name)

    def children(self, el):
        return el.kids

    def frame(self, el):
        """(x, y, w, h) if this node carries a position, else None (triggers the
        document-order fallback in ax_select.order_body_text)."""
        if getattr(el, "pos", None) is None or getattr(el, "size", None) is None:
            return None
        return (el.pos[0], el.pos[1], el.size[0], el.size[1])


def _static_at(v, x, y, w=50.0, h=16.0):
    n = _static(v)
    n.pos = (x, y)
    n.size = (w, h)
    return n


def reversed_multiline_body(lines):
    """A BODY group whose paragraph AXStaticText children are stored in REVERSED
    document order, but positioned so the correct visual order is top-to-bottom.
    Reproduces the Teams row that made the bridge read its own replies back
    shuffled. order_body_text must return the lines joined top-to-bottom."""
    n = len(lines)
    kids = []
    for i, text in enumerate(lines):              # i = visual line index (0 = top)
        # wrap each paragraph in a group, like real Teams; store in reversed order
        para = Node(role="AXGroup", children=[_static_at(text, x=10.0, y=100.0 + i * 20.0)])
        kids.append(para)
    kids.reverse()                                # <-- reversed tree order
    return Node(role="AXGroup", desc="(truncated preview)…", children=kids)


def _static(v):
    return Node(role="AXStaticText", value=v)


def _link_node(text, title="", x=None, y=None, w=60.0, h=16.0):
    """An AXLink whose visible text lives in a child AXStaticText (as Teams does).
    `title` sets the link element's own AXTitle to prove we never emit both the
    title and the child static text (no duplication)."""
    n = Node(role="AXLink", title=title, children=[_static(text)])
    if x is not None:
        n.pos = (x, y)
        n.size = (w, h)
    return n


def link_paragraph(with_frames=True):
    """A message body paragraph mirroring the live bug: static text, a URL link,
    static text. When frames exist Teams strands the AXLink LAST in tree order but
    its frame x belongs in the MIDDLE; order_body_text must place it inline. The
    link carries a bogus AXTitle to check de-duplication (title must NOT appear).
    Expected body (both paths): '1) curl -sI https://example.com 2) cat x 3) git push --dry-run'."""
    if with_frames:
        a = _static_at("1) curl -sI ", x=10.0, y=100.0, w=80.0)
        c = _static_at(" 2) cat x 3) git push --dry-run", x=300.0, y=100.0, w=200.0)
        lk = _link_node("https://example.com", title="example.com 링크", x=120.0, y=100.0)
        kids = [a, c, lk]                 # URL stranded last in tree order
    else:
        a = _static("1) curl -sI ")
        c = _static(" 2) cat x 3) git push --dry-run")
        lk = _link_node("https://example.com", title="example.com 링크")
        kids = [a, lk, c]                 # no frames -> tree order is visual order
    return Node(role="AXGroup", desc="(paragraph)", children=kids)


def glued_link_paragraph():
    """Static text with NO trailing space immediately followed by a link (no
    frames). order_body_text must insert a space so 'dry-runhttps' can't glue."""
    return Node(role="AXGroup", desc="(paragraph)", children=[
        _static("3) git push --dry-run"),
        _link_node("https://example.com"),
    ])


def _timegroup(desc, shown):
    return Node(role="AXGroup", subrole="AXTimeGroup", desc=desc, children=[_static(shown)])


def _sender(name="Alex Kim (Alex)"):
    return Node(role="AXGroup", children=[Node(role="AXGroup", children=[_static(name)])])


def _body_group(desc, tokens):
    kids = [Node(role="AXGroup", children=[_static(t)]) for t in tokens]
    return Node(role="AXGroup", desc=desc, children=kids)


def _appgroup(desc, body_group, extra_siblings=()):
    kids = [Node(role="AXButton", desc="추가 메시지 옵션"), body_group]
    kids.extend(extra_siblings)
    return Node(role="AXGroup", subrole="AXApplicationGroup", desc=desc, children=kids)


def _heading(title, static_value=None):
    return Node(role="AXHeading", title=title,
                children=[_static(static_value if static_value is not None else title)])


def _date_sep(label):
    return Node(role="AXHeading", title=label, children=[Node(role="AXGroup", children=[_static(label)])])


def _msg_variant_a(preview, ts_desc, ts_shown, body_desc, tokens, edited=False, extra=()):
    """Layout A: AXTimeGroup is a DIRECT child of the content group."""
    content_children = [_timegroup(ts_desc, ts_shown)]
    if edited:
        content_children.append(Node(role="AXGroup", children=[_static("편집됨")]))
    content_children.append(_sender())
    content_children.append(_appgroup(f"Alex Kim (Alex) {body_desc} {ts_desc}",
                                       _body_group(body_desc, tokens), extra))
    return [_heading(preview), Node(role="AXGroup", children=content_children)]


def _msg_variant_b(preview, ts_desc, ts_shown, body_desc, tokens, status=None):
    """Layout B (recent): AXTimeGroup under an extra wrapper; appgroup is its sibling."""
    wrapper = Node(role="AXGroup", children=[_timegroup(ts_desc, ts_shown), _sender()])
    app = _appgroup(f"Alex Kim (Alex) {('%s ' % status) if status else ''}{body_desc} {ts_desc}",
                    _body_group(body_desc, tokens))
    content_children = [wrapper, app]
    if status:
        content_children.append(Node(role="AXImage", desc=status))
    return [_heading(preview), Node(role="AXGroup", children=content_children)]


def build_tree(title="채팅 | Alex Kim (Alex)(나) | Microsoft Teams") -> Node:
    rows: List[Node] = []
    # variant A, long body truncated in heading/desc but full in tokens, edited
    rows += _msg_variant_a(
        "Alex Kim (Alex)이(가) 만든 FAKEDATA aaaa bbbb cccc dddd…",
        "2026년 8월 6일 17:54.", "8. 6. 17:54",
        "FAKEDATA aaaa bbbb cccc dddd eeee ffff gggg…",   # desc truncated with …
        ["FAKE", " ", "aaaa", "bbbb", "cccc", "dddd", "eeee", "ffff", "gggg"],
        edited=True)
    rows.append(_date_sep("8월 19일"))
    # variant A with a link preview card as an extra sibling (must be excluded)
    preview_card = Node(role="AXGroup", children=[
        Node(role="AXLink", desc="미리 보기", children=[
            _static("Preview Title"), _static("preview.example.com")])])
    rows += _msg_variant_a(
        "Alex Kim (Alex)이(가) 만든 report link http…",
        "2026년 8월 27일 12:54.", "8. 27. 12:54",
        "report link https://example.com/x",
        ["report link ", "https://example.com/x"],
        extra=(preview_card,))
    rows.append(_date_sep("오늘"))
    # image-only message: body group has no static text -> falls back to desc
    img_body = Node(role="AXGroup", desc="이미지",
                    children=[Node(role="AXGroup", children=[Node(role="AXImage", desc="이미지")])])
    rows += [_heading("Alex Kim (Alex)이(가) 만든 이미지"),
             Node(role="AXGroup", children=[_timegroup("오늘 14:31.", "14:31"), _sender(),
                                            _appgroup("Alex Kim (Alex) 이미지 오늘 14:31.", img_body)])]
    # recent short messages (variant B)
    rows += _msg_variant_b("Alex Kim (Alex)이(가) 만든 하이", "오늘 14:18.", "14:18", "하이", ["하이"])
    rows += _msg_variant_b("Alex Kim (Alex)이(가) 만든 hello", "오늘 14:20.", "14:20", "hello", ["hello"])
    # a bot reply that was previously posted; body starts with "(b)"
    rows += _msg_variant_b("Alex Kim (Alex)이(가) 만든 (b) 완료", "오늘 14:21.", "14:21",
                           "(b) 완료 (claude, 3초)", ["(b) 완료 (claude, 3초)"], status="전송됨")
    # a real command (variant B), with space tokens
    rows += _msg_variant_b("Alex Kim (Alex)이(가) 만든 로그인 테스트 돌려줘",
                           "오늘 14:22.", "14:22", "로그인 테스트 돌려줘",
                           ["로그인", " ", "테스트", " ", "돌려줘"], status="전송됨")

    msg_list = Node(role="AXGroup", desc="메시지 목록",
                    children=[Node(role="AXGroup", children=[Node(role="AXGroup", children=rows)])])
    compose = Node(role="AXTextArea", desc="메시지를 입력하세요.", value="메시지를 입력하세요.\n",
                   children=[Node(role="AXGroup", children=[_static("메시지를 입력하세요.")])])
    web = Node(role="AXWebArea", title=title, children=[msg_list, compose])
    win = Node(role="AXWindow", subrole="AXStandardWindow", title=title, children=[web])
    app = Node(role="AXApplication", title="Microsoft Teams", children=[win])
    app.a["AXFocusedWindow"] = win
    app.a["AXMainWindow"] = win
    app.a["AXWindows"] = [win]
    return app
