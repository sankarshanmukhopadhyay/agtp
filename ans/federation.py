"""
Cross-ANS federation trust (PDD §10 "Naming federation").

A local ANS with insufficient results MAY forward a query to a *trusted*
peer ANS. Trust is bilateral and explicit: each side pins the other's
governance public key and holds a mutually-signed :class:`FederationRecord`
declaring the relationship — the tier mapping and query-forwarding limits.
The record is auditable and unilaterally revocable.

Real establishment additionally proves domain control (a DNS challenge) and
exchanges certificates out of band; that handshake is operational. What this
module implements is the durable artifact it produces — a signed record and
a pinned key — which is what the forwarding path in :mod:`ans.methods`
actually consumes: it verifies a peer's ``ans_signature`` against the pinned
key before trusting any federated result, so a forwarder can neither forge
results nor be impersonated.

Forwarding rules the callers enforce (not this module):
  * the forwarded query carries the original requester and scope unchanged;
  * a forwarder MUST NOT expand scope or lower trust requirements;
  * federation is single-hop here (a federated query is not re-forwarded),
    which bounds fan-out and prevents loops.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from presence.envelope import _b64url, _canonical_bytes, _unb64url


def federation_record(
    ans_a: str, ans_b: str, *,
    tier_mapping: Optional[Dict[str, int]] = None,
    forwarding_limit: int = 10,
    established_at: str = "",
) -> Dict[str, Any]:
    """The canonical (unsigned) bilateral record. ``ans_a``/``ans_b`` are the
    two ANS endpoints, sorted so both sides build byte-identical records."""
    lo, hi = sorted((ans_a, ans_b))
    return {
        "ans_a": lo,
        "ans_b": hi,
        "tier_mapping": tier_mapping or {},
        "forwarding_limit": forwarding_limit,
        "established_at": established_at,
    }


def _record_core(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Sign over the record minus its signatures, wrapped as a one-item list
    # so we reuse the envelope's canonicalizer.
    return [{k: v for k, v in record.items() if k != "signatures"}]


def sign_federation_record(record: Dict[str, Any], signing_service) -> None:
    """Add this party's signature to the record's ``signatures`` map, keyed
    by the signer's key id."""
    sigs = record.setdefault("signatures", {})
    value = signing_service.sign(_canonical_bytes(_record_core(record)))
    sigs[signing_service.key_id] = _b64url(value)


def verify_federation_record(record: Dict[str, Any], public_key: Ed25519PublicKey,
                             key_id: str) -> bool:
    """True if ``key_id``'s signature in the record verifies under
    ``public_key`` (i.e. that party really signed this relationship)."""
    sigs = record.get("signatures")
    if not isinstance(sigs, dict) or key_id not in sigs:
        return False
    try:
        public_key.verify(_unb64url(sigs[key_id]), _canonical_bytes(_record_core(record)))
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Peer:
    endpoint: str
    public_key_raw: bytes
    key_id: str
    record: Dict[str, Any] = field(default_factory=dict)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(self.public_key_raw)


class FederationTrust:
    """A local ANS's set of trusted peer ANS servers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._peers: Dict[str, Peer] = {}

    def add_peer(self, endpoint: str, public_key_raw: bytes, key_id: str,
                 record: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._peers[endpoint] = Peer(
                endpoint=endpoint, public_key_raw=public_key_raw,
                key_id=key_id, record=record or {},
            )

    def revoke_peer(self, endpoint: str) -> bool:
        with self._lock:
            return self._peers.pop(endpoint, None) is not None

    def peers(self) -> List[Peer]:
        with self._lock:
            return list(self._peers.values())

    def get(self, endpoint: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(endpoint)

    def __len__(self) -> int:
        with self._lock:
            return len(self._peers)


def establish_federation(a_registry, a_endpoint, b_registry, b_endpoint,
                         *, tier_mapping=None, forwarding_limit=10,
                         established_at="") -> Dict[str, Any]:
    """
    Establish bilateral federation between two ANS registries (each carrying
    a ``signing_service`` and a ``federation_trust``): build one canonical
    record, have both parties sign it, and pin each other's governance keys.
    Returns the mutually-signed record.

    This is the durable artifact of the (out-of-band) DNS-challenge +
    cert-exchange handshake — the pinned keys and signed record are what the
    forwarding path verifies against.
    """
    record = federation_record(
        a_endpoint, b_endpoint, tier_mapping=tier_mapping,
        forwarding_limit=forwarding_limit, established_at=established_at,
    )
    a_sign = a_registry.signing_service
    b_sign = b_registry.signing_service
    sign_federation_record(record, a_sign)
    sign_federation_record(record, b_sign)

    a_registry.federation_trust.add_peer(
        b_endpoint, b_sign.public_key_raw(), b_sign.key_id, record)
    b_registry.federation_trust.add_peer(
        a_endpoint, a_sign.public_key_raw(), a_sign.key_id, record)
    return record
