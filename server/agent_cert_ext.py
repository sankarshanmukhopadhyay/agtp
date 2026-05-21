"""
AGTP Agent Certificate X.509 v3 extensions.

Per ``draft-hood-agtp-agent-cert-00``, AGTP Agent Certificates carry
eight governance-specific extensions on top of a standard X.509 v3
cert. This module defines the OIDs, value formats, and encode/decode
helpers used by both the CLI generator and the daemon's verifier.

## The eight extensions

| Name | Criticality | Value format |
|---|---|---|
| ``subject-agent-id``         | CRITICAL     | 64 lowercase hex chars |
| ``principal-id``             | CRITICAL     | UTF-8 string (≤256 bytes) |
| ``authority-scope-commitment`` | CRITICAL   | sorted comma-separated UTF-8 list |
| ``governance-zone``          | non-critical | UTF-8 string (``zone:...``) |
| ``trust-tier``               | non-critical | DER-encoded INTEGER (1, 2, or 3) |
| ``archetype``                | non-critical | UTF-8 string |
| ``activation-certificate-id`` | non-critical | 64 lowercase hex chars (Agent Genesis hash) |
| ``agtp-ctl-sct``             | non-critical | RFC 6962 SCT structure (deferred) |

## On the OIDs

The spec's IANA Considerations section marks all eight OIDs as TBD.
Until the I-D is registered, this module uses **provisional OIDs**
under the ITU-T UUID arc (2.25.x per X.667), deterministically
derived from the extension names via UUIDv5 in the DNS namespace
keyed under ``agtp-cert-ext.<name>.nomotic.ai``. These OIDs are:

  * Globally unique by construction (UUID arc).
  * Reproducible: the same name always produces the same OID.
  * Easy to migrate: when IANA allocates final OIDs, only the
    constants in this file change.

Implementations that follow the eventual IANA-allocated OIDs MUST
update the constants here. The on-wire shape (the encoded values)
is unaffected by an OID change.

## On ``authority-scope-commitment``

The spec describes the commitment as "an Ed25519 signature over the
canonical lexicographically sorted Authority-Scope token set." A bare
signature does not let an SEP answer "is token X in the scope?" in
O(1) — the SEP needs the token list itself. Two readings are
possible:

  A. The extension carries the **token list** (and the cert chain's
     own signature covers it).
  B. The extension carries a **signature** over a token list that
     lives elsewhere (e.g., the agent's manifest).

This module implements interpretation (A): the extension value is the
sorted comma-separated UTF-8 token list. The cert's CA signature
binds the list. An SEP verifies the cert chain once at session
establishment, then per-request looks up tokens in the parsed set —
O(1) after parsing. Interpretation (B) requires a registry fetch per
session and adds a second signature; (A) is a strict subset of (B)'s
security model when the issuing CA is trusted.

This deviation is documented loudly so any later switch to (B) is
contained to the helpers below.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from cryptography import x509
from cryptography.x509 import ObjectIdentifier


# ---------------------------------------------------------------------------
# OID assignment (provisional — see module docstring).
# ---------------------------------------------------------------------------


def _provisional_oid(name: str) -> ObjectIdentifier:
    """Build a deterministic UUIDv5-derived OID under the ITU-T UUID
    arc (2.25.x per X.667). Tied to the AGTP extension name so the
    OID is reproducible across implementations until IANA allocates
    real values."""
    u = uuid.uuid5(uuid.NAMESPACE_DNS, f"agtp-cert-ext.{name}.nomotic.ai")
    return ObjectIdentifier(f"2.25.{u.int}")


OID_SUBJECT_AGENT_ID            = _provisional_oid("subject-agent-id")
OID_PRINCIPAL_ID                = _provisional_oid("principal-id")
OID_AUTHORITY_SCOPE_COMMITMENT  = _provisional_oid("authority-scope-commitment")
OID_GOVERNANCE_ZONE             = _provisional_oid("governance-zone")
OID_TRUST_TIER                  = _provisional_oid("trust-tier")
OID_ARCHETYPE                   = _provisional_oid("archetype")
OID_ACTIVATION_CERTIFICATE_ID   = _provisional_oid("activation-certificate-id")
OID_AGTP_CTL_SCT                = _provisional_oid("agtp-ctl-sct")


CRITICAL_OIDS = frozenset({
    OID_SUBJECT_AGENT_ID,
    OID_PRINCIPAL_ID,
    OID_AUTHORITY_SCOPE_COMMITMENT,
})


# ---------------------------------------------------------------------------
# Allowed-value enumerations.
# ---------------------------------------------------------------------------


VALID_TRUST_TIERS = (1, 2, 3)

VALID_ARCHETYPES = frozenset({
    "assistant",
    "analyst",
    "executor",
    "orchestrator",
    "monitor",
})


# ---------------------------------------------------------------------------
# Parsed view of the eight extensions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCertExtensions:
    """Strongly-typed view of the AGTP X.509 extensions on a cert.

    Every field is optional; a vanilla TLS cert without any of these
    extensions yields a default-constructed instance with all fields
    ``None`` / empty. This lets the verifier work uniformly for
    transport-only certs (Phase 2) and full Agent Certs (Phase 3+)
    without branching on cert shape.
    """

    subject_agent_id: Optional[str] = None
    """Canonical Agent-ID (64 lowercase hex). When present, supersedes
    the key-derived Agent-ID at the wire layer."""

    principal_id: Optional[str] = None
    """Principal-ID UTF-8 string (≤256 bytes)."""

    authority_scopes: Optional[Tuple[str, ...]] = None
    """Authorized Authority-Scope tokens, lexicographically sorted.
    The cert's CA signature covers this list; SEPs use it for O(1)
    per-request scope checks against the inbound ``Authority-Scope``
    header. ``None`` when the extension is absent (no transport-layer
    scope enforcement); ``()`` when explicitly empty (no scopes
    authorized)."""

    governance_zone: Optional[str] = None
    """Governance zone identifier (e.g. ``zone:finance``)."""

    trust_tier: Optional[int] = None
    """Agent's Trust Tier: 1 (verified), 2 (org-asserted), 3 (experimental)."""

    archetype: Optional[str] = None
    """Behavioral archetype: assistant / analyst / executor /
    orchestrator / monitor."""

    activation_certificate_id: Optional[str] = None
    """Cross-layer reference to the Agent Genesis hash (64 hex).
    Used by the Genesis-binding check that arrives in Phase 4."""

    agtp_ctl_sct: Optional[bytes] = None
    """Raw Signed Certificate Timestamp bytes (RFC 6962 §3.2). Parser
    surfaces the raw octet string; verification against an AGTP-CTL is
    deferred to a future phase."""


class CertExtensionError(ValueError):
    """Raised when an AGTP cert extension value is malformed."""


# ---------------------------------------------------------------------------
# Encoders — used by tools/generate_agent_cert.py to populate the
# extensions on a fresh CertificateBuilder.
# ---------------------------------------------------------------------------


def _hex_id_bytes(value: str, *, name: str) -> bytes:
    """Validate a 64-char lowercase hex agent-id-shaped value and return
    its UTF-8 bytes for octet-string encoding. The on-wire shape is the
    hex string itself, not the decoded 32 bytes; this keeps the value
    inspectable with standard cert tooling (``openssl x509 -text``)."""
    if not isinstance(value, str):
        raise CertExtensionError(
            f"{name} must be a string; got {type(value).__name__}"
        )
    text = value.strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise CertExtensionError(
            f"{name} must be 64 lowercase hex chars; got {value!r}"
        )
    return text.encode("ascii")


def add_subject_agent_id(builder: x509.CertificateBuilder, value: str) -> x509.CertificateBuilder:
    return builder.add_extension(
        x509.UnrecognizedExtension(
            OID_SUBJECT_AGENT_ID,
            _hex_id_bytes(value, name="subject-agent-id"),
        ),
        critical=True,
    )


def add_principal_id(builder: x509.CertificateBuilder, value: str) -> x509.CertificateBuilder:
    if not isinstance(value, str):
        raise CertExtensionError(
            f"principal-id must be a string; got {type(value).__name__}"
        )
    encoded = value.encode("utf-8")
    if len(encoded) > 256:
        raise CertExtensionError(
            f"principal-id must be ≤256 UTF-8 bytes; got {len(encoded)}"
        )
    return builder.add_extension(
        x509.UnrecognizedExtension(OID_PRINCIPAL_ID, encoded),
        critical=True,
    )


def _canonical_scopes(scopes: List[str]) -> Tuple[str, ...]:
    """Normalize a scope list: strip, drop empties, dedupe,
    lexicographically sort. The output is the canonical form that
    rides on the wire AND the form an SEP compares against."""
    seen = set()
    out: List[str] = []
    for s in scopes:
        if not isinstance(s, str):
            raise CertExtensionError(
                f"scope tokens must be strings; got {type(s).__name__}"
            )
        token = s.strip()
        if not token:
            continue
        if "," in token:
            raise CertExtensionError(
                f"scope token {token!r} contains ',' which is the wire "
                f"separator"
            )
        if token not in seen:
            seen.add(token)
            out.append(token)
    out.sort()
    return tuple(out)


def add_authority_scope_commitment(
    builder: x509.CertificateBuilder, scopes: List[str],
) -> x509.CertificateBuilder:
    canonical = _canonical_scopes(scopes)
    payload = ",".join(canonical).encode("utf-8")
    return builder.add_extension(
        x509.UnrecognizedExtension(OID_AUTHORITY_SCOPE_COMMITMENT, payload),
        critical=True,
    )


def add_governance_zone(builder: x509.CertificateBuilder, zone: str) -> x509.CertificateBuilder:
    if not isinstance(zone, str) or not zone:
        raise CertExtensionError("governance-zone must be a non-empty string")
    return builder.add_extension(
        x509.UnrecognizedExtension(OID_GOVERNANCE_ZONE, zone.encode("utf-8")),
        critical=False,
    )


def add_trust_tier(builder: x509.CertificateBuilder, tier: int) -> x509.CertificateBuilder:
    if tier not in VALID_TRUST_TIERS:
        raise CertExtensionError(
            f"trust-tier must be one of {VALID_TRUST_TIERS}; got {tier!r}"
        )
    # DER INTEGER for small positive integers is two bytes: tag (0x02)
    # + length (0x01) + value. We hand-encode rather than pulling in
    # asn1crypto so the only DER on the wire is this one byte.
    payload = bytes([0x02, 0x01, int(tier)])
    return builder.add_extension(
        x509.UnrecognizedExtension(OID_TRUST_TIER, payload),
        critical=False,
    )


def add_archetype(builder: x509.CertificateBuilder, archetype: str) -> x509.CertificateBuilder:
    if archetype not in VALID_ARCHETYPES:
        raise CertExtensionError(
            f"archetype must be one of {sorted(VALID_ARCHETYPES)}; "
            f"got {archetype!r}"
        )
    return builder.add_extension(
        x509.UnrecognizedExtension(OID_ARCHETYPE, archetype.encode("utf-8")),
        critical=False,
    )


def add_activation_certificate_id(
    builder: x509.CertificateBuilder, value: str,
) -> x509.CertificateBuilder:
    return builder.add_extension(
        x509.UnrecognizedExtension(
            OID_ACTIVATION_CERTIFICATE_ID,
            _hex_id_bytes(value, name="activation-certificate-id"),
        ),
        critical=False,
    )


def add_agtp_ctl_sct(builder: x509.CertificateBuilder, sct_bytes: bytes) -> x509.CertificateBuilder:
    if not isinstance(sct_bytes, (bytes, bytearray)) or not sct_bytes:
        raise CertExtensionError("agtp-ctl-sct must be non-empty bytes")
    return builder.add_extension(
        x509.UnrecognizedExtension(OID_AGTP_CTL_SCT, bytes(sct_bytes)),
        critical=False,
    )


# ---------------------------------------------------------------------------
# Decoder — used by server.mtls.CertVerifier on every verified cert.
# ---------------------------------------------------------------------------


def parse_extensions(cert: x509.Certificate) -> AgentCertExtensions:
    """Read every AGTP extension off ``cert`` and return a strongly-typed
    view. Missing extensions translate to ``None`` fields; malformed
    extension values raise :class:`CertExtensionError` (chain-validated
    certs with malformed AGTP extensions are pathological — better to
    refuse the connection than silently drop the field).
    """
    by_oid = {}
    for ext in cert.extensions:
        by_oid[ext.oid] = ext

    return AgentCertExtensions(
        subject_agent_id=_parse_hex_id(
            by_oid.get(OID_SUBJECT_AGENT_ID), name="subject-agent-id",
        ),
        principal_id=_parse_utf8(
            by_oid.get(OID_PRINCIPAL_ID), name="principal-id", max_bytes=256,
        ),
        authority_scopes=_parse_scope_commitment(
            by_oid.get(OID_AUTHORITY_SCOPE_COMMITMENT),
        ),
        governance_zone=_parse_utf8(
            by_oid.get(OID_GOVERNANCE_ZONE), name="governance-zone",
        ),
        trust_tier=_parse_trust_tier(by_oid.get(OID_TRUST_TIER)),
        archetype=_parse_archetype(by_oid.get(OID_ARCHETYPE)),
        activation_certificate_id=_parse_hex_id(
            by_oid.get(OID_ACTIVATION_CERTIFICATE_ID),
            name="activation-certificate-id",
        ),
        agtp_ctl_sct=_parse_octets(by_oid.get(OID_AGTP_CTL_SCT)),
    )


def _ext_value_bytes(ext: x509.Extension) -> bytes:
    """Pull the raw value bytes from an Extension. ``UnrecognizedExtension``
    exposes them via ``.value``; standard-typed extensions don't apply
    to AGTP custom OIDs so we should always see the unrecognized form."""
    val = ext.value
    if isinstance(val, x509.UnrecognizedExtension):
        return val.value
    # Future-proofing: if cryptography ever gains native support for
    # one of these OIDs, fall back to the public_bytes() encoding.
    try:
        return val.public_bytes()  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise CertExtensionError(
            f"unable to read raw bytes for extension {ext.oid.dotted_string}"
        ) from exc


def _parse_hex_id(ext: Optional[x509.Extension], *, name: str) -> Optional[str]:
    if ext is None:
        return None
    raw = _ext_value_bytes(ext)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CertExtensionError(f"{name} is not ASCII") from exc
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise CertExtensionError(
            f"{name} extension value must be 64 lowercase hex chars; "
            f"got {text!r}"
        )
    return text


def _parse_utf8(
    ext: Optional[x509.Extension],
    *,
    name: str,
    max_bytes: Optional[int] = None,
) -> Optional[str]:
    if ext is None:
        return None
    raw = _ext_value_bytes(ext)
    if max_bytes is not None and len(raw) > max_bytes:
        raise CertExtensionError(
            f"{name} extension value too long: {len(raw)} > {max_bytes}"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CertExtensionError(f"{name} is not valid UTF-8") from exc


def _parse_scope_commitment(
    ext: Optional[x509.Extension],
) -> Optional[Tuple[str, ...]]:
    if ext is None:
        return None
    raw = _ext_value_bytes(ext)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CertExtensionError(
            "authority-scope-commitment is not valid UTF-8"
        ) from exc
    if not text:
        return ()
    tokens = tuple(s for s in text.split(",") if s)
    # Defensive: refuse non-canonical (unsorted / duplicated) lists.
    # The on-wire shape is "canonical" — a non-canonical value is a
    # malformed extension and shouldn't be trusted.
    if list(tokens) != sorted(set(tokens)):
        raise CertExtensionError(
            "authority-scope-commitment is not in canonical form "
            "(must be lexicographically sorted, deduplicated)"
        )
    return tokens


def _parse_trust_tier(ext: Optional[x509.Extension]) -> Optional[int]:
    if ext is None:
        return None
    raw = _ext_value_bytes(ext)
    if len(raw) != 3 or raw[0] != 0x02 or raw[1] != 0x01:
        raise CertExtensionError(
            f"trust-tier extension is not a single-byte DER INTEGER: "
            f"got {raw.hex()}"
        )
    tier = raw[2]
    if tier not in VALID_TRUST_TIERS:
        raise CertExtensionError(
            f"trust-tier must be one of {VALID_TRUST_TIERS}; got {tier}"
        )
    return tier


def _parse_archetype(ext: Optional[x509.Extension]) -> Optional[str]:
    if ext is None:
        return None
    raw = _ext_value_bytes(ext)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CertExtensionError("archetype is not valid UTF-8") from exc
    if text not in VALID_ARCHETYPES:
        raise CertExtensionError(
            f"archetype must be one of {sorted(VALID_ARCHETYPES)}; "
            f"got {text!r}"
        )
    return text


def _parse_octets(ext: Optional[x509.Extension]) -> Optional[bytes]:
    if ext is None:
        return None
    return _ext_value_bytes(ext)


__all__ = [
    "AgentCertExtensions",
    "CertExtensionError",
    "CRITICAL_OIDS",
    "OID_ACTIVATION_CERTIFICATE_ID",
    "OID_AGTP_CTL_SCT",
    "OID_ARCHETYPE",
    "OID_AUTHORITY_SCOPE_COMMITMENT",
    "OID_GOVERNANCE_ZONE",
    "OID_PRINCIPAL_ID",
    "OID_SUBJECT_AGENT_ID",
    "OID_TRUST_TIER",
    "VALID_ARCHETYPES",
    "VALID_TRUST_TIERS",
    "add_activation_certificate_id",
    "add_agtp_ctl_sct",
    "add_archetype",
    "add_authority_scope_commitment",
    "add_governance_zone",
    "add_principal_id",
    "add_subject_agent_id",
    "add_trust_tier",
    "parse_extensions",
]
