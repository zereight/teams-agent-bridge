"""Named roles (mini-bots) that can hand off tasks to each other.

Pure/testable except read_git_changes (which shells out to a read-only
`git status --porcelain`). Roles persist in data/roles.json.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NAME_RE = re.compile(r"^[A-Za-z0-9_\-가-힣]{1,32}$")
# a handoff line: "@dev 로그인 고쳐줘"  (anchored per line)
HANDOFF_LINE = re.compile(r"^\s*@(?P<name>[A-Za-z0-9_\-가-힣]{1,32})\s+(?P<msg>\S.*)$")


@dataclass
class Role:
    name: str
    instructions: str = ""
    agent: str = "claude"              # claude | codex
    allowed_tools: Optional[List[str]] = None   # optional per-role allow override
    session_id: Optional[str] = None   # last agent session, for resume
    created_at: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "instructions": self.instructions, "agent": self.agent,
                "allowed_tools": self.allowed_tools, "session_id": self.session_id,
                "created_at": self.created_at}

    @staticmethod
    def from_dict(d: dict) -> "Role":
        return Role(name=d["name"], instructions=d.get("instructions", ""),
                    agent=d.get("agent", "claude"), allowed_tools=d.get("allowed_tools"),
                    session_id=d.get("session_id"), created_at=d.get("created_at", ""))


def parse_role_add(args: str) -> Tuple[str, str, str]:
    """Parse '<name> [agent=claude|codex|pi] <instructions...>' -> (name, agent, instructions).

    Raises ValueError on a bad name or missing instructions.
    """
    args = (args or "").strip()
    if not args:
        raise ValueError("이름과 지침이 필요해요")
    parts = args.split(None, 1)
    name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    agent = "claude"
    m = re.match(r"(?i)agent\s*=\s*(claude|codex|pi)\s+(.*)$", rest.strip(), re.S)
    if m:
        agent = m.group(1).lower()
        rest = m.group(2)
    if not NAME_RE.match(name):
        raise ValueError(f"이름이 올바르지 않아요: {name!r} (영문/숫자/한글/_/-, 1~32자)")
    if not rest.strip():
        raise ValueError("역할 지침(instructions)이 비어 있어요")
    return name, agent, rest.strip()


def sanitize_allowed(allowed: Optional[List[str]], disallowed: List[str]) -> Optional[List[str]]:
    """Drop any per-role allow entry that collides with a global disallow rule.

    A role may never be looser than the global disallowed list. Matching is by
    the tool head (before '(') OR exact rule, case-insensitive.
    """
    if not allowed:
        return allowed
    def head(rule: str) -> str:
        return rule.split("(", 1)[0].strip().lower()
    dis_heads = {head(d) for d in (disallowed or [])}
    dis_exact = {d.strip().lower() for d in (disallowed or [])}
    out = []
    for a in allowed:
        if a.strip().lower() in dis_exact or head(a) in dis_heads:
            continue
        out.append(a)
    return out


def find_handoffs(result_text: str, known_roles: List[str]) -> List[Tuple[str, str, bool]]:
    """Return [(name, message, known)] for each '@name msg' line in the result."""
    known = {r.lower() for r in known_roles}
    out = []
    for line in (result_text or "").splitlines():
        m = HANDOFF_LINE.match(line)
        if m:
            name = m.group("name")
            out.append((name, m.group("msg").strip(), name.lower() in known))
    return out


def read_git_changes(workdir: str, limit: int = 20) -> List[str]:
    """`git status --porcelain` (read-only). Returns changed paths, or []."""
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=workdir,
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    lines = [ln.rstrip() for ln in r.stdout.splitlines() if ln.strip()]
    return lines[:limit]


class RoleStore:
    def __init__(self, path: str):
        self.path = path
        self.roles: Dict[str, Role] = {}
        self.default_role: str = ""
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.default_role = data.get("default_role", "")
            for d in data.get("roles", []):
                r = Role.from_dict(d)
                self.roles[r.name.lower()] = r

    def save(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        payload = {"default_role": self.default_role,
                   "roles": [r.to_dict() for r in self.roles.values()]}
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".roles-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def get(self, name: str) -> Optional[Role]:
        return self.roles.get((name or "").lower())

    def exists(self, name: str) -> bool:
        return (name or "").lower() in self.roles

    def add(self, role: Role) -> None:
        self.roles[role.name.lower()] = role
        self.save()

    def remove(self, name: str) -> bool:
        key = (name or "").lower()
        if key not in self.roles:
            return False
        del self.roles[key]
        if self.default_role.lower() == key:
            self.default_role = ""
        self.save()
        return True

    def set_default(self, name: str) -> bool:
        if not self.exists(name):
            return False
        self.default_role = self.get(name).name
        self.save()
        return True

    def set_session(self, name: str, session_id: Optional[str]) -> None:
        r = self.get(name)
        if r is not None and session_id:
            r.session_id = session_id
            self.save()

    def names(self) -> List[str]:
        return [r.name for r in self.roles.values()]

    def all(self) -> List["Role"]:
        return list(self.roles.values())
