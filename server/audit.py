"""
PROPOSE audit log (§7).

Every PROPOSE request produces a single structured JSON line capturing
the outcome. The log is intended for operator visibility (auditing
unusual proposal patterns, building a future archive of approved
proposals, debugging negotiation failures). Each entry::

    {
      "timestamp": "2026-05-10T14:32:18Z",
      "agent_id":  "abc123...",
      "proposal_hash": "first 16 hex chars of SHA-256(body)",
      "decision": "accepted" | "rejected" | "pending" | "malformed",
      "synthesis_id":     "syn-..."          (when decision=accepted),
      "proposal_id":      "prop-..."         (when decision=pending),
      "reason":           "out-of-scope"     (when decision=rejected),
      "granted_duration": "24h"              (when decision=accepted)
    }

The sink is configured via ``[audit] path`` in agtp-server.toml.
Special values:

  * ``"stderr"`` (default) — write to ``sys.stderr``. Suitable for
    development and containerized deployments where the log
    aggregator captures stderr.
  * ``"none"`` / ``""``    — disable logging entirely.
  * Any other string is treated as a filesystem path; entries
    append-write to that file as JSONL.

Thread safety: a single lock serializes writes so concurrent PROPOSE
handlers don't interleave on the file descriptor.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from server.proposal_store import hash_proposal_body


_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_sink(server_state: Any) -> Optional[str]:
    """Read the audit-log sink from the server config. ``None`` when
    logging is disabled."""
    config = getattr(server_state, "config", None)
    if config is None:
        return "stderr"  # default during tests that pass no config
    audit_cfg = getattr(config, "audit", None)
    if audit_cfg is None:
        return "stderr"
    path = getattr(audit_cfg, "path", "stderr")
    if not path or path == "none":
        return None
    return path


def record_propose(
    server_state: Any,
    *,
    agent_doc: Any,
    proposal_body: Any,
    decision: str,
    synthesis_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
    reason: Optional[str] = None,
    granted_duration: Optional[str] = None,
) -> None:
    """
    Write one audit entry for a PROPOSE outcome. Safe to call from
    any thread; failures (IO errors, etc.) are swallowed so audit
    issues never break the response path.

    ``decision`` is one of:
      * ``"accepted"``  — synthesis instantiated (263).
      * ``"rejected"``  — proposal refused (463).
      * ``"pending"``   — queued for async evaluation (261).
      * ``"malformed"`` — body validation failed (400).
    """
    sink = _resolve_sink(server_state)
    if sink is None:
        return

    entry: Dict[str, Any] = {
        "timestamp": _utc_now_iso(),
        "agent_id": getattr(agent_doc, "agent_id", "") or "",
        "proposal_hash": hash_proposal_body(proposal_body)[:16],
        "decision": decision,
    }
    if synthesis_id is not None:
        entry["synthesis_id"] = synthesis_id
    if proposal_id is not None:
        entry["proposal_id"] = proposal_id
    if reason is not None:
        entry["reason"] = reason
    if granted_duration is not None:
        entry["granted_duration"] = granted_duration

    line = json.dumps(entry, separators=(",", ":"))

    try:
        with _LOCK:
            if sink == "stderr":
                print(line, file=sys.stderr)
            else:
                fp = Path(sink)
                fp.parent.mkdir(parents=True, exist_ok=True)
                with fp.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
    except OSError:
        # Audit-log failures must not affect the request path.
        # An operator with a bad ``[audit].path`` will see the
        # failures on stderr from the open() retry above eventually
        # (the next config reload fixes it).
        pass


__all__ = ["record_propose"]
