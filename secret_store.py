"""Read secrets from environment variables, optionally from the macOS Keychain.

Secrets are never written to disk or logged by this project. Config only holds
*names* (env var name, keychain service/account).
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


def get_secret(env_name: str, keychain_service: str = "", keychain_account: str = "") -> Optional[str]:
    """Env var first; then (macOS only) `security find-generic-password -s <service> [-a <account>] -w`."""
    if env_name:
        v = os.environ.get(env_name)
        if v:
            return v
    if keychain_service and sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", keychain_service]
        if keychain_account:
            cmd += ["-a", keychain_account]
        cmd.append("-w")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except Exception:
            return None
        if r.returncode == 0:
            v = r.stdout.rstrip("\n")
            return v or None
    return None
