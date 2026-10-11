"""Backend A: Windows UI Automation via pywinauto (uia backend).

New Teams (ms-teams.exe) renders its UI in WebView2, which exposes an
accessibility (UIA) tree. The exact control names/types differ between Teams
versions and UI languages, so EVERY selector is in config.yaml (uia: ...).
Use `python bridge.py --dump-tree` or Accessibility Insights / inspect.exe to
find the right ones (see README.ko.md).

Selector dicts are passed straight to pywinauto's child_window(), so any of
its keywords work: title, title_re, control_type, auto_id, class_name,
found_index, ...
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from backend_base import BackendError, ChatBackend, ChatGuardError, ChatMessage

try:  # Windows only
    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys
    import pyperclip
except Exception:  # pragma: no cover - lets the module import on Linux
    Desktop = None
    send_keys = None
    pyperclip = None


class TeamsUIABackend(ChatBackend):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__()
        if Desktop is None:
            raise BackendError("pywinauto/pyperclip 가 없어요 (Windows에서 pip install -r requirements.txt)")
        self.cfg = cfg
        self.u = cfg["uia"]
        self.title_re = re.compile(cfg["self_chat"]["title_regex"])
        self.msg_re = re.compile(self.u["message_regex"], re.S) if self.u.get("message_regex") else None

    # ------------------------------------------------------------ helpers
    def _window(self):
        spec = Desktop(backend="uia").window(**self.u["window"])
        try:
            spec.wait("exists", timeout=5)
            return spec.wrapper_object()
        except Exception as e:
            raise BackendError(f"Teams 창을 못 찾았어요: {e}") from e

    def _find(self, root, selector: Optional[Dict[str, Any]]):
        if not selector:
            return root
        try:
            return root.child_window(**selector).wrapper_object()
        except Exception as e:
            raise BackendError(f"요소를 못 찾았어요 {selector}: {e}") from e

    def _title_text(self, win) -> str:
        parts = [win.window_text() or ""]
        if self.u.get("chat_header"):
            try:
                hdr = win.child_window(**self.u["chat_header"]).wrapper_object()
                parts.append(hdr.window_text() or "")
            except Exception:
                parts.append("")
        return " | ".join(parts)

    def _verify(self, win) -> str:
        title = self._title_text(win)
        if not self.title_re.search(title):
            raise ChatGuardError(f"나와의 채팅이 아니에요: {title!r}")
        return title

    # ------------------------------------------------------------ interface
    def focus_self_chat(self) -> str:
        win = self._window()
        try:
            if win.is_minimized():
                win.restore()
            win.set_focus()
        except Exception as e:
            raise BackendError(f"Teams 창에 포커스를 못 줬어요: {e}") from e
        time.sleep(0.3)
        return self._verify(win)

    def read_visible_messages(self) -> List[ChatMessage]:
        if not self.u.get("message_list"):
            # the whole window also contains chat-list previews of OTHER chats,
            # which must never be treated as commands
            raise BackendError("uia.message_list 선택자를 먼저 설정해 주세요 (--dump-tree 참고)")
        win = self._window()
        self._verify(win)
        container = self._find(win, self.u.get("message_list"))
        item_sel = dict(self.u["message_item"])
        try:
            items = container.descendants(**item_sel)
        except Exception as e:
            raise BackendError(f"메시지 목록을 못 읽었어요: {e}") from e
        out: List[ChatMessage] = []
        for it in items[-int(self.u["max_messages"]):]:
            raw = self._item_text(it)
            if not raw.strip():
                continue
            text, ts = raw, ""
            if self.msg_re:
                m = self.msg_re.search(raw)
                if not m:
                    continue  # not a chat message (date divider, etc.)
                text = m.group("text")
                ts = m.groupdict().get("ts") or ""
            out.append(ChatMessage(text=text.strip(), timestamp=ts.strip(), raw=raw))
        return out

    def _item_text(self, it) -> str:
        if self.u.get("item_text_source") == "children":
            try:
                texts = [c.window_text() for c in it.descendants(control_type="Text")]
                return "\n".join(t for t in texts if t)
            except Exception:
                return ""
        try:
            return it.window_text() or ""
        except Exception:
            return ""

    def send_message(self, text: str) -> None:
        win = self._window()
        self._verify(win)                         # re-check right before typing
        box = self._find(win, self.u["compose_box"])
        old_clip = None
        if self.u.get("restore_clipboard"):
            try:
                old_clip = pyperclip.paste()
            except Exception:
                old_clip = None
        try:
            box.click_input()
            time.sleep(0.2)
            self._verify(win)
            pyperclip.copy(text)
            if self.u.get("clear_compose_before_paste", True):
                send_keys("^a{BACKSPACE}")        # clear a half-typed draft in the box
            send_keys("^v")
            time.sleep(float(self.u.get("paste_delay_sec", 0.4)))
            send_keys("{ENTER}")
            time.sleep(0.5)
        finally:
            if old_clip is not None:
                try:
                    pyperclip.copy(old_clip)
                except Exception:
                    pass

    def dump_debug(self, path: str) -> None:
        spec = Desktop(backend="uia").window(**self.u["window"])
        spec.wait("exists", timeout=5)
        spec.print_control_identifiers(depth=None, filename=path)
