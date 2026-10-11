#!/usr/bin/env python3
"""Live end-to-end smoke test for the Teams self-chat bridge (run ON the Mac).

This is a PURE CLIENT. It does NOT construct a Bridge and never processes the
chat itself: it only (1) checks that exactly one live bridge is running, (2)
types a probe into the self chat as if you typed it, and (3) watches for the new
'(b) ...' reply produced by that separately-running live bridge (run_mac.sh).

Running a second Bridge here is what caused "two bridges answered the same chat"
in the field, so that path is gone.

Detecting the reply: Teams virtualises the message list (visible count stays
constant as rows recycle), so we never slice "the last N". We compare the SET of
bot messages positioned AFTER the probe row against a baseline snapshot.

    python e2e_mac.py --config config.yaml
    python e2e_mac.py --config config.yaml --probe "/help" --expect "명령어 안내"
    python e2e_mac.py --config config.yaml --steps steps.json --start-first
    python e2e_mac.py --config config.yaml --no-send      # just watch

steps.json is a JSON array of {"probe": "...", "expect": "...", "timeout": 120}.
Exit code 0 if every step saw its expected reply, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import safety
from bridge import make_backend
from config import load_config
from state import normalize_text


# ----------------------------------------------------------------- live-bridge check
def _bridge_pids() -> list:
    """PIDs of running processes whose command line mentions bridge.py (best effort)."""
    try:
        out = subprocess.run(["pgrep", "-f", "bridge.py"], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    pids = []
    for ln in out.stdout.split():
        try:
            pid = int(ln)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _one_live_bridge(data_dir: str):
    """(ok, message). ok=True only if exactly one live bridge holds the lock.

    The live bridge holds an exclusive fcntl lock on data/bridge.lock for its
    whole lifetime (and the OS drops it automatically when that process dies, so
    a stale file is harmless). If we can take the lock, no bridge is running; if
    we cannot, exactly one holds it."""
    lock_path = os.path.join(data_dir, "bridge.lock")
    pids = _bridge_pids()
    try:
        import fcntl
    except ImportError:
        # No advisory locking available: fall back to pgrep only.
        if len(pids) == 1:
            return True, f"live bridge pid {pids[0]} (lock check unavailable)"
        return False, f"기대와 다름: 실행 중인 bridge.py 프로세스 {len(pids)}개 (정확히 1개여야 해요)"
    if not os.path.exists(lock_path):
        return False, "실행 중인 브리지가 없어요 (data/bridge.lock 없음). 먼저 run_mac.sh 로 브리지를 켜 주세요."
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()                        # held by the live bridge -> good
        extra = f" (pid {pids[0]})" if len(pids) == 1 else ""
        return True, f"live bridge is running{extra}"
    # We got the lock, so nobody else holds it.
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()
    return False, "실행 중인 브리지가 없어요 (lock 미점유). 먼저 run_mac.sh 로 브리지를 켜 주세요."


# ----------------------------------------------------------------- chat reads
def _bot_texts_after(backend, cfg, probe) -> set:
    """Normalized bot messages positioned AFTER the last occurrence of `probe` in
    the visible list, i.e. strictly newer than the probe we just typed. This is
    what stops an OLD reply (e.g. a '/role add qa' confirmation) that scrolls back
    into view from being mistaken for the fresh reply. No probe -> whole list."""
    prefixes = cfg["bot_prefixes_detect"]
    visible = backend.read_visible_messages()
    nprobe = normalize_text(probe) if probe else ""
    idx = -1
    if nprobe:
        for i, m in enumerate(visible):
            if normalize_text(m.text) == nprobe:
                idx = i                   # last occurrence
    tail = visible[idx + 1:] if idx >= 0 else visible
    return {normalize_text(m.text) for m in tail if safety.has_bot_prefix(m.text, prefixes)}


def _run_step(backend, cfg, probe, expect, timeout, interval, send=True):
    """Type `probe` (unless send=False) and wait for a NEW bot reply (after the
    probe) that contains `expect`. Returns (ok, matched_texts)."""
    before = _bot_texts_after(backend, cfg, probe if send else None)
    if send:
        print(f"  typing probe: {probe!r}")
        backend.send_message(probe)
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = _bot_texts_after(backend, cfg, probe if send else None)
        new = now - before
        if new:
            if not expect or any(expect in t for t in new):
                return True, new
            # new bot text appeared but not the expected one yet; keep waiting
        time.sleep(interval)
    return False, _bot_texts_after(backend, cfg, probe if send else None) - before


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live e2e CLIENT for the Teams bridge")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--probe", default="/status", help="message to type as the user")
    ap.add_argument("--expect", default="상태:", help="substring expected in the new bot reply")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between polls")
    ap.add_argument("--no-send", action="store_true", help="do not type a probe; just watch")
    ap.add_argument("--steps", help="JSON file: [{probe, expect, timeout}] run in order")
    ap.add_argument("--start-first", action="store_true",
                    help="send /status first and, if the reply says 일시정지, send /start")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    data_dir = cfg["data_dir"]

    ok, msg = _one_live_bridge(data_dir)
    print(("live-bridge check OK: " if ok else "FAIL: ") + msg)
    if not ok:
        return 1

    backend = make_backend(cfg)
    try:
        title = backend.focus_self_chat()
    except Exception as e:
        print(f"FAIL: 자기 채팅 확인 실패: {type(e).__name__}: {e}")
        return 1
    print(f"self chat OK: {title!r}")

    # Optionally make sure the live bridge is not paused before we probe it.
    if args.start_first:
        print("start-first: checking /status ...")
        got, texts = _run_step(backend, cfg, "/status", "상태:", 30.0, args.interval)
        paused = any("일시정지" in t for t in texts)
        if got and paused:
            print("  bridge is paused -> sending /start")
            _run_step(backend, cfg, "/start", "다시 켰어요", 30.0, args.interval)
        elif not got:
            print("  (no /status reply; is the live bridge healthy?)")

    # Build the step list.
    if args.steps:
        with open(args.steps, encoding="utf-8") as f:
            steps = json.load(f)
    elif args.no_send:
        steps = [{"probe": None, "expect": args.expect, "timeout": args.timeout}]
    else:
        steps = [{"probe": args.probe, "expect": args.expect, "timeout": args.timeout}]

    all_ok = True
    for i, st in enumerate(steps, 1):
        probe = st.get("probe")
        expect = st.get("expect", "")
        timeout = float(st.get("timeout", args.timeout))
        send = probe is not None
        print(f"step {i}/{len(steps)}: probe={probe!r} expect={expect!r} timeout={timeout}")
        got, new = _run_step(backend, cfg, probe, expect, timeout, args.interval, send=send)
        if got:
            print(f"  PASS - new bot reply:")
            for t in new:
                print("    -", t[:300])
        else:
            all_ok = False
            print(f"  FAIL - expected {expect!r} not seen within {timeout}s")
            for t in new:
                print("    (saw) -", t[:200])

    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
