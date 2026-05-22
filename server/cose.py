"""
Minimal COSE_Sign1 encoder/verifier for SCITT receipts.

Implements just the slice of CBOR (RFC 8949) and COSE (RFC 8152)
the AGTP-LOG transparency-log spec needs: signed identity-lifecycle
events emitted as RFC 9943 SCITT statements.

We hand-roll the CBOR codec rather than pulling in ``cbor2`` so
that turning on ``[audit].mode = scitt`` doesn't add a third-party
dependency. The surface this module supports — unsigned ints,
negative ints, byte strings, text strings, arrays, maps, single-byte
tags — is tiny and bounded by the COSE_Sign1 structure itself.

## COSE_Sign1 shape (RFC 8152 §4.2, tag 18)

```
COSE_Sign1 = [
    protected:   bstr,    # bstr-wrapped serialized CBOR map
    unprotected: map,     # empty for our messages
    payload:     bstr,    # signed bytes (the lifecycle event)
    signature:   bstr,    # 64-byte Ed25519 signature
]
```

Signing input is ``Sig_structure1`` (RFC 8152 §4.4):

```
Sig_structure1 = [
    context:        tstr = "Signature1",
    body_protected: bstr,
    external_aad:   bstr = h'',   (empty)
    payload:        bstr,
]
```

The protected header always carries:
  * ``alg = -8`` (EdDSA, RFC 8152 §8.2)
  * ``kid = <key_id>``  (text string)
  * ``typ = "application/agtp-lifecycle+json"``  (text string)

The payload is a UTF-8 JSON document — same shape as the JWS
payload the daemon writes in jws mode. SCITT verifiers that
understand application/agtp-lifecycle+json get a structured
record without needing AGTP-specific parsing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# COSE labels (RFC 8152 §3.1).
COSE_HEADER_ALG = 1     # alg
COSE_HEADER_KID = 4     # kid (key identifier)
COSE_HEADER_TYP = 16    # typ (content type marker)

# COSE algorithm identifiers (RFC 8152 §8).
COSE_ALG_EDDSA = -8

# CBOR tag for COSE_Sign1 (RFC 8152 §2).
CBOR_TAG_COSE_SIGN1 = 18

DEFAULT_TYP = "application/agtp-lifecycle+json"


class CoseError(Exception):
    """Raised on COSE encoding / decoding failures."""


# ---------------------------------------------------------------------------
# Minimal CBOR codec.
# ---------------------------------------------------------------------------


def _enc_head(major: int, n: int) -> bytes:
    """Encode a CBOR major-type head with an unsigned argument ``n``."""
    head = major << 5
    if n < 24:
        return bytes([head | n])
    if n < 256:
        return bytes([head | 24, n])
    if n < 65536:
        return bytes([head | 25]) + n.to_bytes(2, "big")
    if n < 2**32:
        return bytes([head | 26]) + n.to_bytes(4, "big")
    return bytes([head | 27]) + n.to_bytes(8, "big")


def cbor_encode(value: Any) -> bytes:
    """Encode a Python value as CBOR. Supports the subset COSE_Sign1
    needs: int, str, bytes, list, dict (with int / str keys)."""
    if value is None:
        return bytes([0xF6])  # null (major 7, value 22)
    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])  # major 7, 21/20
    if isinstance(value, int):
        if value >= 0:
            return _enc_head(0, value)
        return _enc_head(1, -1 - value)
    if isinstance(value, bytes):
        return _enc_head(2, len(value)) + value
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return _enc_head(3, len(encoded)) + encoded
    if isinstance(value, list):
        out = bytearray(_enc_head(4, len(value)))
        for item in value:
            out.extend(cbor_encode(item))
        return bytes(out)
    if isinstance(value, dict):
        # Canonical encoding per RFC 8949 §4.2.1: sort by encoded
        # key bytes (length-first then lexicographic). For our
        # purposes (small maps with integer or short string keys)
        # the simpler "sort keys by their CBOR encoding" rule applies.
        items: List[Tuple[bytes, bytes]] = []
        for k, v in value.items():
            items.append((cbor_encode(k), cbor_encode(v)))
        items.sort(key=lambda pair: pair[0])
        out = bytearray(_enc_head(5, len(items)))
        for k_bytes, v_bytes in items:
            out.extend(k_bytes)
            out.extend(v_bytes)
        return bytes(out)
    raise CoseError(f"cbor_encode: unsupported type {type(value).__name__}")


def _read_head(data: bytes, pos: int) -> Tuple[int, int, int]:
    """Read a CBOR head at ``data[pos]``. Returns (major, argument, new_pos)."""
    if pos >= len(data):
        raise CoseError("cbor_decode: unexpected end of input at head")
    b = data[pos]
    major = b >> 5
    info = b & 0x1F
    pos += 1
    if info < 24:
        return major, info, pos
    if info == 24:
        if pos >= len(data):
            raise CoseError("cbor_decode: short uint8 argument")
        return major, data[pos], pos + 1
    if info == 25:
        if pos + 2 > len(data):
            raise CoseError("cbor_decode: short uint16 argument")
        return major, int.from_bytes(data[pos:pos+2], "big"), pos + 2
    if info == 26:
        if pos + 4 > len(data):
            raise CoseError("cbor_decode: short uint32 argument")
        return major, int.from_bytes(data[pos:pos+4], "big"), pos + 4
    if info == 27:
        if pos + 8 > len(data):
            raise CoseError("cbor_decode: short uint64 argument")
        return major, int.from_bytes(data[pos:pos+8], "big"), pos + 8
    # 28-30 reserved; 31 indefinite-length (we don't emit/consume those).
    raise CoseError(f"cbor_decode: unsupported additional info {info}")


def _decode_at(data: bytes, pos: int) -> Tuple[Any, int]:
    major, arg, pos = _read_head(data, pos)
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major == 2:
        end = pos + arg
        if end > len(data):
            raise CoseError("cbor_decode: short bstr payload")
        return data[pos:end], end
    if major == 3:
        end = pos + arg
        if end > len(data):
            raise CoseError("cbor_decode: short tstr payload")
        return data[pos:end].decode("utf-8"), end
    if major == 4:
        out: List[Any] = []
        for _ in range(arg):
            item, pos = _decode_at(data, pos)
            out.append(item)
        return out, pos
    if major == 5:
        out_d: Dict[Any, Any] = {}
        for _ in range(arg):
            k, pos = _decode_at(data, pos)
            v, pos = _decode_at(data, pos)
            out_d[k] = v
        return out_d, pos
    if major == 6:
        # tag; decode the wrapped value. We return (tag_number, value)
        # only when the caller asked via cbor_decode_tag; otherwise
        # transparent.
        inner, pos = _decode_at(data, pos)
        # Encode the tag as a (tag, value) tuple so callers that care
        # can check it. CoseError if a verifier sees the wrong tag.
        return ("__tag__", arg, inner), pos
    if major == 7:
        if arg == 20:
            return False, pos
        if arg == 21:
            return True, pos
        if arg in (22, 23):
            return None, pos
    raise CoseError(f"cbor_decode: unsupported major type {major}")


def cbor_decode(data: bytes) -> Any:
    value, end = _decode_at(data, 0)
    if end != len(data):
        raise CoseError(
            f"cbor_decode: trailing bytes ({end} consumed of {len(data)})"
        )
    return value


# ---------------------------------------------------------------------------
# COSE_Sign1 build + verify.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoseSign1:
    """A signed lifecycle event in COSE_Sign1 form."""

    bytes_: bytes
    """The full CBOR-encoded tagged COSE_Sign1 bytes — what the
    daemon writes to the lifecycle stream."""

    payload: Dict[str, Any]
    """Decoded JSON payload, for caller convenience."""

    audit_id: str
    """``sha256(bytes_).hexdigest()`` — the chain-anchor for SCITT
    receipts (same role audit_id plays for JWS records)."""


def build_cose_sign1(
    *,
    private_key: Ed25519PrivateKey,
    payload_bytes: bytes,
    kid: str,
    typ: str = DEFAULT_TYP,
) -> bytes:
    """Sign ``payload_bytes`` with the supplied Ed25519 key and
    return CBOR-encoded tagged COSE_Sign1 bytes (RFC 8152 §4.2,
    CBOR tag 18).

    Caller is responsible for ``payload_bytes`` being the canonical
    form it wants signed — for AGTP lifecycle events this is the
    canonical JSON of the event payload, same shape JWS would carry.
    """
    protected_map: Dict[int, Any] = {
        COSE_HEADER_ALG: COSE_ALG_EDDSA,
        COSE_HEADER_KID: kid,
        COSE_HEADER_TYP: typ,
    }
    protected = cbor_encode(protected_map)

    # Sig_structure1 per RFC 8152 §4.4.
    sig_structure = cbor_encode([
        "Signature1",
        protected,
        b"",            # external_aad — empty
        payload_bytes,
    ])
    signature = private_key.sign(sig_structure)

    # COSE_Sign1 array per RFC 8152 §4.2.
    cose_sign1 = cbor_encode([
        protected,
        {},             # unprotected — empty
        payload_bytes,
        signature,
    ])

    # Tagged (tag 18) for clean self-identification on the wire.
    tagged = _enc_head(6, CBOR_TAG_COSE_SIGN1) + cose_sign1
    return tagged


def verify_cose_sign1(
    *,
    cose_bytes: bytes,
    public_key: Ed25519PublicKey,
) -> Dict[str, Any]:
    """Verify a COSE_Sign1 message and return its decoded protected
    header.

    Raises :class:`CoseError` on any structural failure (bad tag,
    wrong shape, signature invalid). Successful return means the
    signature is valid against ``public_key`` — the caller still
    needs to decide whether the embedded ``kid`` belongs to a
    trusted key.
    """
    decoded = cbor_decode(cose_bytes)
    if (
        not isinstance(decoded, tuple)
        or len(decoded) != 3
        or decoded[0] != "__tag__"
        or decoded[1] != CBOR_TAG_COSE_SIGN1
    ):
        raise CoseError("COSE_Sign1 message is not tagged 18")
    cose_array = decoded[2]
    if not isinstance(cose_array, list) or len(cose_array) != 4:
        raise CoseError(
            f"COSE_Sign1 must be a 4-element array; got "
            f"{type(cose_array).__name__} len="
            f"{len(cose_array) if isinstance(cose_array, list) else 'n/a'}"
        )
    protected_bytes, _unprotected, payload, signature = cose_array
    if not isinstance(protected_bytes, bytes):
        raise CoseError("COSE_Sign1[0] (protected) must be a bstr")
    if not isinstance(payload, bytes):
        raise CoseError("COSE_Sign1[2] (payload) must be a bstr")
    if not isinstance(signature, bytes):
        raise CoseError("COSE_Sign1[3] (signature) must be a bstr")
    sig_structure = cbor_encode([
        "Signature1",
        protected_bytes,
        b"",
        payload,
    ])
    try:
        public_key.verify(signature, sig_structure)
    except InvalidSignature as exc:
        raise CoseError("COSE_Sign1 signature did not verify") from exc
    protected_map = cbor_decode(protected_bytes) if protected_bytes else {}
    if not isinstance(protected_map, dict):
        raise CoseError("COSE_Sign1 protected header is not a map")
    return protected_map


def parse_cose_payload(cose_bytes: bytes) -> Dict[str, Any]:
    """Parse a COSE_Sign1 message and return ``(payload_dict, header_dict)``
    — no signature verification. Used by INSPECT to surface lifecycle
    event content alongside the opaque bytes."""
    decoded = cbor_decode(cose_bytes)
    if (
        not isinstance(decoded, tuple)
        or len(decoded) != 3
        or decoded[0] != "__tag__"
        or decoded[1] != CBOR_TAG_COSE_SIGN1
    ):
        raise CoseError("COSE_Sign1 message is not tagged 18")
    cose_array = decoded[2]
    if not isinstance(cose_array, list) or len(cose_array) != 4:
        raise CoseError("COSE_Sign1 must be a 4-element array")
    protected_bytes, _unprotected, payload_bytes, _sig = cose_array
    import json as _json
    try:
        payload = _json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise CoseError(
            f"COSE_Sign1 payload is not valid JSON: {exc}"
        ) from exc
    header = cbor_decode(protected_bytes) if protected_bytes else {}
    return {"header": header, "payload": payload}


def cose_audit_id(cose_bytes: bytes) -> str:
    """sha256 of the COSE bytes — analog of audit_id for JWS records.
    Stamped as the Audit-ID anchor for SCITT-mode chains."""
    return hashlib.sha256(cose_bytes).hexdigest()


__all__ = [
    "CBOR_TAG_COSE_SIGN1",
    "COSE_ALG_EDDSA",
    "COSE_HEADER_ALG",
    "COSE_HEADER_KID",
    "COSE_HEADER_TYP",
    "CoseError",
    "CoseSign1",
    "DEFAULT_TYP",
    "build_cose_sign1",
    "cbor_decode",
    "cbor_encode",
    "cose_audit_id",
    "parse_cose_payload",
    "verify_cose_sign1",
]
