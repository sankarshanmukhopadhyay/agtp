"""
Presence-record signing (PDD §6.1 "Gossip" / §11.1).

A presence record can be signed by the announcing agent so that:

  * a relay or peer coordinator cannot **mutate** the record (change its
    capabilities, trust posture, or visibility) — any change breaks the
    signature; and
  * with the key→Agent-ID binding (:func:`binds_to_genesis`), a party
    cannot **forge** a record for an *existing* agent whose Agent-ID is the
    hash of a Genesis carrying a different public key.

The signature covers the canonical (RFC-8785-shaped) form of the record's
identity + discovery content, and the announcing agent's Ed25519 public key
is carried inline so a verifier can check integrity without a key lookup::

    record.signature = {
        "alg": "EdDSA",
        "public_key": "<base64url raw 32 bytes>",
        "value":      "<base64url 64-byte signature>",
    }

Integrity verification (:func:`verify_record`) needs only the record.
Identity binding (:func:`binds_to_genesis`) additionally needs the agent's
Genesis; where a coordinator cannot resolve one (a foreign agent), it falls
back to integrity-only, which is trust-on-first-use for the key.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SIGNING_ALG = "EdDSA"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def signing_payload(record) -> bytes:
    """
    Canonical bytes a signature covers: the record's identity and its
    discovery-facing content (so neither the identity nor what a query
    returns can be altered without detection). Deliberately excludes the
    signature itself and node-local fields (attachment).
    """
    payload = {
        "agent_id": record.agent_id,
        "result_entry": record.result_entry,
        "visibility": record.visibility.to_dict(),
        "scopes": list(record.scopes),
        "announced_at": record.announced_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_record(record, signing_service) -> None:
    """
    Sign ``record`` in place with an agent's :class:`SigningService`,
    embedding the public key so verifiers need no key lookup.
    """
    signature = signing_service.sign(signing_payload(record))
    record.signature = {
        "alg": SIGNING_ALG,
        "public_key": _b64url(signing_service.public_key_raw()),
        "value": _b64url(signature),
    }


def record_public_key(record) -> Optional[Ed25519PublicKey]:
    """The Ed25519 public key embedded in the record's signature, or None."""
    sig = record.signature
    if not isinstance(sig, dict):
        return None
    pk = sig.get("public_key")
    if not isinstance(pk, str):
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(_unb64url(pk))
    except (ValueError, TypeError):
        return None


def verify_record(record) -> bool:
    """
    Integrity check: the record carries a well-formed EdDSA signature that
    verifies over its canonical content using the embedded public key.
    Returns False (never raises) for unsigned or tampered records.
    """
    sig = record.signature
    if not isinstance(sig, dict) or sig.get("alg") != SIGNING_ALG:
        return False
    value = sig.get("value")
    public_key = record_public_key(record)
    if public_key is None or not isinstance(value, str):
        return False
    try:
        public_key.verify(_unb64url(value), signing_payload(record))
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature and friends
        return False


def binds_to_genesis(record, genesis) -> bool:
    """
    Identity binding: the key that signed the record is the agent's Genesis
    key, and the Genesis hashes to the record's Agent-ID. Together with
    :func:`verify_record` this prevents forging a record for an existing
    agent (you would need that agent's private key).
    """
    if genesis is None:
        return False
    sig = record.signature if isinstance(record.signature, dict) else {}
    record_pk = sig.get("public_key")
    if not record_pk:
        return False
    try:
        from core.key_encoding import detect_format, pem_to_b64url_raw
        genesis_pk = genesis.agent_public_key or ""
        if detect_format(genesis_pk) == "pem":
            genesis_pk = pem_to_b64url_raw(genesis_pk)
        genesis_pk = genesis_pk.strip()
    except Exception:  # noqa: BLE001
        return False
    if record_pk.strip() != genesis_pk:
        return False
    try:
        return genesis.canonical_agent_id() == record.agent_id
    except Exception:  # noqa: BLE001
        return False
