"""Audit log: append-only JSONL inside engagement_dir.

Every tool call before/after, every scope violation, every approval decision
gets a line. This is the legal evidence chain.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _default(o: Any):
    return list(o) if isinstance(o, set) else str(o)


class AuditLog:
    def __init__(self, engagement_dir: Path):
        self.path = engagement_dir / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event_type: str, **fields) -> None:
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "type": event_type,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False, default=_default)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
