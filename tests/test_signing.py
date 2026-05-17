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
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        server_id="agents.example.com",
        issued_at="2026-05-15T14:23:11Z",
        status=200,
    )
    assert record["kid"] == service.key_id
    assert record["alg"] == "Ed25519"
    assert record["payload"]["server_id"] == "agents.example.com"
    assert record["payload"]["status"] == 200

    # Decode the signature and verify it against the canonical payload.
    sig = record["signature"]
    # base64url, no padding — pad back for decoding.
    padded = sig + "=" * (-len(sig) % 4)
    signature_bytes = base64.urlsafe_b64decode(padded)
    canonical = json.dumps(
        record["payload"], sort_keys=True, separators=(",", ":"),
    )
    assert service.verify(canonical.encode("utf-8"), signature_bytes) is True


def test_attribution_record_extras(tmp_path: Path) -> None:
    service = SigningService.from_key_path(str(_write_key(tmp_path)))
    record = service.build_attribution_record(
        server_id="x",
        issued_at="2026-05-15T00:00:00Z",
        status=200,
        extra={"trace_id": "trace-abc"},
    )
    assert record["payload"]["trace_id"] == "trace-abc"
