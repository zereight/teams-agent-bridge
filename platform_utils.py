"""OS helpers: lock/screensaver detection and DPI. Safe to import on any OS."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Mapping, Optional


def is_windows() -> bool:
    return os.name == "nt"


def is_mac() -> bool:
    return sys.platform == "darwin"


def enable_dpi_awareness() -> None:
    """Windows only: make coordinates/screenshots use physical pixels. (macOS uses points.)"""
    if not is_windows():
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ------------------------------------------------------------------ macOS
def mac_session_locked(session: Optional[Mapping[str, Any]]) -> bool:
    """Interpret Quartz.CGSessionCopyCurrentDictionary().

    Locked if there is no GUI session, the screen is locked, or the session is
    not on the console (fast user switching / login window).
    """
    if not session:
        return True
    d = dict(session)
    if d.get("CGSSessionScreenIsLocked"):
        return True
    on_console = d.get("kCGSSessionOnConsoleKey")
    if on_console is not None and not bool(on_console):
        return True
    return False


def mac_screensaver_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-x", "ScreenSaverEngine"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _mac_locked() -> bool:
    try:
        import Quartz  # pyobjc-framework-Quartz
    except Exception:
        return False  # cannot tell; the title/frontmost checks still guard sends
    if mac_session_locked(Quartz.CGSessionCopyCurrentDictionary()):
        return True
    if mac_screensaver_running():
        return True
    try:
        if Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()):
            return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------ Windows
def _win_locked() -> bool:
    import ctypes
    user32 = ctypes.windll.user32
    hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not hdesk:
        return True
    try:
        if not user32.SwitchDesktop(hdesk):
            return True
    finally:
        user32.CloseDesktop(hdesk)
    running = ctypes.c_int(0)
    if user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0) and running.value:
        return True  # SPI_GETSCREENSAVERRUNNING
    return False


def is_workstation_locked() -> bool:
    """True if the screen is locked/asleep or the screensaver runs.

    Computer use cannot see or drive a locked screen, so the bridge skips the cycle.
    """
    if is_mac():
        return _mac_locked()
    if is_windows():
        return _win_locked()
    return False
