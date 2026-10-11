import os

import platform_utils as pu
import secret_store


def test_mac_session_locked():
    assert pu.mac_session_locked(None) is True
    assert pu.mac_session_locked({"CGSSessionScreenIsLocked": 1}) is True
    assert pu.mac_session_locked({"kCGSSessionOnConsoleKey": False}) is True
    assert pu.mac_session_locked({"kCGSSessionOnConsoleKey": True,
                                  "CGSSessionScreenIsLocked": 0}) is False
    assert pu.mac_session_locked({"kCGSSessionOnConsoleKey": True}) is False


def test_is_workstation_locked_no_gui(monkeypatch):
    # With neither a mac nor a windows session, there is no lock concept here and
    # the loop must never be blocked. Mock the OS probes so this is deterministic
    # on Linux AND on the user's Mac (where the real probe would otherwise run).
    monkeypatch.setattr(pu, "is_mac", lambda: False)
    monkeypatch.setattr(pu, "is_windows", lambda: False)
    assert pu.is_workstation_locked() is False


def test_secret_from_env(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "s3cret")
    assert secret_store.get_secret("MY_TEST_KEY") == "s3cret"


def test_secret_missing(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    # no keychain on Linux -> None
    assert secret_store.get_secret("NOPE_KEY") is None
    assert secret_store.get_secret("") is None


def test_secret_env_beats_keychain(monkeypatch):
    monkeypatch.setenv("K", "fromenv")
    # even with a keychain service set, env var wins and no subprocess is needed
    assert secret_store.get_secret("K", "someservice") == "fromenv"
