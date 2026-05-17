"""
AuditLog — append-only JSONL writer with thread-safe writes.

The on-disk format is one JSON object per line. Operators consume
the log with normal line-oriented tools (``tail``, ``jq``, log
aggregators that understand JSONL).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict


class AuditLog:
    """Append-only JSONL log. Opens the file lazily on first write."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fd = None
        self._open_failed = False

    def write(self, entry: Dict[str, Any]) -> None:
        """Append ``entry`` as a single JSON line. On I/O failure,
        logs once to stderr and silently drops subsequent entries —
        an audit module that crashes the daemon would be worse than
        a missing audit trail."""
        if self._open_failed:
            return
        line = json.dumps(entry, separators=(",", ":"), default=str)
        with self._lock:
            try:
                if self._fd is None:
                    parent = os.path.dirname(self.path)
                    if parent and not os.path.isdir(parent):
                        os.makedirs(parent, exist_ok=True)
                    self._fd = open(self.path, "ab", buffering=0)
                self._fd.write(line.encode("utf-8") + b"\n")
            except OSError as exc:
                if not self._open_failed:
                    print(
                        f"[mod_audit] failed to write to {self.path}: {exc}",
                        file=sys.stderr,
                    )
                    self._open_failed = True

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    self._fd.close()
                except OSError:
                    pass
                self._fd = None
