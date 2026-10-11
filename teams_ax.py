"""Backend A (macOS): Accessibility API (AXUIElement via pyobjc).

Finds the Microsoft Teams app by bundle id (default com.microsoft.teams2 =
new Teams), reads the message list from the AX tree, and types replies with
clipboard + Cmd+V + Return.

Every selector lives in config.yaml (ax: ...) because the Teams AX tree
differs between versions and languages. Use `python bridge.py --dump-tree`
to print the tree (role, subrole, title, description, value, identifier).

Requires: System Settings > Privacy & Security > Accessibility -> allow the
app that runs Python (Terminal / iTerm / VS Code ...).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import ax_select
from backend_base import BackendError, ChatBackend, ChatGuardError, ChatMessage

try:
    import ApplicationServices as AS  # pyobjc-framework-ApplicationServices
except Exception:  # pragma: no cover - Linux / Windows
    AS = None


class MacAX:
    """Thin adapter over the pyobjc AX functions (same shape as the test fake)."""

    def __init__(self, messaging_timeout: float = 2.0):
        if AS is None:
            raise BackendError("pyobjc-framework-ApplicationServices 가 없어요 (pip install -r requirements.txt)")
        self.timeout = messaging_timeout

    def is_trusted(self) -> bool:
        return bool(AS.AXIsProcessTrusted())

    def app_element(self, pid: int):
        el = AS.AXUIElementCreateApplication(pid)
        try:
            AS.AXUIElementSetMessagingTimeout(el, float(self.timeout))
        except Exception:
            pass
        return el

    def attr(self, el, name: str):
        try:
            err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
        except Exception:
            return None
        return val if err == 0 else None

    def children(self, el) -> List[Any]:
        v = self.attr(el, "AXChildren")
        return list(v) if v else []

    def set_attr(self, el, name: str, value) -> bool:
        try:
            return AS.AXUIElementSetAttributeValue(el, name, value) == 0
        except Exception:
            return False

    def frame(self, el) -> Optional[Tuple[float, float, float, float]]:
        pos, size = self.attr(el, "AXPosition"), self.attr(el, "AXSize")
        if pos is None or size is None:
            return None
        pt_type = getattr(AS, "kAXValueCGPointType", getattr(AS, "kAXValueTypeCGPoint", 1))
        sz_type = getattr(AS, "kAXValueCGSizeType", getattr(AS, "kAXValueTypeCGSize", 2))
        try:
            ok1, p = AS.AXValueGetValue(pos, pt_type, None)
            ok2, s = AS.AXValueGetValue(size, sz_type, None)
        except Exception:
            return None
        if not (ok1 and ok2):
            return None
        return float(p.x), float(p.y), float(s.width), float(s.height)


class TeamsAXBackend(ChatBackend):
    def __init__(self, cfg: Dict[str, Any], ax=None, desktop=None, sleep=time.sleep):
        super().__init__()
        self.cfg = cfg
        self.a = cfg["ax"]
        self.bundle_id = self.a["bundle_id"]
        self.title_re = re.compile(cfg["self_chat"]["title_regex"])
        self.msg_re = re.compile(self.a["message_regex"], re.S) if self.a.get("message_regex") else None
        self.sleep = sleep
        if ax is None:
            ax = MacAX(self.a.get("messaging_timeout_sec", 2.0))
            if not ax.is_trusted():
                raise BackendError(
                    "손쉬운 사용(Accessibility) 권한이 없어요. 시스템 설정 > 개인정보 보호 및 보안 > "
                    "손쉬운 사용에서 이 터미널 앱을 켜 주세요.")
        if desktop is None:
            from mac_desktop import MacDesktop
            desktop = MacDesktop()
        self.ax = ax
        self.desktop = desktop
        self._manual_ax_done = False
        for key in ("window", "chat_header", "message_list", "message_item", "compose_box"):
            if self.a.get(key):
                ax_select.validate_selector(self.a[key])

    # ------------------------------------------------------------ helpers
    def _limits(self) -> Tuple[int, int]:
        return int(self.a.get("max_depth", 60)), int(self.a.get("max_nodes", 20000))

    def _app(self):
        pid = self.desktop.pid_for(self.bundle_id)
        if not pid:
            raise BackendError(f"Teams({self.bundle_id})가 실행 중이 아니에요")
        app = self.ax.app_element(pid)
        if self.a.get("enable_manual_accessibility") and not self._manual_ax_done:
            # Chromium/Electron-style apps only build their web AX tree when asked.
            self.ax.set_attr(app, "AXManualAccessibility", True)
            self._manual_ax_done = True
        return app

    def _window(self):
        app = self._app()
        sel = self.a.get("window") or {}
        cands = []
        for key in ("AXFocusedWindow", "AXMainWindow"):
            w = self.ax.attr(app, key)
            if w is not None:
                cands.append(w)
        cands += list(self.ax.attr(app, "AXWindows") or [])
        for w in cands:
            if not sel or ax_select.matches(ax_select.node_info(self.ax, w), sel):
                return w
        raise BackendError("Teams 창을 못 찾았어요 (ax.window 선택자 확인)")

    def _title_text(self, win) -> str:
        parts = [ax_select.as_text(self.ax.attr(win, "AXTitle"))]
        if self.a.get("chat_header"):
            md, mn = self._limits()
            hdr = ax_select.find_one(self.ax, win, self.a["chat_header"], md, mn)
            parts.append(ax_select.own_text(self.ax, hdr) if hdr is not None else "")
        return " | ".join(parts)

    def _verify(self, win) -> str:
        title = self._title_text(win)
        if not self.title_re.search(title):
            raise ChatGuardError(f"나와의 채팅이 아니에요: {title!r}")
        return title

    def _verify_frontmost(self) -> None:
        front = self.desktop.frontmost_bundle_id()
        if front != self.bundle_id:
            raise ChatGuardError(f"맨 앞 앱이 Teams가 아니에요: {front!r}")

    # ------------------------------------------------------------ interface
    def _activate_and_verify(self) -> str:
        self.desktop.activate(self.bundle_id)
        self.sleep(float(self.a.get("activate_delay_sec", 0.6)))
        self._verify_frontmost()
        return self._verify(self._window())

    def focus_self_chat(self) -> str:
        # The AX tree can be read while Teams is in the background, so by
        # default we only bring Teams to the front when we actually type.
        if self.a.get("activate_for_read", False):
            return self._activate_and_verify()
        return self._verify(self._window())

    def read_visible_messages(self) -> List[ChatMessage]:
        if not self.a.get("message_list"):
            # reading the whole window would also pick up chat-list previews of
            # OTHER conversations, which must never become commands
            raise BackendError("ax.message_list 선택자를 먼저 설정해 주세요 (--dump-tree 참고)")
        win = self._window()
        self._verify(win)
        md, mn = self._limits()
        container = ax_select.find_one(self.ax, win, self.a["message_list"], md, mn)
        if container is None:
            raise BackendError("메시지 목록(ax.message_list)을 못 찾았어요")
        if self.a.get("message_strategy", "generic") == "timegroup":
            return self._read_timegroup(container, md, mn)
        return self._read_generic(container, md, mn)

    def _read_timegroup(self, container, md: int, mn: int) -> List[ChatMessage]:
        rows = ax_select.extract_messages_timegroup(
            self.ax, container,
            app_subrole=self.a.get("app_group_subrole", "AXApplicationGroup"),
            time_subrole=self.a.get("time_group_subrole", "AXTimeGroup"),
            status_words=self.a.get("status_words", []),
            body_join=self.a.get("body_join", ""),
            max_depth=md, max_nodes=mn)
        out: List[ChatMessage] = []
        for r in rows[-int(self.a.get("max_messages", 30)):]:
            out.append(ChatMessage(text=r["text"], timestamp=r["timestamp"], raw=r["raw"],
                                   extra={"sender": r["sender"]}))
        return out

    def _read_generic(self, container, md: int, mn: int) -> List[ChatMessage]:
        items = ax_select.find_all(self.ax, container, self.a["message_item"], md, mn)
        mode = self.a.get("item_text_source", "children")
        out: List[ChatMessage] = []
        for it in items[-int(self.a.get("max_messages", 30)):]:
            raw = ax_select.element_text(self.ax, it, mode)
            if not raw.strip():
                continue
            text, ts = raw, ""
            if self.msg_re:
                m = self.msg_re.search(raw)
                if not m:
                    continue
                text = m.group("text")
                ts = m.groupdict().get("ts") or ""
            out.append(ChatMessage(text=text.strip(), timestamp=(ts or "").strip(), raw=raw))
        return out

    def send_message(self, text: str) -> None:
        self._activate_and_verify()                # activate + frontmost + title check
        win = self._window()
        md, mn = self._limits()
        box = ax_select.find_one(self.ax, win, self.a["compose_box"], md, mn)
        if box is None:
            raise BackendError("입력창(ax.compose_box)을 못 찾았어요. 전송 취소")
        focused = self.ax.set_attr(box, "AXFocused", True)
        if not focused or self.a.get("click_compose_box"):
            fr = self.ax.frame(box)
            if fr is None:
                raise BackendError("입력창 위치를 못 구했어요. 전송 취소")
            x, y, w, h = fr
            self.desktop.click(x + w / 2, y + h / 2)
        self.sleep(0.2)
        self._verify_frontmost()                   # re-check right before pasting
        self._verify(win)
        old = self.desktop.get_clipboard() if self.a.get("restore_clipboard", True) else None
        try:
            self.desktop.set_clipboard(text)
            if self.a.get("clear_compose_before_paste", True):
                self.desktop.select_all_delete()
            self.desktop.paste()
            self.sleep(float(self.a.get("paste_delay_sec", 0.4)))
            self.desktop.press_return()
            self.sleep(0.5)
        finally:
            if old is not None:
                try:
                    self.desktop.set_clipboard(old)
                except Exception:
                    pass

    def dump_debug(self, path: str) -> None:
        md, mn = self._limits()
        app = self._app()
        text = ax_select.dump_tree(self.ax, app, md, mn)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(text)
