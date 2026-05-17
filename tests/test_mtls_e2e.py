"""
End-to-end mTLS test.

Spins up a real TLS server with ``CERT_REQUIRED``, a client that
presents a valid Agent-Cert, and exercises the verification path
that's hooked into ``handle_connection``. This proves the wire-level
mTLS handshake works and that the per-connection verification
populates ``request.verified_cert`` as designed.

The test deliberately stays at the protocol layer: it doesn't go
through the full AGTP dispatch (which would require an agent
document, endpoint registry, etc). What it validates is that the
TLS layer + CertVerifier produces the right VerifiedCert.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from server.mtls import (
    CertVerifier,
    build_server_ssl_context,
    derive_agent_id_from_public_key,
)


def _gen_keypair_cert(
    *,
    common_name: str,
    issuer_name: x509.Name | None = None,
    issuer_key: Ed25519PrivateKey | None = None,
    is_ca: bool = False,
) -> tuple[Ed25519PrivateKey, x509.Certificate]:
    """Generate an Ed25519 keypair and a cert. Self-signed when no
    issuer is supplied. Adds SKI/AKI extensions so the cert validates
    under modern OpenSSL chain-building rules."""
    key = Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    actual_issuer = issuer_name if issuer_name is not None else subject
    actual_issuer_key = issuer_key if issuer_key is not None else key
    now = datetime.now(tz=timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    aki_source = (
        actual_issuer_key.public_key()
        if issuer_key is not None
        else key.public_key()
    )
    aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(aki_source)
    if is_ca:
        key_usage = x509.KeyUsage(
            digital_signature=False,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        )
    else:
        key_usage = x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=False,
            crl_sign=False,
            encipher_only=False,
            decipher_only=False,
        )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(actual_issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=is_ca, path_length=None),
            critical=True,
        )
        .add_extension(key_usage, critical=True)
        .add_extension(ski, critical=False)
        .add_extension(aki, critical=False)
    )
    cert = builder.sign(private_key=actual_issuer_key, algorithm=None)
    return key, cert


def _write_pem(
    tmp_path: Path,
    name: str,
    cert: x509.Certificate,
    key: Ed25519PrivateKey | None = None,
) -> tuple[Path, Path | None]:
    cert_path = tmp_path / f"{name}.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = None
    if key is not None:
        key_path = tmp_path / f"{name}.key"
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


@pytest.fixture
def mtls_certs(tmp_path: Path):
    """Build a CA + a server cert signed by it + a client cert signed by it."""
    # CA (self-signed).
    ca_key, ca_cert = _gen_keypair_cert(common_name="test-ca", is_ca=True)
    ca_cert_path, _ = _write_pem(tmp_path, "ca", ca_cert)

    # Server cert (signed by CA).
    server_key, server_cert = _gen_keypair_cert(
        common_name="server",
        issuer_name=ca_cert.subject,
        issuer_key=ca_key,
    )
    server_cert_path, server_key_path = _write_pem(
        tmp_path, "server", server_cert, server_key,
    )

    # Client cert (signed by CA).
    client_key, client_cert = _gen_keypair_cert(
        common_name="client-agent",
        issuer_name=ca_cert.subject,
        issuer_key=ca_key,
    )
    client_cert_path, client_key_path = _write_pem(
        tmp_path, "client", client_cert, client_key,
    )

    client_agent_id = derive_agent_id_from_public_key(client_key.public_key())

    return {
        "ca_path": ca_cert_path,
        "server_cert_path": server_cert_path,
        "server_key_path": server_key_path,
        "client_cert_path": client_cert_path,
        "client_key_path": client_key_path,
        "client_agent_id": client_agent_id,
        "client_cert": client_cert,
    }


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_mtls_handshake_and_verify(mtls_certs) -> None:
    """A client presenting a CA-signed cert completes the handshake;
    the server-side CertVerifier extracts the right Agent-ID."""
    port = _pick_free_port()

    server_ctx = build_server_ssl_context(
        certfile=str(mtls_certs["server_cert_path"]),
        keyfile=str(mtls_certs["server_key_path"]),
        ca_bundle_path=str(mtls_certs["ca_path"]),
        require_client_cert=True,
    )

    verifier = CertVerifier()
    verified_holder: dict = {}
    server_error: dict = {}

    def server_loop() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        sock.settimeout(5.0)
        try:
            raw, _ = sock.accept()
            try:
                conn = server_ctx.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                server_error["error"] = exc
                raw.close()
                return
            try:
                # Grab the cert immediately, before the client has a
                # chance to close. On Windows, even a brief race
                # between client close and getpeercert can cause an
                # OSError on the cert lookup.
                der = conn.getpeercert(binary_form=True)
                verified_holder["verified"] = verifier.verify_peer_cert(der)
                # Drain one byte so the client knows the server is
                # done processing before tearing down the connection.
                try:
                    conn.sendall(b"x")
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001
                server_error["error"] = exc
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        finally:
            sock.close()

    t = threading.Thread(target=server_loop, daemon=True)
    t.start()
    time.sleep(0.05)  # let the listener bind

    # Client side: trust the test server (we're validating the
    # server-side mTLS path, not the client's chain verification).
    # Loads the client cert+key so the server can verify them.
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    client_ctx.load_cert_chain(
        certfile=str(mtls_certs["client_cert_path"]),
        keyfile=str(mtls_certs["client_key_path"]),
    )

    client_error: Exception | None = None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="server") as conn:
                # Wait for the server's byte; gives the server time
                # to record the verified cert before we tear down.
                try:
                    conn.recv(1)
                except (ssl.SSLError, OSError):
                    pass
    except Exception as exc:
        client_error = exc

    t.join(timeout=5.0)

    assert "error" not in server_error, f"server-side TLS error: {server_error.get('error')}"
    assert client_error is None, f"client-side TLS error: {client_error!r}"
    verified = verified_holder.get("verified")
    assert verified is not None, "server did not record a verified cert"
    assert verified.agent_id == mtls_certs["client_agent_id"]
    assert verified.subject_common_name == "client-agent"
    assert len(verified.fingerprint) == 64


def test_mtls_rejects_client_without_cert(mtls_certs) -> None:
    """With CERT_REQUIRED, a client connecting without a cert fails
    the handshake."""
    port = _pick_free_port()

    server_ctx = build_server_ssl_context(
        certfile=str(mtls_certs["server_cert_path"]),
        keyfile=str(mtls_certs["server_key_path"]),
        ca_bundle_path=str(mtls_certs["ca_path"]),
        require_client_cert=True,
    )

    rejected: dict = {}

    def server_loop() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        sock.settimeout(5.0)
        try:
            raw, _ = sock.accept()
            try:
                server_ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError as exc:
                rejected["error"] = str(exc)
            finally:
                raw.close()
        finally:
            sock.close()

    t = threading.Thread(target=server_loop, daemon=True)
    t.start()
    time.sleep(0.05)

    # Client without a cert (server-side strictness is what we're testing).
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="server") as conn:
                pass
    except ssl.SSLError:
        pass  # client also sees the failure; that's expected
    except OSError:
        pass

    t.join(timeout=5.0)
    assert "error" in rejected, "server should have refused the handshake"


def test_mtls_optional_accepts_client_without_cert(mtls_certs) -> None:
    """With CERT_OPTIONAL, a client without a cert can still connect.
    The server sees no peer cert and skips Agent-ID derivation."""
    port = _pick_free_port()

    server_ctx = build_server_ssl_context(
        certfile=str(mtls_certs["server_cert_path"]),
        keyfile=str(mtls_certs["server_key_path"]),
        ca_bundle_path=str(mtls_certs["ca_path"]),
        require_client_cert=False,
    )

    result: dict = {}

    def server_loop() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        sock.settimeout(5.0)
        try:
            raw, _ = sock.accept()
            try:
                conn = server_ctx.wrap_socket(raw, server_side=True)
                result["peer_cert"] = conn.getpeercert(binary_form=True)
                conn.close()
            except ssl.SSLError as exc:
                result["error"] = str(exc)
                raw.close()
        finally:
            sock.close()

    t = threading.Thread(target=server_loop, daemon=True)
    t.start()
    time.sleep(0.05)

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw:
        with client_ctx.wrap_socket(raw, server_hostname="server"):
            pass

    t.join(timeout=5.0)
    assert "error" not in result
    # CERT_OPTIONAL with no client cert: getpeercert returns None or empty bytes.
    assert not result.get("peer_cert")
