"""
Ed25519 public-key encoding helpers — PEM ↔ base64url-of-raw-bytes.

Background
----------

The IETF specs (draft-hood-agtp-identifiers, draft-hood-independent-agtp)
mandate ``manifest_issuer_public_key`` and ``Genesis.issuer_public_key``
be carried as the 32 raw Ed25519 public key bytes encoded with
base64url (no padding). The codebase historically stored these as PEM
(PKCS#8 SubjectPublicKeyInfo blocks), which is a richer container that
embeds the algorithm OID and a structured header. The two are
equivalent in cryptographic content (same 32 underlying bytes) but
have very different on-the-wire byte representations.

This module provides three things:

  1. :func:`pem_to_b64url_raw` and :func:`b64url_raw_to_pem` — the
     two conversions, both round-tripping for valid Ed25519 keys.
  2. :func:`fingerprint_b64url_raw` — the spec-canonical fingerprint
     (sha256 of the raw 32 bytes, hex), regardless of which input
     format you have on hand.
  3. :func:`detect_format` — given a key string, identify whether
     it's PEM or b64url-raw.

Migration story
---------------

Existing AgentGenesis and AgentDocument objects on disk store the
key in PEM. Re-encoding to b64url-raw would change the canonical
JSON of the document, which would change its sha256 hash, which
would change derived identifiers (Agent-ID, manifest_fingerprint).
Migrating storage format is therefore a v0 → v1 transition that
needs explicit handoff, not a transparent in-place upgrade.

For now, this module ships the helpers so:

  * Spec-conformant verifiers reading wire bytes in b64url-raw can
    accept our PEM-encoded documents by converting on the fly.
  * Wire emitters that need to publish spec-aligned bytes can
    convert before sending.
  * Cryptographic operations that need the spec's canonical
    fingerprint formula (sha256 of 32 raw bytes) have a single
    function to call regardless of which form they have.

The dataclasses themselves continue to store whatever format their
source file used — accepting both on read, emitting both unchanged
on write. The full storage-format flip awaits a future commit
that defines the v1 transition rules.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidKey, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class KeyEncodingError(ValueError):
    """Raised when a key string can't be parsed in either format."""


def pem_to_b64url_raw(pem: str) -> str:
    """Convert a PEM-encoded Ed25519 public key to the spec-aligned
    base64url-of-raw-bytes form (no padding).

    The input is the standard SubjectPublicKeyInfo PEM block:

        -----BEGIN PUBLIC KEY-----
        MCowBQYDK2VwAyEA...
        -----END PUBLIC KEY-----

    Output is the 32 raw Ed25519 public key bytes encoded with
    URL-safe base64 and no padding (the form RFC 8032 §5.1.2
    describes for canonical wire encoding).

    Raises :class:`KeyEncodingError` when the input isn't a valid
    Ed25519 PEM block.
    """
    try:
        pub = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise KeyEncodingError(
            f"input is not a valid PEM-encoded public key: {exc}"
        ) from exc
    if not isinstance(pub, Ed25519PublicKey):
        raise KeyEncodingError(
            "PEM input is not an Ed25519 public key "
            f"(got {type(pub).__name__})"
        )
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_raw_to_pem(b64url: str) -> str:
    """Convert a base64url-of-raw-bytes Ed25519 public key to PEM.

    Accepts either padded or unpadded base64url; the spec form is
    unpadded but tolerant parsing keeps things robust.

    Raises :class:`KeyEncodingError` on malformed input or wrong key
    length.
    """
    text = b64url.strip()
    if not text:
        raise KeyEncodingError("input is empty")
    # Restore padding if it was stripped (b64url canonical form).
    padded = text + ("=" * (-len(text) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise KeyEncodingError(
            f"input is not valid base64url: {exc}"
        ) from exc
    if len(raw) != 32:
        raise KeyEncodingError(
            f"raw Ed25519 public key must be 32 bytes; got {len(raw)}"
        )
    try:
        pub = Ed25519PublicKey.from_public_bytes(raw)
    except (InvalidKey, ValueError) as exc:
        raise KeyEncodingError(
            f"32 raw bytes are not a valid Ed25519 public key: {exc}"
        ) from exc
    pem_bytes = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem_bytes.decode("ascii")


def fingerprint_b64url_raw(key: str) -> str:
    """Return the spec-canonical fingerprint of an Ed25519 public key.

    Spec formula (AGTP-IDENTIFIERS / AGTP-CERT): the fingerprint is
    ``sha256(raw_ed25519_public_key_bytes)`` rendered as 64 lowercase
    hex characters. The input may be in either PEM or b64url-raw —
    this helper handles the conversion transparently.

    Use this when you need to compare keys cryptographically across
    encodings: two strings that look different (PEM vs. b64url-raw)
    but encode the same key produce the same fingerprint, while
    different keys always produce different fingerprints regardless
    of format.

    Raises :class:`KeyEncodingError` when the input isn't a valid
    key in either format.
    """
    fmt = detect_format(key)
    if fmt == "pem":
        # Round through cryptography to extract the raw 32 bytes.
        try:
            pub = serialization.load_pem_public_key(key.encode("utf-8"))
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise KeyEncodingError(f"invalid PEM: {exc}") from exc
        if not isinstance(pub, Ed25519PublicKey):
            raise KeyEncodingError("PEM is not Ed25519")
        raw = pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    else:
        # Already b64url-raw — extract the 32 bytes directly.
        text = key.strip()
        padded = text + ("=" * (-len(text) % 4))
        try:
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (binascii.Error, ValueError) as exc:
            raise KeyEncodingError(f"invalid b64url: {exc}") from exc
        if len(raw) != 32:
            raise KeyEncodingError(
                f"raw Ed25519 key must be 32 bytes; got {len(raw)}"
            )
    return hashlib.sha256(raw).hexdigest()


def detect_format(key: str) -> str:
    """Return ``"pem"`` or ``"b64url_raw"`` based on input shape.

    The detection is lightweight — PEM has a recognizable
    ``-----BEGIN PUBLIC KEY-----`` framing; anything else is
    treated as b64url-raw and validated by the conversion
    function when the caller actually needs the bytes. Useful
    for callers that want to branch on format without invoking
    the full parser.
    """
    if "-----BEGIN" in key:
        return "pem"
    return "b64url_raw"


__all__ = [
    "KeyEncodingError",
    "b64url_raw_to_pem",
    "detect_format",
    "fingerprint_b64url_raw",
    "pem_to_b64url_raw",
]
