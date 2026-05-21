"""
Tests for the Ed25519 signing service.

Covers key loading, signing/verification round-trips, key-id
derivation, the build_attribution_record helper, and the boot-time
load error paths.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.signing import (
    KeyLoadError,
    SigningError,
    SigningService,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _write_key(tmp_path: Path, name: str = "test.key") -> Path:
    """Generate a fresh Ed25519 key and write it as PEM to tmp_path."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / name
    path.write_bytes(pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# Key loading.
# ---------------------------------------------------------------------------


def test_loads_valid_pem_key(tmp_path: Path) -> None:
    key_path = _write_key(tmp_path)
    service = SigningService.from_key_path(str(key_path))
    assert service.key_id.startswith("ed25519-")
    assert len(service.key_id) == len("ed25519-") + 16


def test_loads_with_explicit_key_id(tmp_path: Path) -> None:
    key_path = _write_key(tmp_path)
    service = SigningService.from_key_path(str(key_path), key_id="my-key-v1")
    assert service.key_id == "my-key-v1"


def test_rejects_missing_key(tmp_path: Path) -> None:
    with pytest.raises(KeyLoadError, match="not found"):
        SigningService.from_key_path(str(tmp_path / "nope.key"))


def test_rejects_empty_path() -> None:
    with pytest.raises(KeyLoadError, match="empty"):
        SigningService.from_key_path("")


def test_rejects_malformed_pem(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_bytes(b"not actually a PEM file")
    with pytest.raises(KeyLoadError, match="parsed as PEM"):
        SigningService.from_key_path(str(bad))


def test_rejects_non_ed25519_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "rsa.key"
    path.write_bytes(pem)
    with pytest.raises(KeyLoadError, match="not Ed25519"):
        SigningService.from_key_path(str(path))


def test_warns_on_world_readable_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX file modes don't apply on Windows")
    key_path = _write_key(tmp_path, "loose.key")
    os.chmod(key_path, 0o644)
    SigningService.from_key_path(str(key_path))
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "chmod 0600" in captured.err


# ---------------------------------------------------------------------------
# Signing and verification.
# ---------------------------------------------------------------------------


def test_sign_and_verify_round_trip(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    data = b"the payload to sign"
    signature = service.sign(data)
    assert len(signature) == 64  # Ed25519 signatures are always 64 bytes
    assert service.verify(data, signature) is True


def test_verify_rejects_tampered_data(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    signature = service.sign(b"original")
    assert service.verify(b"tampered", signature) is False


def test_verify_rejects_wrong_signature(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    sig1 = service.sign(b"x")
    sig2 = service.sign(b"y")
    assert sig1 != sig2
    assert service.verify(b"x", sig2) is False


def test_sign_rejects_non_bytes(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    with pytest.raises(SigningError, match="bytes"):
        service.sign("not bytes")  # type: ignore[arg-type]


def test_sign_canonical_is_stable(tmp_path: Path) -> None:
    """The canonical encoding produces the same signature regardless
    of dict-insertion order — that's the whole point."""
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    sig1 = service.sign_canonical({"a": 1, "b": 2, "c": 3})
    sig2 = service.sign_canonical({"c": 3, "a": 1, "b": 2})
    assert sig1 == sig2


def test_sign_canonical_distinguishes_payloads(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    sig1 = service.sign_canonical({"a": 1})
    sig2 = service.sign_canonical({"a": 2})
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# Public key export.
# ---------------------------------------------------------------------------


def test_public_key_pem_export(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    pem = service.public_key_pem()
    assert pem.startswith(b"-----BEGIN PUBLIC KEY-----")
    assert pem.rstrip().endswith(b"-----END PUBLIC KEY-----")


def test_public_key_raw_is_32_bytes(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    raw = service.public_key_raw()
    assert len(raw) == 32


def test_key_id_derived_from_public_key(tmp_path: Path) -> None:
    """Two services loaded from the same key file produce the same kid."""
    key_path = _write_key(tmp_path)
    a = SigningService.from_key_path(str(key_path))
    b = SigningService.from_key_path(str(key_path))
    assert a.key_id == b.key_id


def test_key_id_differs_across_keys(tmp_path: Path) -> None:
    a = SigningService.from_key_path(str(_write_key(tmp_path, "a.key")))
    b = SigningService.from_key_path(str(_write_key(tmp_path, "b.key")))
    assert a.key_id != b.key_id


# ---------------------------------------------------------------------------
# build_attribution_record.
# ---------------------------------------------------------------------------


def test_attribution_record_round_trip(tmp_path: Path) -> None:
    """A signed Attribution-Record is a valid JWS Compact form that
    verifies against the service's public key."""
    from server.signing import (
        parse_attribution_record,
        verify_attribution_record,
        audit_id_for,
    )

    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        agent_id="a" * 64,
        server_id="agents.example.com",
        issued_at="2026-05-15T14:23:11Z",
        status=200,
        request_id="req-1",
    )

    # Three dot-separated base64url segments.
    parts = record.jws.split(".")
    assert len(parts) == 3
    assert all(parts)  # signed JWS: no segment is empty

    # Header carries EdDSA and the daemon's kid.
    header, payload, signature = parse_attribution_record(record.jws)
    assert header["alg"] == "EdDSA"
    assert header["typ"] == "JWT"
    assert header["kid"] == service.key_id

    # Payload carries the daemon-known fields; empty values are omitted.
    assert payload["server_id"] == "agents.example.com"
    assert payload["status"] == 200
    assert payload["agent_id"] == "a" * 64
    assert payload["request_id"] == "req-1"
    assert "owner_id" not in payload  # not set

    # Verifier accepts a valid signature.
    verified = verify_attribution_record(record.jws, service.public_key)
    assert verified == payload

    # audit_id is sha256 of the compact JWS.
    assert record.audit_id == audit_id_for(record.jws)


def test_attribution_record_extras(tmp_path: Path) -> None:
    """Handler-supplied `attribution_extra` rides under a top-level
    `extra` key in the JWS payload."""
    from server.signing import parse_attribution_record

    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        server_id="x",
        issued_at="2026-05-15T00:00:00Z",
        status=200,
        extra={"intent_assertion_jti": "jti-abc"},
    )
    _, payload, _ = parse_attribution_record(record.jws)
    assert payload["extra"] == {"intent_assertion_jti": "jti-abc"}


def test_attribution_record_chain_field(tmp_path: Path) -> None:
    """previous_audit_id rides in the payload when supplied."""
    from server.signing import parse_attribution_record

    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        server_id="x",
        issued_at="2026-05-15T00:00:00Z",
        status=200,
        previous_audit_id="aud-prev",
    )
    _, payload, _ = parse_attribution_record(record.jws)
    assert payload["previous_audit_id"] == "aud-prev"


def test_attribution_record_omits_empty_fields(tmp_path: Path) -> None:
    """Empty-string identifier fields drop from the payload so
    verifiers see only what the daemon actually observed."""
    from server.signing import parse_attribution_record

    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        server_id="x",
        issued_at="2026-05-15T00:00:00Z",
        status=200,
    )
    _, payload, _ = parse_attribution_record(record.jws)
    # Mandatory always-present fields.
    assert set(payload.keys()) == {"server_id", "issued_at", "status"}


def test_unsigned_attribution_record(tmp_path: Path) -> None:
    """alg:none records carry an empty signature segment but the
    payload is otherwise identical and parseable with the same
    helper."""
    from server.signing import parse_attribution_record

    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_unsigned_attribution_record(
        server_id="x",
        issued_at="2026-05-15T00:00:00Z",
        status=200,
        agent_id="b" * 64,
    )
    parts = record.jws.split(".")
    assert len(parts) == 3
    assert parts[2] == ""  # empty signature segment per RFC 7515 §6
    header, payload, signature = parse_attribution_record(record.jws)
    assert header["alg"] == "none"
    assert "kid" not in header
    assert signature == b""
    assert payload["agent_id"] == "b" * 64


def test_verify_rejects_tampered_payload(tmp_path: Path) -> None:
    """A modified payload segment must fail verification."""
    from server.signing import (
        AttributionRecordError,
        verify_attribution_record,
    )

    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        server_id="x", issued_at="2026-05-15T00:00:00Z", status=200,
    )
    # Re-encode payload with a status change, keep the original signature.
    parts = record.jws.split(".")
    parts[1] = base64.urlsafe_b64encode(
        b'{"server_id":"x","issued_at":"2026-05-15T00:00:00Z","status":500}'
    ).rstrip(b"=").decode("ascii")
    tampered = ".".join(parts)
    with pytest.raises(AttributionRecordError):
        verify_attribution_record(tampered, service.public_key)
