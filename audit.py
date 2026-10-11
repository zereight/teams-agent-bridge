"""Append-only JSON-lines audit log with rotation."""
from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import Any


class Audit:
    def __init__(self, log_dir: str, max_bytes: int = 5_000_000, backup_count: int = 10):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "audit.jsonl")
        self._log = logging.getLogger(f"audit.{id(self)}")
        self._log.setLevel(logging.INFO)
        self._log.propagate = False
        handler = RotatingFileHandler(self.path, maxBytes=max_bytes,
                                      backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._handler = handler
        self._log.addHandler(handler)
        self._console = logging.getLogger("bridge")

    def event(self, kind: str, **data: Any) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": kind, **data}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        self._log.info(line)
        self._console.info("%s %s", kind, json.dumps(data, ensure_ascii=False, default=str)[:300])

    def close(self) -> None:
        self._log.removeHandler(self._handler)
        self._handler.close()
