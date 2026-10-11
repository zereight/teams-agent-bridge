"""macOS desktop actions: activate an app, frontmost check, clipboard, keys, clicks.

Uses pyobjc (AppKit + Quartz) and the built-in pbcopy/pbpaste/open commands.
Posting key/mouse events requires the Accessibility permission for the app
that runs Python (Terminal, iTerm, ...).
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

try:
    import Quartz
    from AppKit import NSRunningApplication, NSWorkspace
except Exception:  # pragma: no cover - lets the module import on Linux
    Quartz = None
    NSRunningApplication = NSWorkspace = None

KEY_V = 9
KEY_A = 0
KEY_RETURN = 36
KEY_DELETE = 51  # backspace

_UTF8_ENV = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")


class MacDesktop:
    def __init__(self) -> None:
        if Quartz is None:
            raise RuntimeError("pyobjc(Quartz/AppKit)가 없어요. pip install -r requirements.txt")

    # ---------------------------------------------------------------- apps
    def pid_for(self, bundle_id: str) -> Optional[int]:
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        if apps and len(apps) > 0:
            return int(apps[0].processIdentifier())
        return None

    def activate(self, bundle_id: str) -> None:
        subprocess.run(["open", "-b", bundle_id], capture_output=True, timeout=15)

    def frontmost_bundle_id(self) -> str:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.bundleIdentifier()) if app is not None else ""

    # ---------------------------------------------------------------- clipboard
    def get_clipboard(self) -> Optional[str]:
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=5, env=_UTF8_ENV)
            return r.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None

    def set_clipboard(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, env=_UTF8_ENV, check=True)

    # ---------------------------------------------------------------- input
    def _key(self, keycode: int, cmd: bool = False) -> None:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            if cmd:
                Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.03)

    def click(self, x: float, y: float) -> None:
        pt = (float(x), float(y))
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

    def select_all_delete(self) -> None:
        self._key(KEY_A, cmd=True)
        self._key(KEY_DELETE)

    def paste(self) -> None:
        self._key(KEY_V, cmd=True)

    def press_return(self) -> None:
        self._key(KEY_RETURN)
