"""
Per-audit-id JWS persistence store.

Companion to :mod:`server.audit_chain`. Where the chain head store
tracks *the latest* audit_id per agent, this store keeps *the full
JWS* for every audit_id the daemon ever produced. The INSPECT
method reads from here; the chain inspector walks the chain by
following ``previous_audit_id`` and fetching each record.

Storage layout::

    {records_root}/
        {prefix}/
            {audit_id}.jws

``{prefix}`` is the first two hex characters of the audit_id, which
shards the directory tree into 256 buckets so the OS doesn't choke
on a single directory with millions of files. The JWS file is the
raw compact-form bytes (one line, ASCII), exactly as it was
stamped onto the Attribution-Record header.

Atomicity:
  Writes use a temp file in the same directory + ``os.replace`` so
  a partial write never publishes a malformed record. Reads are
  lock-free.

Portability:
  Plain text files in a directory tree. No DB. Default root is
  platform-appropriate (``~/.agtp/audit/records/`` on POSIX,
  ``%APPDATA%\\agtp\\audit\\records\\`` on Windows). Operators
  running multiple daemons on one host MUST set
  ``[audit].records_root`` explicitly to prevent collisions.

Thread safety:
  A module-level lock serializes writes. Reads do not need
  synchronization; reading a JWS file that's mid-rewrite is
  impossible because the writer uses atomic rename.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional


_LOCK = threading.Lock()


_HEX_CHARS = frozenset("0123456789abcdef")


def _safe_audit_id(audit_id: str) -> str:
    """Defensive: ensure the audit_id is well-formed 64-char hex
    before using it as a path component. Returns the lowered string
    on success; raises :class:`ValueError` on malformed input. The
    daemon only ever writes IDs it computed itself, but the INSPECT
    handler reads IDs from untrusted callers."""
    text = audit_id.strip().lower()
    if len(text) != 64 or any(c not in _HEX_CHARS for c in text):
        raise ValueError(f"audit_id is not 64-char hex: {audit_id!r}")
    return text


class AuditRecordStore:
    """File-backed per-audit-id JWS store.

    Construction is cheap — the root directory is created lazily on
    first write. Pass the configured ``[audit].records_root`` path;
    the daemon resolves the default at startup.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    def write(self, audit_id: str, jws: str) -> None:
        """Persist ``jws`` under ``audit_id``. Atomic via tmp +
        replace; serialized through the module lock so concurrent
        dispatches don't corrupt the file.

        Failures (disk full, permission denied) propagate so the
        caller decides whether to fail the response. ``_finalize_response``
        catches them and logs without breaking the wire path."""
        safe = _safe_audit_id(audit_id)
        path = self._path_for(safe)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".jws.tmp")
            tmp.write_text(jws, encoding="ascii")
            os.replace(tmp, path)

    def read(self, audit_id: str) -> Optional[str]:
        """Read the JWS for ``audit_id``. Returns ``None`` when the
        record doesn't exist (e.g., another agent's record, a
        rotated-out record, or an attacker-supplied id)."""
        try:
            safe = _safe_audit_id(audit_id)
        except ValueError:
            return None
        path = self._path_for(safe)
        try:
            return path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _path_for(self, safe_audit_id: str) -> Path:
        prefix = safe_audit_id[:2]
        return self.root / prefix / f"{safe_audit_id}.jws"


def default_records_root() -> Path:
    """Return the platform-appropriate default records directory.

    Linux/macOS: ``~/.agtp/audit/records/``
    Windows:     ``%APPDATA%\\agtp\\audit\\records\\`` when APPDATA
                 is set; otherwise the home-dir fallback.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "agtp" / "audit" / "records"
    return Path.home() / ".agtp" / "audit" / "records"


__all__ = [
    "AuditRecordStore",
    "default_records_root",
]
