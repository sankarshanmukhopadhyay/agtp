"""
Per-agent audit chain head store.

Each Attribution-Record the daemon produces carries a
``previous_audit_id`` that links it to the agent's prior record,
forming a per-agent hash chain. The "chain head" is the most recent
audit_id known for a given agent; the daemon reads it before
building a new record and writes it after.

Storage layout::

    {chain_head_root}/
        {agent_id}.json     # one file per agent

Each file holds a compact JSON object::

    {"last_audit_id": "<64-hex>", "last_at": "<ISO 8601 UTC>"}

Atomicity:
  Writes use a temp file in the same directory + ``os.replace`` so a
  partial write never corrupts the head. Reads tolerate missing files
  (first record an agent ever produces has no predecessor).

Portability:
  The store is a plain directory of small JSON files. No database
  required. The default root is ``~/.agtp/audit/chain_heads/`` (or
  ``%APPDATA%\\agtp\\audit\\chain_heads\\`` on Windows). Operators can
  override via ``[audit].chain_head_root`` in agtp-server.toml.

Thread safety:
  A re-entrant transaction lock serializes the complete head-read,
  record-write, and head-update operation in the daemon. This prevents
  concurrent responses from creating sibling records from one predecessor.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_LOCK = threading.RLock()


@contextmanager
def audit_append_transaction():
    """Serialize the complete audit append transaction.

    Callers hold this lock across head read, record construction, record
    persistence, and head update. This prevents concurrent responses for the
    same process from creating sibling records from one predecessor and
    silently orphaning the losing branch.
    """
    with _LOCK:
        yield


@dataclass(frozen=True)
class ChainHead:
    """The latest audit record for a single agent."""

    audit_id: str
    at_iso: str


class AuditChainStore:
    """File-backed per-agent chain head store.

    Construction is cheap — the root directory is created lazily on
    first write. Pass the configured ``[audit].chain_head_root`` path;
    the daemon resolves the default at startup.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    def head(self, agent_id: str) -> Optional[ChainHead]:
        """Read the current chain head for ``agent_id``.

        Returns ``None`` when the agent has no recorded head (first
        record), when the file is missing, or when the file is
        malformed (corrupted files are treated as missing so a single
        bad write doesn't poison the chain forever — the next write
        recovers).
        """
        path = self._path_for(agent_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        audit_id = data.get("last_audit_id")
        at_iso = data.get("last_at")
        if not isinstance(audit_id, str) or not isinstance(at_iso, str):
            return None
        return ChainHead(audit_id=audit_id, at_iso=at_iso)

    def write(self, agent_id: str, audit_id: str, at_iso: str) -> None:
        """Replace the chain head for ``agent_id``.

        Atomic via temp file + ``os.replace``. Creates the root
        directory on first call. Failures (filesystem full, permission
        denied) propagate to the caller — the daemon catches them so
        the response path keeps working even when the chain store is
        unhealthy, but the caller decides whether to surface the
        failure.
        """
        if not agent_id:
            return  # server-level operations have no agent to chain
        path = self._path_for(agent_id)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = json.dumps(
                {"last_audit_id": audit_id, "last_at": at_iso},
                separators=(",", ":"),
            )
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)

    def _path_for(self, agent_id: str) -> Path:
        # Agent IDs are 64-hex strings; safe as filenames. We still
        # strip the value so a stray header value with a path
        # separator can't escape the root directory.
        safe = agent_id.strip().replace("/", "").replace("\\", "")
        return self.root / f"{safe}.json"


def default_chain_head_root() -> Path:
    """Return the platform-appropriate default chain-head directory.

    Linux/macOS: ``~/.agtp/audit/chain_heads/``
    Windows:     ``%APPDATA%\\agtp\\audit\\chain_heads\\`` when APPDATA
                 is set; otherwise the home-dir fallback.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "agtp" / "audit" / "chain_heads"
    return Path.home() / ".agtp" / "audit" / "chain_heads"


__all__ = [
    "AuditChainStore",
    "audit_append_transaction",
    "ChainHead",
    "default_chain_head_root",
]
