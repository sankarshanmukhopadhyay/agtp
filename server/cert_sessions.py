"""
Per-certificate-serial active session registry + revocation broadcast.

AGTP-CERT §6.2-6.3 mandates that a server hosting agents over mTLS
maintain a per-cert-serial map of active sessions and tear those
sessions down when the cert is revoked. The broadcast envelope is
an AGTP ``NOTIFY`` with ``event_type=certificate_revoked``,
recipient ``infrastructure:broadcast``, urgency ``critical``;
infrastructure receivers MUST terminate all sessions for the
named ``subject_agent_id`` before serving its next request and
SHOULD achieve this within 30 seconds.

This module provides three pieces:

  * :class:`CertSessionRegistry` — the in-process map keyed by
    cert serial (decimal integer string), each entry a set of
    session_ids the cert authorized. Thread-safe.
  * :func:`build_revocation_notify_envelope` — produces the
    NOTIFY body shape AGTP-CERT §6.2 specifies, ready to ride a
    ``handle_notify`` call. The actual emission (which depends
    on deployment topology — broadcast vs. fan-out vs. external
    queue) is left to the caller; this helper produces the
    canonical envelope so every emitter agrees on the bytes.
  * :func:`apply_revocation_notify` — the receiver side. Given a
    parsed NOTIFY body and a registry, tears down every session
    bound to the named cert serial AND every session bound to a
    different serial that resolves to the same
    ``subject_agent_id`` (cert rotation case).

v00 scope: the registry data structure and helper functions ship
fully tested. Full distributed broadcast wiring — emission on
REVOKE handlers, multi-daemon fan-out, the 30-second deadline
enforcement loop — is operational work that depends on
deployment topology and lands as a follow-up.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set


@dataclass
class CertSessionRegistry:
    """Per-cert-serial active session registry.

    The registry maps ``cert_serial`` (decimal string of the X.509
    serial number) to:

      * the ``subject_agent_id`` the cert authorized (so revocation
        events naming an agent_id can sweep across serials for
        cert rotations), and
      * the set of session_ids currently active under that cert.

    All operations are guarded by a single lock — contention is
    fine for the expected access pattern (per-request register
    + sporadic revoke sweep).
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _serial_to_sessions: Dict[str, Set[str]] = field(default_factory=dict)
    _serial_to_agent: Dict[str, str] = field(default_factory=dict)

    # ----- writers -----

    def register(
        self,
        *,
        cert_serial: str,
        session_id: str,
        subject_agent_id: str,
    ) -> None:
        """Bind a session to a cert serial.

        Called on the first request a session presents; subsequent
        registrations of the same (serial, session_id) tuple are
        idempotent. The ``subject_agent_id`` is recorded so the
        revocation receiver can find all serials authorizing the
        same agent (the cert-rotation case where Agent-ID stays
        constant across keys).
        """
        if not cert_serial or not session_id:
            return
        with self._lock:
            self._serial_to_sessions.setdefault(cert_serial, set()).add(
                session_id,
            )
            if subject_agent_id:
                self._serial_to_agent[cert_serial] = subject_agent_id

    def deregister_session(self, session_id: str) -> None:
        """Clean up a session that has terminated normally. Walks
        every serial to find the session — cost is O(serials) but
        the per-serial set lookup is O(1)."""
        if not session_id:
            return
        with self._lock:
            for sessions in self._serial_to_sessions.values():
                sessions.discard(session_id)

    # ----- readers -----

    def sessions_for_serial(self, cert_serial: str) -> List[str]:
        with self._lock:
            return sorted(
                self._serial_to_sessions.get(cert_serial, set())
            )

    def sessions_for_agent(self, subject_agent_id: str) -> List[str]:
        """Every session authorized by any cert that maps to this
        agent — across all serials for the agent (cert rotation
        produces multiple serials authorizing the same agent_id)."""
        if not subject_agent_id:
            return []
        out: Set[str] = set()
        target = subject_agent_id.lower()
        with self._lock:
            for serial, mapped_agent in self._serial_to_agent.items():
                if mapped_agent.lower() == target:
                    out |= self._serial_to_sessions.get(serial, set())
        return sorted(out)

    def serials_for_agent(self, subject_agent_id: str) -> List[str]:
        if not subject_agent_id:
            return []
        target = subject_agent_id.lower()
        with self._lock:
            return sorted(
                serial
                for serial, mapped in self._serial_to_agent.items()
                if mapped.lower() == target
            )

    # ----- revocation sweep -----

    def revoke_serial(self, cert_serial: str) -> List[str]:
        """Drop every session bound to a single cert serial and
        return the terminated session_ids. Use when revocation
        targets a specific serial (single-cert revoke)."""
        if not cert_serial:
            return []
        with self._lock:
            sessions = sorted(
                self._serial_to_sessions.pop(cert_serial, set())
            )
            self._serial_to_agent.pop(cert_serial, None)
        return sessions

    def revoke_agent(self, subject_agent_id: str) -> List[str]:
        """Drop every session for every serial that maps to the
        given subject_agent_id. The agent-wide hammer used when
        the revocation envelope identifies the agent rather than a
        single cert."""
        terminated: List[str] = []
        for serial in self.serials_for_agent(subject_agent_id):
            terminated.extend(self.revoke_serial(serial))
        return terminated

    def __len__(self) -> int:
        with self._lock:
            return sum(
                len(s) for s in self._serial_to_sessions.values()
            )


# ---------------------------------------------------------------------------
# Revocation NOTIFY envelope (AGTP-CERT §6.2).
# ---------------------------------------------------------------------------


def build_revocation_notify_envelope(
    *,
    subject_agent_id: str,
    cert_serial: str,
    reason: str = "revoked",
    revoked_at: str = "",
    issuer: str = "",
) -> Dict[str, Any]:
    """Build the canonical ``NOTIFY`` body shape AGTP-CERT §6.2
    mandates for cert revocation propagation.

    The envelope's outer fields are the standard NOTIFY shape;
    the inner ``payload`` carries cert-specific details so
    receivers can decide whether to sweep by serial alone or by
    serial-plus-agent (cert rotation).

    Callers serialize the result and present it as the body of a
    ``NOTIFY`` request to whichever recipients the deployment's
    revocation topology fans out to. The actual transport
    decision (single broadcast endpoint, multi-server fan-out,
    external queue) is operator policy; this helper guarantees
    every emitter produces the same envelope bytes.
    """
    return {
        "event_type": "certificate_revoked",
        "recipient": "infrastructure:broadcast",
        "urgency": "critical",
        "payload": {
            "subject_agent_id": subject_agent_id,
            "cert_serial": cert_serial,
            "reason": reason,
            "revoked_at": revoked_at,
            "issuer": issuer,
        },
    }


def apply_revocation_notify(
    notify_body: Dict[str, Any],
    registry: CertSessionRegistry,
) -> Dict[str, Any]:
    """Receiver-side handler for a ``certificate_revoked`` NOTIFY.

    Inspects the envelope, sweeps every matching session out of
    the registry, and returns a structured summary the caller can
    log or echo back. The returned dict carries:

      * ``terminated_sessions`` — session_ids the sweep removed
      * ``serials_swept``       — cert serials the sweep cleared
      * ``subject_agent_id``    — the agent identity targeted

    Best-effort: malformed or non-certificate_revoked envelopes
    return an empty result rather than raising — callers can
    branch on len(``terminated_sessions``).
    """
    out: Dict[str, Any] = {
        "terminated_sessions": [],
        "serials_swept": [],
        "subject_agent_id": "",
    }
    if not isinstance(notify_body, dict):
        return out
    if notify_body.get("event_type") != "certificate_revoked":
        return out
    payload = notify_body.get("payload") or {}
    if not isinstance(payload, dict):
        return out
    subject = str(payload.get("subject_agent_id") or "").strip()
    serial = str(payload.get("cert_serial") or "").strip()
    out["subject_agent_id"] = subject

    terminated: List[str] = []
    serials: List[str] = []
    if serial:
        sessions = registry.revoke_serial(serial)
        if sessions:
            terminated.extend(sessions)
            serials.append(serial)
    if subject:
        # Also sweep any other serials that map to this agent
        # (cert rotation case). revoke_agent walks the full set;
        # if the explicit serial was already swept, only the
        # remaining serials get hit.
        agent_serials = registry.serials_for_agent(subject)
        for s in agent_serials:
            if s == serial:
                continue
            sessions = registry.revoke_serial(s)
            if sessions:
                terminated.extend(sessions)
                serials.append(s)
    out["terminated_sessions"] = sorted(set(terminated))
    out["serials_swept"] = sorted(set(serials))
    return out


__all__ = [
    "CertSessionRegistry",
    "apply_revocation_notify",
    "build_revocation_notify_envelope",
]
