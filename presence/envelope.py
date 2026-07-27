"""
The ``ans_signature`` signed-response envelope (PDD §6.3 / §7.4).

Every ranked DISCOVER response is signed by the responding coordinator (or
ANS) over its result set, so a requester can confirm the results were not
tampered with in transit and came from the expected governance key. The
signature covers the canonical (RFC-8785-shaped, sorted-key) form of the
``results`` array.

    ans_signature = {
        "algorithm": "EdDSA",
        "key_id": "<responder key id>",
        "value": "<base64url(no pad) of the 64-byte Ed25519 signature>",
    }

The requester resolves the responder's public key (via the responder's
manifest — an ANS resolves its own governance key through its Agent
Manifest Document) and calls :func:`verify_result_set`. Unsigned or
invalid responses MUST be rejected.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ALGORITHM = "EdDSA"


def _canonical_bytes(results: List[Dict[str, Any]]) -> bytes:
    """RFC-8785-shaped canonical JSON of the result set: sorted keys, no
    whitespace. Both signer and verifier canonicalize identically."""
    return json.dumps(results, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_result_set(signing_service, results: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Produce the ``ans_signature`` object over ``results`` using a
    :class:`server.signing.SigningService`.
    """
    signature = signing_service.sign(_canonical_bytes(results))
    return {
        "algorithm": ALGORITHM,
        "key_id": signing_service.key_id,
        "value": _b64url(signature),
    }


def verify_result_set(
    public_key: Ed25519PublicKey,
    results: List[Dict[str, Any]],
    ans_signature: Optional[Dict[str, Any]],
) -> bool:
    """
    Verify an ``ans_signature`` against ``results`` with the responder's
    Ed25519 public key. Returns False (never raises) on any problem —
    missing signature, wrong algorithm, malformed value, or bad signature.
    """
    if not isinstance(ans_signature, dict):
        return False
    if ans_signature.get("algorithm") != ALGORITHM:
        return False
    value = ans_signature.get("value")
    if not isinstance(value, str):
        return False
    try:
        signature = _unb64url(value)
    except (ValueError, TypeError):
        return False
    try:
        public_key.verify(signature, _canonical_bytes(results))
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature and friends
        return False


def public_key_from_raw(raw: bytes) -> Ed25519PublicKey:
    """Rebuild an Ed25519 public key from its 32 raw bytes (as a
    coordinator/ANS publishes via ``public_key_raw()``)."""
    return Ed25519PublicKey.from_public_bytes(raw)
