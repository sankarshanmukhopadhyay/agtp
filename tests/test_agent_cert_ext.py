"""
Tests for the AGTP Agent Certificate X.509 v3 extensions
(server.agent_cert_ext) and their integration with CertVerifier.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from server.agent_cert_ext import (
    AgentCertExtensions,
    CertExtensionError,
    CRITICAL_OIDS,
    OID_ACTIVATION_CERTIFICATE_ID,
    OID_ARCHETYPE,
    OID_AUTHORITY_SCOPE_COMMITMENT,
    OID_GOVERNANCE_ZONE,
    OID_PRINCIPAL_ID,
    OID_SUBJECT_AGENT_ID,
    OID_TRUST_TIER,
    add_activation_certificate_id,
    add_archetype,
    add_authority_scope_commitment,
    add_governance_zone,
    add_principal_id,
    add_subject_agent_id,
    add_trust_tier,
    parse_extensions,
)
from server.mtls import CertVerificationError, CertVerifier


AGENT_HEX = "a" * 64
GENESIS_HEX = "f" * 64


# ---------------------------------------------------------------------------
# Cert-building fixture: tiny self-signed cert builder for tests.
# ---------------------------------------------------------------------------


def _build_test_cert(
    *,
    subject_agent_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    authority_scopes: Optional[list] = None,
    governance_zone: Optional[str] = None,
    trust_tier: Optional[int] = None,
    archetype: Optional[str] = None,
    activation_certificate_id: Optional[str] = None,
) -> tuple[bytes, Ed25519PrivateKey]:
    """Build a self-signed cert with the requested AGTP extensions.

    Returns (DER bytes, private key) so tests can verify and re-derive
    Agent-IDs.
    """
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-agent")])
    now = datetime.now(tz=timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(pub).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=90))
    )
    if subject_agent_id is not None:
        builder = add_subject_agent_id(builder, subject_agent_id)
    if principal_id is not None:
        builder = add_principal_id(builder, principal_id)
    if authority_scopes is not None:
        builder = add_authority_scope_commitment(builder, authority_scopes)
    if governance_zone is not None:
        builder = add_governance_zone(builder, governance_zone)
    if trust_tier is not None:
        builder = add_trust_tier(builder, trust_tier)
    if archetype is not None:
        builder = add_archetype(builder, archetype)
    if activation_certificate_id is not None:
        builder = add_activation_certificate_id(builder, activation_certificate_id)
    cert = builder.sign(private_key=key, algorithm=None)
    return cert.public_bytes(encoding=serialization.Encoding.DER), key


# ---------------------------------------------------------------------------
# OID assignment.
# ---------------------------------------------------------------------------


def test_each_oid_is_unique() -> None:
    """The eight extension OIDs must all be distinct."""
    oids = {
        OID_SUBJECT_AGENT_ID,
        OID_PRINCIPAL_ID,
        OID_AUTHORITY_SCOPE_COMMITMENT,
        OID_GOVERNANCE_ZONE,
        OID_TRUST_TIER,
        OID_ARCHETYPE,
        OID_ACTIVATION_CERTIFICATE_ID,
    }
    assert len(oids) == 7  # SCT excluded — deferred


def test_critical_oids_match_spec() -> None:
    """AGTP-CERT §4.1.2: subject-agent-id, principal-id, and
    authority-scope-commitment are CRITICAL; the rest are not."""
    assert OID_SUBJECT_AGENT_ID in CRITICAL_OIDS
    assert OID_PRINCIPAL_ID in CRITICAL_OIDS
    assert OID_AUTHORITY_SCOPE_COMMITMENT in CRITICAL_OIDS
    assert OID_GOVERNANCE_ZONE not in CRITICAL_OIDS
    assert OID_TRUST_TIER not in CRITICAL_OIDS
    assert OID_ARCHETYPE not in CRITICAL_OIDS


# ---------------------------------------------------------------------------
# Round-trip: encode → parse.
# ---------------------------------------------------------------------------


def test_full_extension_roundtrip() -> None:
    """A cert built with all extensions parses back into a matching
    AgentCertExtensions instance."""
    der, _ = _build_test_cert(
        subject_agent_id=AGENT_HEX,
        principal_id="chris@nomotic.ai",
        authority_scopes=["bookings:write", "ledger:read", "audit:*"],
        governance_zone="zone:finance",
        trust_tier=1,
        archetype="analyst",
        activation_certificate_id=GENESIS_HEX,
    )
    cert = x509.load_der_x509_certificate(der)
    ext = parse_extensions(cert)
    assert ext.subject_agent_id == AGENT_HEX
    assert ext.principal_id == "chris@nomotic.ai"
    # Scopes round-trip in canonical (sorted) order.
    assert ext.authority_scopes == ("audit:*", "bookings:write", "ledger:read")
    assert ext.governance_zone == "zone:finance"
    assert ext.trust_tier == 1
    assert ext.archetype == "analyst"
    assert ext.activation_certificate_id == GENESIS_HEX


def test_no_extensions_yields_default_block() -> None:
    """A cert with no AGTP extensions parses to all-None fields."""
    der, _ = _build_test_cert()
    cert = x509.load_der_x509_certificate(der)
    ext = parse_extensions(cert)
    assert ext == AgentCertExtensions()
    assert ext.subject_agent_id is None
    assert ext.authority_scopes is None


# ---------------------------------------------------------------------------
# Scope canonicalization.
# ---------------------------------------------------------------------------


def test_scopes_deduplicated_and_sorted() -> None:
    """add_authority_scope_commitment normalizes its input to a
    deduplicated, lexicographically-sorted list before encoding."""
    der, _ = _build_test_cert(
        authority_scopes=["zeta", "alpha", "alpha", "beta"],
    )
    cert = x509.load_der_x509_certificate(der)
    ext = parse_extensions(cert)
    assert ext.authority_scopes == ("alpha", "beta", "zeta")


def test_empty_scope_list() -> None:
    """An explicit empty scope list round-trips as an empty tuple
    (not None) so verifiers can distinguish 'no scopes authorized'
    from 'no extension present'."""
    der, _ = _build_test_cert(authority_scopes=[])
    cert = x509.load_der_x509_certificate(der)
    ext = parse_extensions(cert)
    assert ext.authority_scopes == ()


def test_scope_with_comma_is_rejected() -> None:
    """Scope tokens containing ',' (the wire separator) are refused
    at encode time so the canonical form is unambiguous."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(pub).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    with pytest.raises(CertExtensionError):
        add_authority_scope_commitment(b, ["good", "bad,token"])


# ---------------------------------------------------------------------------
# Validation: trust-tier and archetype enumerations.
# ---------------------------------------------------------------------------


def test_trust_tier_validation() -> None:
    """Only 1, 2, 3 are valid trust tiers."""
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    with pytest.raises(CertExtensionError):
        add_trust_tier(b, 4)


def test_archetype_validation() -> None:
    """Only the five named archetypes are valid."""
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    with pytest.raises(CertExtensionError):
        add_archetype(b, "rogue-archetype")


def test_principal_id_length_cap() -> None:
    """principal-id is capped at 256 UTF-8 bytes per AGTP-CERT §4.1.2."""
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    with pytest.raises(CertExtensionError):
        add_principal_id(b, "x" * 257)


def test_hex_id_validation() -> None:
    """subject-agent-id and activation-certificate-id must be exactly
    64 lowercase hex chars."""
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    with pytest.raises(CertExtensionError):
        add_subject_agent_id(b, "ABCDEF" * 11)  # too short and uppercase
    with pytest.raises(CertExtensionError):
        add_activation_certificate_id(b, "g" * 64)  # non-hex


# ---------------------------------------------------------------------------
# CertVerifier integration.
# ---------------------------------------------------------------------------


def test_verifier_prefers_subject_agent_id() -> None:
    """When the cert carries a matching subject-agent-id extension,
    VerifiedCert.agent_id IS that value (not the key-derived hash —
    they should be equal, but the explicit field is authoritative)."""
    # Build cert; we need to know the key-derived id ahead of time.
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    from server.mtls import derive_agent_id_from_public_key
    aid = derive_agent_id_from_public_key(pub)

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(pub).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    b = add_subject_agent_id(b, aid)
    cert = b.sign(private_key=key, algorithm=None)
    der = cert.public_bytes(encoding=serialization.Encoding.DER)

    verified = CertVerifier().verify_peer_cert(der)
    assert verified.agent_id == aid
    assert verified.extensions.subject_agent_id == aid


def test_verifier_refuses_mismatched_subject_agent_id() -> None:
    """A subject-agent-id extension whose value disagrees with the
    key-derived Agent-ID MUST be refused with detail
    extension-mismatch — that's the substitution-attack defense."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(tz=timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(pub).serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    # Wrong subject-agent-id (a different valid hex string).
    b = add_subject_agent_id(b, "c" * 64)
    cert = b.sign(private_key=key, algorithm=None)
    der = cert.public_bytes(encoding=serialization.Encoding.DER)

    with pytest.raises(CertVerificationError) as exc_info:
        CertVerifier().verify_peer_cert(der)
    assert exc_info.value.detail == "extension-mismatch"


def test_verifier_with_no_extensions_uses_key_derived_id() -> None:
    """Transport-only cert (Phase-2 shape) yields a VerifiedCert
    whose agent_id is the key-derived hash and whose extensions
    block is all-None."""
    der, _ = _build_test_cert()  # no extension args
    from server.mtls import derive_agent_id_from_public_key
    cert = x509.load_der_x509_certificate(der)
    pub = cert.public_key()
    expected = derive_agent_id_from_public_key(pub)

    verified = CertVerifier().verify_peer_cert(der)
    assert verified.agent_id == expected
    assert verified.extensions.subject_agent_id is None
    assert verified.extensions.authority_scopes is None
