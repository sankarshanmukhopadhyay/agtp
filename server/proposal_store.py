"""
In-process store for asynchronous PROPOSE evaluations (§7).

When a server opts into ``[policies.synthesis] async_evaluation_enabled
= true``, the PROPOSE handler returns 261 Negotiation In Progress
with a ``proposal_id``. The agent polls ``QUERY /proposals/{proposal_id}``
to retrieve the proposal's current state. While the evaluation is in
progress the poll returns 261; once the evaluation resolves the poll
returns 263 (accepted) or 463 (rejected) with the final result.

The store is intentionally in-process and non-durable: a server
restart loses the in-flight proposals. Durable storage is future
work (the user's §7 prompt scopes this out explicitly).

Concurrency model
-----------------

Tests drive the state machine by calling :meth:`resolve_accepted` or
:meth:`resolve_rejected` directly; production deployments wire those
calls into whatever evaluation engine they prefer (human review
pipeline, policy engine, external service consultation). The store
itself does not schedule background work — it's a transition target.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


_PROPOSAL_ID_PREFIX = "prop-"


def new_proposal_id() -> str:
    """Generate a URL-safe proposal id (e.g. ``prop-AbCdEfGhIjKl``)."""
    return _PROPOSAL_ID_PREFIX + secrets.token_urlsafe(9)


def hash_proposal_body(body: Any) -> str:
    """
    Hash a proposal body deterministically for audit correlation.

    Accepts either a parsed dict (the post-validation form) or the
    raw bytes from the wire. The hash is SHA-256, hex-encoded; the
    audit log uses the first 16 hex chars to keep entries terse.
    """
    if isinstance(body, (bytes, bytearray)):
        material = bytes(body)
    elif isinstance(body, dict):
        material = json.dumps(body, sort_keys=True).encode("utf-8")
    else:
        material = str(body).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass
class ProposalRecord:
    """One in-flight proposal's full state."""

    proposal_id: str
    agent_id: str
    proposal_body: Dict[str, Any]
    state: str  # "pending" | "accepted" | "rejected"
    evaluation_started_at: datetime
    deadline: datetime
    persistent: bool = False
    requested_seconds: Optional[float] = None
    # Resolution fields. ``result`` carries the proposal_approved /
    # proposal_rejected response body for the poll endpoint to
    # forward verbatim.
    result_status: Optional[int] = None
    result_body: Optional[Dict[str, Any]] = None
    resolved_at: Optional[datetime] = None


class ProposalStore:
    """
    Thread-safe map of ``proposal_id -> ProposalRecord``.

    The PROPOSE handler creates pending records via :meth:`create`;
    the evaluation pipeline (test code or production engine)
    transitions them via :meth:`resolve_accepted` or
    :meth:`resolve_rejected`. ``QUERY /proposals/{proposal_id}``
    reads via :meth:`lookup`.
    """

    def __init__(self, *, max_evaluation_seconds: float = 600.0) -> None:
        self._records: Dict[str, ProposalRecord] = {}
        self._lock = threading.Lock()
        self.max_evaluation_seconds = float(max_evaluation_seconds)

    # ----- creation -----

    def create(
        self,
        *,
        agent_id: str,
        proposal_body: Dict[str, Any],
        persistent: bool = False,
        requested_seconds: Optional[float] = None,
    ) -> str:
        """Register a new pending proposal; return its id."""
        now = datetime.now(tz=timezone.utc)
        record = ProposalRecord(
            proposal_id=new_proposal_id(),
            agent_id=agent_id,
            proposal_body=dict(proposal_body),
            state="pending",
            evaluation_started_at=now,
            deadline=now + timedelta(seconds=self.max_evaluation_seconds),
            persistent=persistent,
            requested_seconds=requested_seconds,
        )
        with self._lock:
            self._records[record.proposal_id] = record
        return record.proposal_id

    # ----- lookup -----

    def lookup(self, proposal_id: str) -> Optional[ProposalRecord]:
        with self._lock:
            return self._records.get(proposal_id)

    # ----- state transitions -----

    def resolve_accepted(
        self,
        proposal_id: str,
        *,
        body: Dict[str, Any],
    ) -> bool:
        """Mark the proposal as accepted. ``body`` is the
        ``proposal_approved`` response body the poll endpoint will
        forward. Returns True on success, False if the proposal is
        unknown or already resolved."""
        return self._resolve(
            proposal_id,
            state="accepted",
            status=263,
            body=body,
        )

    def resolve_rejected(
        self,
        proposal_id: str,
        *,
        body: Dict[str, Any],
    ) -> bool:
        """Mark the proposal as rejected. Same shape as
        :meth:`resolve_accepted`."""
        return self._resolve(
            proposal_id,
            state="rejected",
            status=463,
            body=body,
        )

    def _resolve(
        self,
        proposal_id: str,
        *,
        state: str,
        status: int,
        body: Dict[str, Any],
    ) -> bool:
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            record = self._records.get(proposal_id)
            if record is None or record.state != "pending":
                return False
            record.state = state
            record.result_status = status
            record.result_body = dict(body)
            record.resolved_at = now
        return True

    # ----- accessors used by the PROPOSE handler -----

    def evaluation_started_at(self, proposal_id: str) -> Optional[str]:
        with self._lock:
            record = self._records.get(proposal_id)
        if record is None:
            return None
        return record.evaluation_started_at.isoformat().replace("+00:00", "Z")

    def max_evaluation_duration_str(self) -> str:
        from server.synthesis_duration import format_duration_seconds
        return format_duration_seconds(self.max_evaluation_seconds)

    # ----- maintenance -----

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def sweep_expired(self) -> int:
        """Walk pending proposals; mark any past their deadline as
        rejected with reason ``"evaluation-timeout"``. Returns the
        count expired. Callable from a background thread or from the
        boot sweep alongside synthesis invalidation."""
        from core import status as status_codes
        now = datetime.now(tz=timezone.utc)
        expired_ids: list = []
        with self._lock:
            for pid, record in self._records.items():
                if record.state == "pending" and now >= record.deadline:
                    expired_ids.append(pid)
        for pid in expired_ids:
            body = {
                "error": {
                    "code": "proposal-rejected",
                    "reason": status_codes.PROPOSAL_REASON_POLICY_REFUSED,
                    "explanation": (
                        "evaluation deadline exceeded before a decision "
                        "could be reached"
                    ),
                }
            }
            self._resolve(pid, state="rejected", status=463, body=body)
        return len(expired_ids)


__all__ = [
    "ProposalRecord",
    "ProposalStore",
    "hash_proposal_body",
    "new_proposal_id",
]
