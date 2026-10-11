"""Backend B (fallback): screenshot + vision model. Works on macOS and Windows.

read:  screenshot the Teams window -> ask the model for JSON
       {"chat_title": str, "messages": [{"text","timestamp"}], "compose_box": {"x","y"}}
send:  re-screenshot + re-verify title -> click compose box -> paste -> Return/Enter

Retina/HiDPI: screenshots are in PIXELS but mouse clicks use POINTS. We grab the
window pixel rectangle, downscale for the API, and map the model's pixel
coordinates back to screen POINTS using (window point size / screenshot pixels).

Model name is a config value (vision.model). API key comes from the env var in
vision.api_key_env (or the macOS Keychain if configured). On macOS this needs
BOTH Accessibility (to click/type) and Screen Recording (to capture) permission.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from backend_base import BackendError, ChatBackend, ChatGuardError, ChatMessage
from secret_store import get_secret

IS_MAC = sys.platform == "darwin"

try:
    import mss
    import pyautogui
    import pyperclip
    from PIL import Image
except Exception:  # pragma: no cover
    mss = pyautogui = pyperclip = Image = None

try:
    import anthropic
except Exception:  # pragma: no cover
    anthropic = None

PROMPT = """This is a screenshot of the Microsoft Teams desktop app.
Return ONLY a JSON object, no prose, with this shape:
{"chat_title": "<title shown in the chat header, exactly as displayed>",
 "messages": [{"text": "<full message text exactly as shown>", "timestamp": "<time shown or empty>"}],
 "compose_box": {"x": <int>, "y": <int>}}
Rules:
- messages: the chat messages visible in the OPEN conversation, oldest first, at most %d.
- Copy text verbatim, keep Korean as-is, keep any leading "(b)" exactly.
- Do not include the left chat list, date dividers, system notices, or the compose draft.
- compose_box: pixel coordinates in THIS image of the centre of the message input box.
- If no chat is open, use "chat_title": "" and "messages": []."""


def extract_json(text: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in model reply")
    return json.loads(m.group(0))


def map_pixel_to_point(px: float, py: float, win_left: float, win_top: float,
                       scale_x: float, scale_y: float) -> Tuple[int, int]:
    """Model gave pixel coords in the (possibly downscaled) screenshot; map to screen points."""
    return int(win_left + px * scale_x), int(win_top + py * scale_y)


class _MacWin:
    def __init__(self, bundle_id: str):
        from mac_desktop import MacDesktop
        self.d = MacDesktop()
        self.bundle_id = bundle_id
        import ax_select  # noqa
        import teams_ax
        self.teams_ax = teams_ax

    def rect_points(self) -> Tuple[float, float, float, float]:
        pid = self.d.pid_for(self.bundle_id)
        if not pid:
            raise BackendError(f"Teams({self.bundle_id})가 실행 중이 아니에요")
        ax = self.teams_ax.MacAX()
        app = ax.app_element(pid)
        win = ax.attr(app, "AXFocusedWindow") or ax.attr(app, "AXMainWindow")
        wins = ax.attr(app, "AXWindows") or []
        if win is None and wins:
            win = wins[0]
        if win is None:
            raise BackendError("Teams 창을 못 찾았어요")
        fr = ax.frame(win)
        if fr is None:
            raise BackendError("Teams 창 크기를 못 구했어요")
        return fr  # x, y, w, h in points

    def activate(self) -> None:
        self.d.activate(self.bundle_id)

    def frontmost_ok(self) -> bool:
        return self.d.frontmost_bundle_id() == self.bundle_id


class _WinWin:
    def __init__(self, title_re: str):
        import pygetwindow as gw
        self.gw = gw
        self.re = re.compile(title_re)

    def _w(self):
        wins = [w for w in self.gw.getAllWindows() if w.title and self.re.search(w.title)]
        if not wins:
            raise BackendError("Teams 창을 못 찾았어요")
        return wins[0]

    def rect_points(self):
        w = self._w()
        return float(w.left), float(w.top), float(w.width), float(w.height)

    def activate(self):
        w = self._w()
        try:
            if w.isMinimized:
                w.restore()
            w.activate()
        except Exception:
            pass

    def frontmost_ok(self):
        return True  # pygetwindow has no reliable frontmost check; title guard covers it


class TeamsVisionBackend(ChatBackend):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__()
        if mss is None or pyautogui is None:
            raise BackendError("mss/pyautogui/pyperclip/Pillow 가 없어요 (pip install -r requirements.txt)")
        if anthropic is None:
            raise BackendError("anthropic 패키지가 없어요 (pip install anthropic)")
        self.cfg = cfg
        self.v = cfg["vision"]
        if not self.v.get("model"):
            raise BackendError("vision.model 을 config.yaml 에 설정해 주세요")
        key = get_secret(self.v.get("api_key_env", ""), self.v.get("api_keychain_service", ""),
                         self.v.get("api_keychain_account", ""))
        if not key:
            raise BackendError(f"API 키가 없어요. 환경변수 {self.v.get('api_key_env')} 를 설정하세요")
        self.client = anthropic.Anthropic(api_key=key)
        self.title_re = re.compile(cfg["self_chat"]["title_regex"])
        self.win = (_MacWin(self.v.get("bundle_id", "com.microsoft.teams2")) if IS_MAC
                    else _WinWin(self.v["window_title_regex"]))
        self._cache: Optional[Tuple[float, Dict[str, Any], Tuple[float, float, float, float]]] = None
        pyautogui.FAILSAFE = True

    def _grab(self) -> Tuple[Dict[str, Any], Tuple[float, float, float, float]]:
        """Return (model_data, (win_left_pt, win_top_pt, scale_x, scale_y))."""
        self.win.activate()
        time.sleep(0.5)
        lx, ty, wpt, hpt = self.win.rect_points()
        with mss.mss() as sct:
            # match monitors by locating the one containing the window; grab in pixels
            shot = sct.grab({"left": int(lx), "top": int(ty), "width": int(wpt), "height": int(hpt)})
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        px_w, px_h = img.width, img.height          # physical pixels captured
        down = 1.0
        maxw = int(self.v["max_image_width"])
        if img.width > maxw:
            down = img.width / maxw
            img = img.resize((maxw, int(img.height / down)))
        # model coords are in the downscaled image; map to screen points:
        # downscaled_px -> physical_px (*down) -> points (* wpt/px_w)
        scale_x = down * (wpt / px_w if px_w else 1.0)
        scale_y = down * (hpt / px_h if px_h else 1.0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        try:
            resp = self.client.messages.create(
                model=self.v["model"], max_tokens=int(self.v["max_tokens"]),
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": PROMPT % int(self.cfg["ax"]["max_messages"])}]}])
            reply = "".join(getattr(b, "text", "") for b in resp.content)
            data = extract_json(reply)
        except Exception as e:
            raise BackendError(f"비전 분석 실패: {e}") from e
        return data, (lx, ty, scale_x, scale_y)

    def _analyse(self, force: bool = False):
        now = time.time()
        if not force and self._cache and now - self._cache[0] < float(self.v["cache_sec"]):
            return self._cache[1], self._cache[2]
        data, geo = self._grab()
        self._cache = (now, data, geo)
        return data, geo

    def _verify(self, data: Dict[str, Any]) -> str:
        title = str(data.get("chat_title") or "")
        if not self.title_re.search(title):
            raise ChatGuardError(f"나와의 채팅이 아니에요: {title!r}")
        if not self.win.frontmost_ok():
            raise ChatGuardError("맨 앞 앱이 Teams가 아니에요")
        return title

    def focus_self_chat(self) -> str:
        data, _ = self._analyse(force=True)
        return self._verify(data)

    def read_visible_messages(self) -> List[ChatMessage]:
        data, _ = self._analyse()
        self._verify(data)
        out = []
        for m in data.get("messages") or []:
            t = str(m.get("text") or "").strip()
            if t:
                out.append(ChatMessage(text=t, timestamp=str(m.get("timestamp") or ""),
                                       raw=json.dumps(m, ensure_ascii=False)))
        return out

    def send_message(self, text: str) -> None:
        data, (lx, ty, sx, sy) = self._analyse(force=True)
        self._verify(data)
        cb = data.get("compose_box") or {}
        if "x" in cb and "y" in cb:
            x, y = map_pixel_to_point(float(cb["x"]), float(cb["y"]), lx, ty, sx, sy)
        rlx, rty, rwpt, rhpt = self.win.rect_points()
        if not ("x" in cb and "y" in cb):
            rx, ry = self.v["compose_box_relative"]
            x, y = int(rlx + rwpt * rx), int(rty + rhpt * ry)
        if not (rlx <= x <= rlx + rwpt and rty <= y <= rty + rhpt):
            raise BackendError("입력창 좌표가 Teams 창 밖이에요. 전송 취소")
        pyautogui.click(x, y)
        time.sleep(0.2)
        pyperclip.copy(text)
        pyautogui.hotkey("command" if IS_MAC else "ctrl", "v")
        time.sleep(float(self.v.get("paste_delay_sec", 0.4)))
        pyautogui.press("enter")
        time.sleep(0.5)
        self._cache = None

    def dump_debug(self, path: str) -> None:
        data, geo = self._analyse(force=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"analysis": data, "geometry": geo}, f, ensure_ascii=False, indent=1)
        print(json.dumps(data, ensure_ascii=False, indent=1))
