"""
Presence record and visibility shapes.

A :class:`PresenceRecord` is what an agent publishes with ANNOUNCE and
what PROBE and the population query read back. Its serialized form is the
gossip payload described in the PDD §7.2::

    {
      "agent_id": "4f7e1a2b9c3d...",
      "visibility": {
        "presence_mode": "tier-scoped",
        "disclosure_mode": "capabilities",
        "audience_scope": "tier:2 AND capability:booking"
      },
      "scopes": ["{capability: booking, region: us}", "{tier: 2}"],
      "timestamp": "2026-07-21T18:00:00Z",
      "signature": {"alg": "ES256", "jws": "[base64]"}
    }

M1 scope
--------
The full three-axis visibility model (presence/disclosure/audience) is an
M2 concern. M1 records default to ``public`` presence with ``capabilities``
disclosure and an empty (everyone) audience, and the coordinator does not
enforce the axes yet. The fields exist now so the wire shape is stable and
M2 fills in enforcement without a record-format change.

The ``signature`` slot is likewise carried but unverified in M1 (the v00
codebase carries zero crypto on the presence path by design; JWS
verification lands in M3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


#: Default announcement TTL, in seconds. TTL-based aging is not enforced
#: until M2; the field is recorded now so records are already TTL-shaped.
DEFAULT_TTL_SECONDS = 60


def utc_now_iso() -> str:
    """Z-suffixed ISO-8601 UTC timestamp (second precision)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class Visibility:
    """
    The declared visibility posture of a presence record.

    Three orthogonal axes (PDD §6.1). M1 uses only the defaults; M2 wires
    enforcement and sources the certificate-declared maximum envelope from
    the ``presence-visibility`` AGTP-CERT extension, with the runtime
    ``Presence-Mode`` header allowed to reduce within it.

    * ``presence_mode``   — public | tier-scoped | owner-domain |
                            explicit-only | invisible
    * ``disclosure_mode`` — full | capabilities | identity-only |
                            existence-only
    * ``audience_scope``  — audience expression (e.g.
                            ``"tier:2 AND capability:booking"``); empty
                            string means "everyone".
    """

    presence_mode: str = "public"
    disclosure_mode: str = "capabilities"
    audience_scope: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "presence_mode": self.presence_mode,
            "disclosure_mode": self.disclosure_mode,
            "audience_scope": self.audience_scope,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Visibility":
        if not isinstance(data, dict):
            return cls()
        return cls(
            presence_mode=str(data.get("presence_mode", "public")),
            disclosure_mode=str(data.get("disclosure_mode", "capabilities")),
            audience_scope=str(data.get("audience_scope", "")),
        )


@dataclass
class PresenceRecord:
    """
    A single agent's presence in the substrate.

    ``result_entry`` is the lightweight, discovery-safe projection of the
    agent (identity + capability + trust posture). It never carries a
    network address: the whole point of the principals-not-hosts model is
    that discovery returns an Agent-ID, not an endpoint. Routing to the
    agent goes through the coordinator/relay carried in ``attachment``,
    which is coordinator-internal and is deliberately excluded from every
    discovery-facing payload.
    """

    agent_id: str
    result_entry: Dict[str, Any]
    scopes: List[str] = field(default_factory=list)
    visibility: Visibility = field(default_factory=Visibility)
    announced_at: str = field(default_factory=utc_now_iso)
    #: Monotonic-ish wall-clock epoch the record was announced at, used
    #: for TTL aging (M2). Kept alongside the ISO ``announced_at`` so
    #: expiry math needs no string parsing.
    announced_at_epoch: float = field(default_factory=time.time)
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    #: The subject's owner-domain, consulted by the ``owner-domain``
    #: presence mode. Coordinator-internal; not a discovery-facing field.
    owner_domain: Optional[str] = None
    #: Relay routing hint. Coordinator-internal; never surfaced to
    #: discovery payloads. In M1 the relay is the coordinator itself.
    attachment: Optional[Dict[str, Any]] = None
    #: {"alg": ..., "jws": ...}. Present-but-unverified in M1.
    signature: Optional[Dict[str, Any]] = None

    def expires_at(self) -> Optional[float]:
        """Epoch at which this record ages out, or None if it never does
        (``ttl_seconds <= 0`` disables aging)."""
        if self.ttl_seconds <= 0:
            return None
        return self.announced_at_epoch + self.ttl_seconds

    def is_expired(self, now: float) -> bool:
        """True once ``now`` has passed the record's TTL window."""
        expiry = self.expires_at()
        return expiry is not None and now >= expiry

    def to_announcement_dict(self) -> Dict[str, Any]:
        """The §7.2 gossip/announcement shape (no attachment)."""
        return {
            "agent_id": self.agent_id,
            "visibility": self.visibility.to_dict(),
            "scopes": list(self.scopes),
            "timestamp": self.announced_at,
            "signature": self.signature,
        }

    def to_result_entry(self) -> Dict[str, Any]:
        """
        The discovery-facing projection returned by the population query.

        Guaranteed free of any network address so callers route by
        Agent-ID, not by endpoint.
        """
        return dict(self.result_entry)

    def to_gossip_dict(self) -> Dict[str, Any]:
        """
        The self-contained record shape exchanged during gossip
        anti-entropy (:mod:`presence.gossip`). Carries everything a peer
        coordinator needs to serve discovery and PROBE for this agent —
        identity, capabilities, trust posture, visibility, scopes, and the
        origin announce time used for conflict resolution.

        ``attachment`` is deliberately omitted: it is the origin
        coordinator's relay hint (node-local routing), and cross-node
        message routing is an M4 rendezvous concern. Discovery never
        surfaces it regardless.
        """
        return {
            "agent_id": self.agent_id,
            "result_entry": dict(self.result_entry),
            "scopes": list(self.scopes),
            "visibility": self.visibility.to_dict(),
            "owner_domain": self.owner_domain,
            "announced_at": self.announced_at,
            "announced_at_epoch": self.announced_at_epoch,
            "ttl_seconds": self.ttl_seconds,
            "signature": self.signature,
        }

    @classmethod
    def from_gossip_dict(cls, data: Dict[str, Any]) -> "PresenceRecord":
        """Reconstruct a record received from a peer during gossip."""
        return cls(
            agent_id=str(data["agent_id"]),
            result_entry=dict(data.get("result_entry") or {}),
            scopes=list(data.get("scopes") or []),
            visibility=Visibility.from_dict(data.get("visibility")),
            owner_domain=data.get("owner_domain"),
            announced_at=str(data.get("announced_at") or utc_now_iso()),
            announced_at_epoch=float(data.get("announced_at_epoch") or 0.0),
            ttl_seconds=int(data.get("ttl_seconds") or DEFAULT_TTL_SECONDS),
            attachment=None,  # relay hint is node-local; not propagated
            signature=data.get("signature"),
        )
