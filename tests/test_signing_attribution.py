"""
End-to-end Attribution-Record signing test.

When the daemon is configured with [signing].enabled and an
[audit].attribution_records_enabled, every response should carry an
Ed25519-signed Attribution-Record header that verifies against the
daemon's public key.

We exercise the wire response by calling ``_finalize_response``
directly with a config carrying a real signing service — that's the
narrow function responsible for the header, and the test catches
any drift in either the helper or the wire shape.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core import wire
from server.config import (
    AuditConfig, ServerConfig, ServerInfo, SigningConfig,
)
from server.main import _finalize_response
from server.signing import SigningService


def _make_signing_service(tmp_path: Path) -> SigningService:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "signing.key"
    path.write_bytes(pem)
    return SigningService.from_key_path(str(path))


def _make_config(
    *,
    attribution_enabled: bool,
    signing_service: SigningService | None,
) -> ServerConfig:
    config = ServerConfig(
        server=ServerInfo(
            server_id="test.example.com",
            operator="test",
            contact="t@t",
        ),
        audit=AuditConfig(
            path="stderr",
            attribution_records_enabled=attribution_enabled,
        ),
        signing=SigningConfig(enabled=signing_service is not None),
    )
    if signing_service is not None:
        # Stash on the config the same way run() does.
        config.signing_service = signing_service
    return config


def _make_response() -> wire.AGTPResponse:
    return wire.AGTPResponse(
        status_code=200,
        status_text="OK",
        headers={},
        body_bytes=b"{}",
    )


# ---------------------------------------------------------------------------
# Attribution-Record absent when not opted in.
# ---------------------------------------------------------------------------


def test_no_attribution_record_when_disabled() -> None:
    config = _make_config(attribution_enabled=False, signing_service=None)
    response = _make_response()
    _finalize_response(response, None, config)
    assert "Attribution-Record" not in response.headers


# ---------------------------------------------------------------------------
# Unsigned placeholder when signing not loaded.
# ---------------------------------------------------------------------------


def test_unsigned_placeholder_when_signing_disabled() -> None:
    config = _make_config(attribution_enabled=True, signing_service=None)
    response = _make_response()
    _finalize_response(response, None, config)
    assert "Attribution-Record" in response.headers
    record = json.loads(response.headers["Attribution-Record"])
    # Pre-§5 placeholder shape: signature is literally "placeholder".
    assert record["signature"] == "placeholder"
    assert "kid" not in record  # placeholder has no kid


# ---------------------------------------------------------------------------
# Real Ed25519 signature when signing loaded.
# ---------------------------------------------------------------------------


def test_signed_record_when_signing_enabled(tmp_path: Path) -> None:
    service = _make_signing_service(tmp_path)
    config = _make_config(attribution_enabled=True, signing_service=service)
    response = _make_response()
    response.status_code = 263  # exercise a non-200 status code path

    _finalize_response(response, None, config)
    header = response.headers["Attribution-Record"]
    record = json.loads(header)

    # Shape from build_attribution_record.
    assert record["kid"] == service.key_id
    assert record["alg"] == "Ed25519"
    assert record["payload"]["server_id"] == "test.example.com"
    assert record["payload"]["status"] == 263
    assert "issued_at" in record["payload"]
    assert record["signature"] != "placeholder"

    # The signature verifies against the canonical payload.
    canonical = json.dumps(
        record["payload"], sort_keys=True, separators=(",", ":"),
    )
    sig_b64 = record["signature"]
    padded = sig_b64 + "=" * (-len(sig_b64) % 4)
    signature = base64.urlsafe_b64decode(padded)
    assert service.verify(canonical.encode("utf-8"), signature) is True


def test_signed_record_changes_with_status(tmp_path: Path) -> None:
    """Different status codes produce different signatures."""
    service = _make_signing_service(tmp_path)
    config = _make_config(attribution_enabled=True, signing_service=service)

    response_200 = _make_response()
    _finalize_response(response_200, None, config)
    record_200 = json.loads(response_200.headers["Attribution-Record"])

    response_500 = _make_response()
    response_500.status_code = 500
    _finalize_response(response_500, None, config)
    record_500 = json.loads(response_500.headers["Attribution-Record"])

    assert record_200["signature"] != record_500["signature"]
