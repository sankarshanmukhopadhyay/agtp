"""
Tests for the mTLS Agent-Cert verifier.

Covers cert parsing, Agent-ID derivation, validity-window checks,
public-key-type rejection, and the Agent-ID header cross-check.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from server.mtls import (
    CertVerificationError,
    CertVerifier,
    VerifiedCert,
    derive_agent_id_from_public_key,
)


# ---------------------------------------------------------------------------
# Fixture: build certs in-memory so the tests have no on-disk dependencies.
# ---------------------------------------------------------------------------


def _build_self_signed_cert(
    *,
    common_name: str = "test-agent",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> tuple[bytes, Ed25519PrivateKey]:
    """Returns (cert_der, private_key)."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(tz=timezone.utc)
    nb = valid_from or (now - timedelta(minutes=1))
    na = valid_until or (now + timedelta(days=365))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(nb)
        .not_valid_after(na)
        .sign(private_key=private_key, algorithm=None)
    )
    der = cert.public_bytes(encoding=serialization.Encoding.DER)
    return der, private_key


# ---------------------------------------------------------------------------
# derive_agent_id_from_public_key.
# ---------------------------------------------------------------------------


def test_agent_id_is_64_hex_chars() -> None:
    private_key = Ed25519PrivateKey.generate()
    agent_id = derive_agent_id_from_public_key(private_key.public_key())
    assert len(agent_id) == 64
    int(agent_id, 16)  # parses as hex


def test_agent_id_stable_for_same_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    a = derive_agent_id_from_public_key(private_key.public_key())
    b = derive_agent_id_from_public_key(private_key.public_key())
    assert a == b


def test_agent_id_differs_across_keys() -> None:
    a = derive_agent_id_from_public_key(Ed25519PrivateKey.generate().public_key())
    b = derive_agent_id_from_public_key(Ed25519PrivateKey.generate().public_key())
    assert a != b


def test_agent_id_equals_sha256_of_raw_public_key() -> None:
    """Spot-check: the derivation IS sha256(raw_public_key_bytes)."""
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    expected = hashlib.sha256(raw).hexdigest()
    assert derive_agent_id_from_public_key(private_key.public_key()) == expected


# ---------------------------------------------------------------------------
# CertVerifier.verify_peer_cert.
# ---------------------------------------------------------------------------


def test_verifies_valid_self_signed_cert() -> None:
    der, key = _build_self_signed_cert(common_name="lauren")
    v = CertVerifier()
    verified = v.verify_peer_cert(der)
    assert isinstance(verified, VerifiedCert)
    assert verified.agent_id == derive_agent_id_from_public_key(key.public_key())
    assert verified.subject_common_name == "lauren"
    # Fingerprint is SHA-256 of the DER bytes.
    assert verified.fingerprint == hashlib.sha256(der).hexdigest()


def test_rejects_empty_bytes() -> None:
    v = CertVerifier()
    with pytest.raises(CertVerificationError) as exc_info:
        v.verify_peer_cert(b"")
    assert exc_info.value.detail == "not-presented"


def test_rejects_non_x509_bytes() -> None:
    v = CertVerifier()
    with pytest.raises(CertVerificationError) as exc_info:
        v.verify_peer_cert(b"\x00\x01\x02 garbage")
    assert exc_info.value.detail == "not-x509"


def test_rejects_non_ed25519_cert() -> None:
    """Build a cert with an RSA key. The verifier must refuse it
    because Agent-IDs are bound to Ed25519 public keys."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pub = rsa_key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rsa-agent")])
    now = datetime.now(tz=timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(rsa_pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key=rsa_key, algorithm=__import__("cryptography").hazmat.primitives.hashes.SHA256())
    )
    der = cert.public_bytes(encoding=serialization.Encoding.DER)
    v = CertVerifier()
    with pytest.raises(CertVerificationError) as exc_info:
        v.verify_peer_cert(der)
    assert exc_info.value.detail == "not-ed25519"


def test_rejects_expired_cert() -> None:
    now = datetime.now(tz=timezone.utc)
    der, _ = _build_self_signed_cert(
        valid_from=now - timedelta(days=10),
        valid_until=now - timedelta(days=1),
    )
    v = CertVerifier()
    with pytest.raises(CertVerificationError) as exc_info:
        v.verify_peer_cert(der)
    assert exc_info.value.detail == "expired"


def test_rejects_not_yet_valid_cert() -> None:
    now = datetime.now(tz=timezone.utc)
    der, _ = _build_self_signed_cert(
        valid_from=now + timedelta(days=1),
        valid_until=now + timedelta(days=10),
    )
    v = CertVerifier()
    with pytest.raises(CertVerificationError) as exc_info:
        v.verify_peer_cert(der)
    assert exc_info.value.detail == "expired"


# ---------------------------------------------------------------------------
# Agent-ID header cross-check.
# ---------------------------------------------------------------------------


def test_cross_check_accepts_matching_header() -> None:
    der, key = _build_self_signed_cert()
    verified = CertVerifier().verify_peer_cert(der)
    # No-op when the values match — call returns None.
    CertVerifier.cross_check_agent_id_header(verified, verified.agent_id)


def test_cross_check_no_op_when_header_empty() -> None:
    der, _ = _build_self_signed_cert()
    verified = CertVerifier().verify_peer_cert(der)
    CertVerifier.cross_check_agent_id_header(verified, "")


def test_cross_check_rejects_mismatched_header() -> None:
    der, _ = _build_self_signed_cert()
    verified = CertVerifier().verify_peer_cert(der)
    with pytest.raises(CertVerificationError) as exc_info:
        CertVerifier.cross_check_agent_id_header(
            verified, "0" * 64,  # bogus agent_id
        )
    assert exc_info.value.detail == "agent-id-mismatch"


def test_cross_check_is_case_insensitive() -> None:
    der, _ = _build_self_signed_cert()
    verified = CertVerifier().verify_peer_cert(der)
    # Upper-case the header value; should still match.
    CertVerifier.cross_check_agent_id_header(verified, verified.agent_id.upper())


# ---------------------------------------------------------------------------
# build_server_ssl_context.
# ---------------------------------------------------------------------------


def test_build_ssl_context_no_mtls(tmp_path: Path) -> None:
    """Without a ca_bundle_path, the context is plain TLS server."""
    from server.mtls import build_server_ssl_context
    # Generate a server cert.
    der, key = _build_self_signed_cert(common_name="server")
    cert_pem = x509.load_der_x509_certificate(der).public_bytes(
        serialization.Encoding.PEM,
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    ctx = build_server_ssl_context(certfile=str(cert_path), keyfile=str(key_path))
    import ssl as _ssl
    assert ctx.verify_mode == _ssl.CERT_NONE


def test_build_ssl_context_mtls_optional(tmp_path: Path) -> None:
    from server.mtls import build_server_ssl_context
    der, key = _build_self_signed_cert(common_name="server")
    cert_pem = x509.load_der_x509_certificate(der).public_bytes(
        serialization.Encoding.PEM,
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    ca_path = tmp_path / "ca.crt"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    ca_path.write_bytes(cert_pem)  # self-trust for the test
    ctx = build_server_ssl_context(
        certfile=str(cert_path),
        keyfile=str(key_path),
        ca_bundle_path=str(ca_path),
        require_client_cert=False,
    )
    import ssl as _ssl
    assert ctx.verify_mode == _ssl.CERT_OPTIONAL


def test_build_ssl_context_mtls_required(tmp_path: Path) -> None:
    from server.mtls import build_server_ssl_context
    der, key = _build_self_signed_cert(common_name="server")
    cert_pem = x509.load_der_x509_certificate(der).public_bytes(
        serialization.Encoding.PEM,
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    ca_path = tmp_path / "ca.crt"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    ca_path.write_bytes(cert_pem)
    ctx = build_server_ssl_context(
        certfile=str(cert_path),
        keyfile=str(key_path),
        ca_bundle_path=str(ca_path),
        require_client_cert=True,
    )
    import ssl as _ssl
    assert ctx.verify_mode == _ssl.CERT_REQUIRED


# ---------------------------------------------------------------------------
# Gateway trust block — verify mTLS state flows through.
# ---------------------------------------------------------------------------


def test_gateway_trust_block_reflects_mtls() -> None:
    """When EndpointContext carries verified-cert info, the gateway
    request frame's `trust` block shows agent_cert_mtls."""
    from agtp.handlers import EndpointContext
    from server.gateway import GatewayServer

    ctx = EndpointContext(
        input={},
        agent_id="abc",
        method="QUERY",
        path="/",
        agent_verified=True,
        agent_cert_fingerprint="deadbeef" * 8,
    )
    server = GatewayServer(socket_path="127.0.0.1:0")
    trust = server._build_trust_block(ctx)
    assert trust["method"] == "agent_cert_mtls"
    assert trust["verified"] is True
    assert trust["agent_cert_fingerprint"] == "deadbeef" * 8


def test_gateway_trust_block_falls_back_to_header_when_not_verified() -> None:
    from agtp.handlers import EndpointContext
    from server.gateway import GatewayServer

    ctx = EndpointContext(
        input={},
        agent_id="abc",
        method="QUERY",
        path="/",
        # agent_verified=False, agent_cert_fingerprint=None (defaults)
    )
    server = GatewayServer(socket_path="127.0.0.1:0")
    trust = server._build_trust_block(ctx)
    assert trust["method"] == "agent_id_header"
    assert trust["agent_cert_fingerprint"] is None
