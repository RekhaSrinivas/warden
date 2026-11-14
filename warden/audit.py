"""Append-only, hash-chained audit log.

Each entry stores the previous entry's hash, and its own hash covers its
content plus that link. Editing or deleting any entry breaks every hash after it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256((entry["prev_hash"] + _canonical(body)).encode("utf-8")).hexdigest()


class AuditLog:
    """In memory when `path` is None, otherwise mirrored to a JSONL file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._append_lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        if path is not None and path.exists():
            self._entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def append(self, event: str, **data: Any) -> dict[str, Any]:
        # Locked: the card network's real-time authorization call writes here while the engine may be mid-payment.
        with self._append_lock:
            return self._append(event, data)

    def _append(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        prev = self._entries[-1]["hash"] if self._entries else GENESIS
        entry = json.loads(_canonical({
            "seq": len(self._entries),
            "ts": round(time.time(), 3),
            "event": event,
            "data": data,
            "prev_hash": prev,
        }))
        entry["hash"] = _entry_hash(entry)
        self._entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(_canonical(entry) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def verify(self) -> int | None:
        """Return the index of the first broken entry, or None if the chain is intact."""
        prev = GENESIS
        for i, entry in enumerate(self._entries):
            if entry.get("prev_hash") != prev or entry.get("hash") != _entry_hash(entry):
                return i
            prev = entry["hash"]
        return None
