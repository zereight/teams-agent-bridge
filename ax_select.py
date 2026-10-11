"""Pure helpers for walking a macOS Accessibility (AX) tree with configurable selectors.

No pyobjc import here, so it is unit-tested on Linux with a fake tree. The
real tree access goes through an *adapter* object with two methods:

    adapter.attr(element, "AXRole") -> value or None
    adapter.children(element)        -> list of elements

Selector dict keys (all optional, all must match):
    role, subrole, identifier          exact match
    title_re, description_re, value_re regex search on that attribute
    role_description_re                regex search on AXRoleDescription
    text_re                            regex search on title|description|value (any)
    depth                              exact depth relative to the search root (1 = direct child)
    index                              which match to pick for find_one (default 0, -1 = last)
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

TEXT_ATTRS = ("AXValue", "AXTitle", "AXDescription")
DUMP_ATTRS = ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXIdentifier")


def as_text(v: Any) -> str:
    """Coerce an AX attribute value to text; non-text values (elements, AXValue structs) -> ''."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def node_info(ax, el) -> Dict[str, str]:
    return {k: as_text(ax.attr(el, k)) for k in
            ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue",
             "AXIdentifier", "AXRoleDescription")}


_REGEX_KEYS = {
    "title_re": ("AXTitle",),
    "description_re": ("AXDescription",),
    "value_re": ("AXValue",),
    "role_description_re": ("AXRoleDescription",),
    "text_re": ("AXTitle", "AXDescription", "AXValue"),
}
_EXACT_KEYS = {"role": "AXRole", "subrole": "AXSubrole", "identifier": "AXIdentifier"}
_META_KEYS = {"depth", "index"}


def validate_selector(sel: Dict[str, Any]) -> None:
    unknown = set(sel) - set(_REGEX_KEYS) - set(_EXACT_KEYS) - _META_KEYS
    if unknown:
        raise ValueError(f"알 수 없는 선택자 키: {sorted(unknown)}")


def matches(info: Dict[str, str], sel: Dict[str, Any]) -> bool:
    for key, attr in _EXACT_KEYS.items():
        if key in sel and info.get(attr, "") != str(sel[key]):
            return False
    for key, attrs in _REGEX_KEYS.items():
        if key in sel:
            rx = re.compile(sel[key])
            if not any(rx.search(info.get(a, "")) for a in attrs):
                return False
    return True


def walk(ax, root, max_depth: int = 60, max_nodes: int = 20000) -> Iterator[Tuple[Any, int]]:
    """Depth-first, document order. Yields (element, depth); root has depth 0."""
    stack: List[Tuple[Any, int]] = [(root, 0)]
    seen = 0
    while stack:
        el, d = stack.pop()
        yield el, d
        seen += 1
        if seen >= max_nodes:
            return
        if d >= max_depth:
            continue
        kids = ax.children(el) or []
        for k in reversed(list(kids)):
            stack.append((k, d + 1))


def find_all(ax, root, sel: Dict[str, Any], max_depth: int = 60, max_nodes: int = 20000) -> List[Any]:
    validate_selector(sel)
    want_depth = sel.get("depth")
    out = []
    for el, d in walk(ax, root, max_depth if want_depth is None else min(max_depth, int(want_depth)),
                      max_nodes):
        if d == 0:
            continue  # never match the root itself
        if want_depth is not None and d != int(want_depth):
            continue
        if matches(node_info(ax, el), sel):
            out.append(el)
    return out


def find_one(ax, root, sel: Optional[Dict[str, Any]], max_depth: int = 60,
             max_nodes: int = 20000) -> Optional[Any]:
    if not sel:
        return None
    found = find_all(ax, root, sel, max_depth, max_nodes)
    idx = int(sel.get("index", 0))
    try:
        return found[idx]
    except IndexError:
        return None


def own_text(ax, el) -> str:
    for a in TEXT_ATTRS:
        t = as_text(ax.attr(el, a)).strip()
        if t:
            return t
    return ""


def element_text(ax, el, mode: str = "children", max_nodes: int = 2000) -> str:
    """Text of a message element.

    self      first non-empty of AXValue / AXTitle / AXDescription of the element
    children  AXStaticText values below the element joined by newlines (web content)
    all       self + children (duplicates removed)
    """
    parts: List[str] = []
    if mode in ("self", "all"):
        t = own_text(ax, el)
        if t:
            parts.append(t)
    if mode in ("children", "all"):
        for node, d in walk(ax, el, max_depth=40, max_nodes=max_nodes):
            if d == 0:
                continue
            if as_text(ax.attr(node, "AXRole")) == "AXStaticText":
                t = own_text(ax, node)
                if t and t not in parts:
                    parts.append(t)
    return "\n".join(parts)


def _short(s: str, n: int = 120) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 1] + "…"


def dump_tree(ax, root, max_depth: int = 60, max_nodes: int = 20000) -> str:
    lines = []
    for el, d in walk(ax, root, max_depth, max_nodes):
        info = node_info(ax, el)
        bits = [info["AXRole"] or "?"]
        if info["AXSubrole"]:
            bits.append(f"[{info['AXSubrole']}]")
        for key, label in (("AXTitle", "title"), ("AXDescription", "desc"),
                           ("AXValue", "value"), ("AXIdentifier", "id")):
            if info[key]:
                bits.append(f'{label}="{_short(info[key])}"')
        lines.append("  " * d + " ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# macOS Teams message extraction (strategy="timegroup")
#
# Real new-Teams (com.microsoft.teams2) message rows have this shape inside the
# "메시지 목록" container (verified against a real --dump-tree):
#
#   AXHeading title="<sender>이(가) 만든 <TRUNCATED preview>…"   <- ignore (truncated)
#   AXGroup                                                      <- content group
#     [AXGroup]?                                                 <- optional wrapper (recent msgs)
#       AXGroup [AXTimeGroup] desc="2026년 8월 6일 17:54."        <- FULL timestamp (desc)
#       AXGroup > AXGroup > AXStaticText value="<sender>"        <- sender
#     AXGroup [AXApplicationGroup] desc="<sender> <body…> <ts>"  <- per-message anchor
#       AXButton desc="추가 메시지 옵션"
#       AXGroup desc="<body (maybe truncated …)>"                <- BODY group
#         ... AXStaticText value="..."                           <- FULL body tokens
#       [AXGroup ...link preview card...]?                       <- sibling, excluded
#   AXHeading title="8월 19일" / "오늘" / "화요일"                 <- date separator, ignore
#
# We anchor on AXApplicationGroup (exactly one per message), take the body from
# its first child AXGroup (concatenating AXStaticText descendants = FULL text,
# never the truncated heading/desc), and read the timestamp + sender from the
# enclosing content group (the AXApplicationGroup's parent), which also holds
# the AXTimeGroup. This handles both layout variants.

SEP = "\x1f"


def subrole_of(ax, el) -> str:
    return as_text(ax.attr(el, "AXSubrole"))


def role_of(ax, el) -> str:
    return as_text(ax.attr(el, "AXRole"))


def _frame(ax, el):
    """(x, y, w, h) of an element if the adapter exposes frames, else None.

    The pure/test adapter may provide a frame(el) method; MacAX does. We never
    read AXPosition/AXSize structs through attr() because they are opaque."""
    fn = getattr(ax, "frame", None)
    if fn is None:
        return None
    try:
        fr = fn(el)
    except Exception:
        return None
    if not fr or len(fr) < 2:
        return None
    return fr


def _link_text(ax, el, max_nodes: int = 400) -> str:
    """Display text of an AXLink as ONE string. Prefer the concatenated
    AXStaticText descendants (Teams puts the visible URL/label there); fall back
    to the link's own AXValue/AXTitle/AXDescription. Never return both, so a link
    title and its child static text are not duplicated in the body."""
    statics = []
    seen = 0
    stack = [el]
    first = True
    while stack and seen < max_nodes:
        node = stack.pop()
        seen += 1
        if not first and role_of(ax, node) == "AXStaticText":
            sv = as_text(ax.attr(node, "AXValue")) or own_text(ax, node)
            if sv:
                statics.append(sv)
        first = False
        for k in reversed(ax.children(node) or []):
            stack.append(k)
    joined = "".join(statics).strip()
    return joined or own_text(ax, el)


def _text_segments(ax, el, skip_subroles=(), max_nodes: int = 4000):
    """[(value, frame, is_link)] for every AXStaticText and AXLink under `el`, in
    DOCUMENT (preorder) order, not descending into skip_subroles subtrees.

    An AXLink is emitted as a SINGLE segment carrying the link element's own
    frame (so it can be placed inline by position) and its combined text; we do
    not descend into the link, so its child AXStaticText (the URL) is never
    collected a second time nor stranded at the end of the row."""
    skip = set(skip_subroles)
    out = []
    seen = 0
    stack = [el]
    while stack and seen < max_nodes:
        node = stack.pop()
        seen += 1
        r = role_of(ax, node)
        if r == "AXLink":
            v = _link_text(ax, node)
            if v:
                out.append((v, _frame(ax, node), True))
            continue                      # do NOT descend into the link
        if r == "AXStaticText":
            v = as_text(ax.attr(node, "AXValue")) or own_text(ax, node)
            out.append((v, _frame(ax, node), False))
        kids = [k for k in (ax.children(node) or []) if subrole_of(ax, k) not in skip]
        for k in reversed(kids):          # push reversed so we POP in document order
            stack.append(k)
    return out


# Back-compat: old callers expect (value, frame) pairs for static text only.
def _static_segments(ax, el, skip_subroles=(), max_nodes: int = 4000):
    return [(v, fr) for v, fr, _lk in _text_segments(ax, el, skip_subroles, max_nodes)]


def _join_tokens(items, join: str) -> str:
    """Join [(value, is_link)] with `join`, but guarantee a space between a link
    and an adjacent token so an empty join can't glue 'dry-runhttps://...'. """
    parts = []
    for idx, (v, lk) in enumerate(items):
        if idx > 0:
            prev_v, prev_lk = items[idx - 1]
            glue = join
            if (lk or prev_lk) and not glue.strip():
                left_ws = bool(parts and parts[-1][-1:] in (" ", "\t", "\n"))
                right_ws = v[:1] in (" ", "\t", "\n")
                if not left_ws and not right_ws:
                    glue = " "
            parts.append(glue)
        parts.append(v)
    return "".join(parts)


def collect_static_text(ax, el, join: str = "", skip_subroles=(), max_nodes: int = 4000) -> str:
    """Concatenate AXStaticText (and AXLink) values under `el` in document order,
    not descending into skip_subroles subtrees."""
    segs = _text_segments(ax, el, skip_subroles, max_nodes)
    return _join_tokens([(v, lk) for v, _, lk in segs if v], join)


def order_body_text(ax, el, body_join: str = "", skip_subroles=(), line_tol: float = 6.0,
                    max_nodes: int = 4000) -> str:
    """Full body text of a message, in VISUAL order.

    When every segment has a frame, segments are grouped into lines by y
    (within line_tol px), ordered left-to-right by x within a line and
    top-to-bottom between lines; lines are joined with '\n' and intra-line
    tokens with body_join. This fixes Teams rows whose paragraph AXStaticText
    children come back in a reversed/shuffled tree order. Without frames we fall
    back to document order joined by body_join."""
    segs = [(v, fr, lk) for v, fr, lk in _text_segments(ax, el, skip_subroles, max_nodes) if v]
    if not segs:
        return ""
    if not all(fr is not None for _, fr, _ in segs):
        # No frames: trust document/tree order (links already placed inline as a
        # single segment), padding links so adjacent tokens can't glue.
        return _join_tokens([(v, lk) for v, _, lk in segs], body_join)
    items = sorted(segs, key=lambda s: (s[1][1], s[1][0]))   # y, then x
    lines = []
    cur = []            # list of (x, value, is_link)
    ref_y = None
    for v, fr, lk in items:
        x, y = fr[0], fr[1]
        if ref_y is None or abs(y - ref_y) <= line_tol:
            cur.append((x, v, lk))
            if ref_y is None:
                ref_y = y
        else:
            lines.append(_join_tokens(
                [(val, l) for _, val, l in sorted(cur, key=lambda t: t[0])], body_join))
            cur = [(x, v, lk)]
            ref_y = y
    if cur:
        lines.append(_join_tokens(
            [(val, l) for _, val, l in sorted(cur, key=lambda t: t[0])], body_join))
    return "\n".join(lines)


def _first_timegroup_desc(ax, el, time_subrole: str, max_nodes: int = 4000) -> str:
    for node, d in walk(ax, el, max_depth=40, max_nodes=max_nodes):
        if d == 0:
            continue
        if subrole_of(ax, node) == time_subrole:
            return as_text(ax.attr(node, "AXDescription")).strip()
    return ""


def _body_group(ax, app_el):
    """The first child AXGroup of the AXApplicationGroup (the body; skips the AXButton)."""
    for k in ax.children(app_el) or []:
        if role_of(ax, k) == "AXGroup":
            return k
    return None


def extract_messages_timegroup(ax, container, *, app_subrole="AXApplicationGroup",
                               time_subrole="AXTimeGroup", status_words=(),
                               body_join="", max_depth=60, max_nodes=20000):
    """Return list of dicts {text, timestamp, sender, raw} in document order.

    Pure: works with any adapter exposing attr(el, name) and children(el).
    """
    status = {s.strip() for s in status_words}
    out = []
    for node, d in walk(ax, container, max_depth, max_nodes):
        for child in ax.children(node) or []:
            if subrole_of(ax, child) != app_subrole:
                continue
            bodygrp = _body_group(ax, child)
            if bodygrp is None:
                continue
            body = order_body_text(ax, bodygrp, body_join).strip()
            if not body:
                body = as_text(ax.attr(bodygrp, "AXDescription")).strip()
            if not body:
                continue
            ts = _first_timegroup_desc(ax, node, time_subrole)
            sender = ""
            for cand in collect_static_text(
                    ax, node, SEP, skip_subroles=(app_subrole, time_subrole)).split(SEP):
                c = cand.strip()
                if c and c not in status:
                    sender = c
                    break
            out.append({"text": body, "timestamp": ts, "sender": sender,
                        "raw": as_text(ax.attr(child, "AXDescription"))})
    return out
