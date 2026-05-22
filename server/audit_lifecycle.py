"""
Per-agent lifecycle event stream.

Companion to :mod:`server.audit_records` (Phase 6, per-action JWS
records) and :mod:`server.audit_chain` (per-agent chain heads).
Phase 8 adds this third store specifically for identity-lifecycle
events: ACTIVATE, DEACTIVATE, REVOKE, and any future per-state
transitions.

Why a separate stream:

  * Lifecycle events are sparse (a few per agent's lifetime) and
    semantically distinct from per-request action records.
  * Regulators, governance auditors, and the chain inspector need
    "show me everything that happened to this agent's identity"
    without filtering thousands of per-action records.
  * Operators may want different retention policies on the two
    streams (lifecycle = forever, action = N days).

Storage layout::

    {lifecycle_root}/
        {agent_id}.jsonl     # one JWS per line, append-only

Two on-disk forms, chosen by ``[audit].mode``:

  * ``jws`` (default) — each line is a JWS Compact-form
    Attribution-Record (three dot-separated base64url segments).
  * ``scitt`` — each line is ``"cose:" + base64url(COSE_Sign1
    bytes)``. The COSE form is an RFC 9943 SCITT statement; any
    SCITT verifier consumes it without AGTP knowledge.

The reader sniffs the prefix on each line. The two forms can
coexist in the same file across a mode flip; existing lines stay
verifiable in their original format.

Each event payload carries:

  * ``event_type``    "activate" / "deactivate" / "revoke" /
                       "reinstate" / "deprecate"
  * ``agent_id``      the agent the event applies to
  * ``previous_status`` the agent's status before the transition
  * ``new_status``    the agent's status after the transition
  * ``reason``        optional operator-supplied free-form string
  * ``issued_at``     ISO 8601 UTC timestamp
  * ``server_id``     daemon that processed the event

The same SigningService key signs both forms — one key, one
verifier model regardless of mode.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterator, List, Optional


_LOCK = threading.Lock()


_HEX_CHARS = frozenset("0123456789abcdef")


def _safe_agent_id(agent_id: str) -> str:
    text = agent_id.strip().lower()
    if len(text) != 64 or any(c not in _HEX_CHARS for c in text):
        raise ValueError(f"agent_id is not 64-char hex: {agent_id!r}")
    return text


class AuditLifecycleStore:
    """Append-only per-agent lifecycle event log.

    Construction is cheap — the root directory is created lazily on
    first write. Writes are serialized through a module lock so
    concurrent ACTIVATE/REVOKE handlers can't corrupt the file.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    def append(self, agent_id: str, line: str) -> None:
        """Append one already-encoded lifecycle event line to the
        agent's stream.

        ``line`` is whatever the caller wants stored — JWS Compact
        for ``mode = jws`` (the default) or ``cose:<base64url>``
        for ``mode = scitt``. Both are ASCII-safe by construction.

        Atomic per-write (POSIX append guarantees an atomic write up
        to PIPE_BUF; lifecycle events are tiny, well under the
        limit). The module lock additionally serializes writes
        within the daemon process.
        """
        safe = _safe_agent_id(agent_id)
        path = self._path_for(safe)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="ascii") as f:
                f.write(line + "\n")

    def append_cose(self, agent_id: str, cose_bytes: bytes) -> str:
        """Convenience wrapper for SCITT mode. Base64url-encodes the
        COSE_Sign1 bytes with a ``cose:`` prefix and appends. Returns
        the encoded line so callers can compute the audit_id from
        the original bytes if they need to."""
        import base64 as _b64
        encoded = _b64.urlsafe_b64encode(cose_bytes).rstrip(b"=").decode("ascii")
        line = "cose:" + encoded
        self.append(agent_id, line)
        return line

    def read_all(self, agent_id: str) -> List[str]:
        """Return every JWS line ever appended for ``agent_id``,
        oldest first. Empty list when the agent has no lifecycle
        events recorded (never had one, or an attacker-supplied id)."""
        try:
            safe = _safe_agent_id(agent_id)
        except ValueError:
            return []
        path = self._path_for(safe)
        try:
            text = path.read_text(encoding="ascii")
        except FileNotFoundError:
            return []
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()]

    def iter_events(self, agent_id: str) -> Iterator[str]:
        """Generator yielding one JWS per recorded event. Useful when
        an agent has a long lifecycle history and reading all events
        eagerly would waste memory. Today the lifecycle store is
        line-oriented so this is just a thin wrapper around
        :meth:`read_all`; a future revision may stream from disk."""
        for jws in self.read_all(agent_id):
            yield jws

    def _path_for(self, safe_agent_id: str) -> Path:
        return self.root / f"{safe_agent_id}.jsonl"


def default_lifecycle_root() -> Path:
    """Platform-appropriate default lifecycle directory."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "agtp" / "audit" / "lifecycle"
    return Path.home() / ".agtp" / "audit" / "lifecycle"


__all__ = [
    "AuditLifecycleStore",
    "default_lifecycle_root",
]
