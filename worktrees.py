"""Per-task git worktree isolation.

Each task (or handoff chain) runs in its own git worktree under
~/teams-agent-bridge/worktrees/<role>-<ts> on branch bridge/<role>-<MMDD-HHMM>,
created from the repo's current HEAD. The bridge never pushes. /clean removes
merged/old worktrees; /branches lists them.

Shells out to read-only / local git only (no network).
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Worktree:
    path: str
    branch: str
    repo: str


def _git(repo: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True, timeout=timeout)


def repo_root(path: str) -> Optional[str]:
    """Top-level git dir containing `path`, or None if not a repo."""
    try:
        r = _git(path, "rev-parse", "--show-toplevel")
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def branch_name(role: str, now: Optional[float] = None) -> str:
    stamp = time.strftime("%m%d-%H%M", time.localtime(now))
    safe = (role or "task").replace("/", "-")
    return f"bridge/{safe}-{stamp}"


def worktree_dirname(role: str, now: Optional[float] = None) -> str:
    stamp = time.strftime("%m%d-%H%M%S", time.localtime(now))
    safe = (role or "task").replace("/", "-")
    return f"{safe}-{stamp}"


def create(repo: str, base_dir: str, role: str, now: Optional[float] = None) -> Worktree:
    """git worktree add -b bridge/<role>-<MMDD-HHMM> <base_dir>/<role>-<ts> HEAD"""
    os.makedirs(base_dir, exist_ok=True)
    branch = branch_name(role, now)
    path = os.path.join(base_dir, worktree_dirname(role, now))
    # avoid branch-name collisions when two tasks start in the same minute
    b, n = branch, 1
    existing = {w.branch for w in list_worktrees(repo)}
    while b in existing:
        n += 1
        b = f"{branch}-{n}"
    branch = b
    r = _git(repo, "worktree", "add", "-b", branch, path, "HEAD", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add 실패: {r.stderr.strip() or r.stdout.strip()}")
    return Worktree(path=path, branch=branch, repo=repo)


def list_worktrees(repo: str) -> List[Worktree]:
    try:
        r = _git(repo, "worktree", "list", "--porcelain")
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out, cur = [], {}
    for line in r.stdout.splitlines() + [""]:
        if not line.strip():
            if cur.get("worktree"):
                out.append(Worktree(path=cur.get("worktree", ""),
                                    branch=cur.get("branch", "").replace("refs/heads/", ""),
                                    repo=repo))
            cur = {}
            continue
        if line.startswith("worktree "):
            cur["worktree"] = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip()
        elif line.startswith("detached"):
            cur["branch"] = "(detached)"
    return out


def bridge_worktrees(repo: str, base_dir: str) -> List[Worktree]:
    base = os.path.realpath(base_dir)
    return [w for w in list_worktrees(repo)
            if os.path.realpath(w.path).startswith(base + os.sep) or w.branch.startswith("bridge/")]


def diffstat(worktree_path: str, max_files: int = 20) -> List[str]:
    """`git diff --stat HEAD` lines inside the worktree (committed+uncommitted vs HEAD)."""
    try:
        r = subprocess.run(["git", "-C", worktree_path, "diff", "--stat", "HEAD"],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    lines = [ln.rstrip() for ln in r.stdout.splitlines() if ln.strip()]
    return lines[:max_files]


def is_merged(repo: str, branch: str, into: str = "") -> bool:
    """True if `branch` is fully merged into `into` (default: repo HEAD/default)."""
    base = into
    if not base:
        for cand in ("main", "master"):
            if _git(repo, "rev-parse", "--verify", cand).returncode == 0:
                base = cand
                break
    if not base:
        return False
    r = _git(repo, "branch", "--merged", base)
    if r.returncode != 0:
        return False
    merged = {ln.strip().lstrip("* ").strip() for ln in r.stdout.splitlines()}
    return branch in merged


def clean_candidates(repo: str, base_dir: str, older_than_days: float = 7.0,
                     into: str = "") -> List[Worktree]:
    """Bridge worktrees that are merged OR older than `older_than_days`."""
    now = time.time()
    out = []
    for w in bridge_worktrees(repo, base_dir):
        old = False
        try:
            old = (now - os.path.getmtime(w.path)) > older_than_days * 86400
        except OSError:
            old = True
        if old or is_merged(repo, w.branch, into):
            out.append(w)
    return out


def remove_worktree(wt: Worktree) -> bool:
    r = _git(wt.repo, "worktree", "remove", "--force", wt.path, timeout=60)
    if r.returncode != 0:
        return False
    if wt.branch and wt.branch not in ("(detached)", "main", "master"):
        _git(wt.repo, "branch", "-D", wt.branch)
    return True
