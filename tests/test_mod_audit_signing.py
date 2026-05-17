"""
Tests for mod_audit's Ed25519 signing path.

Exercises the AuditHook when given a SigningService: the on-disk
shape becomes {kid, alg, signature, payload} instead of flat
fields, and the signature verifies against the canonical payload.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import EndpointSpec, SemanticBlock
from mod_audit.hook import AuditHook
from mod_audit.log import AuditLog
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


def _spec() -> EndpointSpec:
    return EndpointSpec(
        name="BOOK",
        path="/room",
        description="Book.",
        semantic=SemanticBlock(
            intent="Book.",
            actor="agent",
            outcome="Done.",
            capability="transaction",
            confidence=0.9,
            impact="reversible",
            is_idempotent=False,
        ),
    )


def _ctx() -> EndpointContext:
    return EndpointContext(
        input={},
        agent_id="agent-1",
        request_id="req-1",
        method="BOOK",
        path="/room",
    )


# ---------------------------------------------------------------------------
# Signed envelope shape.
# ---------------------------------------------------------------------------


def test_signed_hook_emits_signed_envelope(tmp_path: Path) -> None:
    service = _make_signing_service(tmp_path)
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log, signing_service=service)

    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointResponse(body={"reservation_id": "res-1"}, status=200),
        server_state=None,
    )
    log.close()

    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    # Top-level shape: signed envelope.
    assert set(entry.keys()) == {"kid", "alg", "signature", "payload"}
    assert entry["kid"] == service.key_id
    assert entry["alg"] == "Ed25519"
    # The payload carries the receipt fields.
    payload = entry["payload"]
    assert payload["method"] == "BOOK"
    assert payload["path"] == "/room"
    assert payload["agent_id"] == "agent-1"
    assert payload["outcome"] == "ok"
    assert payload["status"] == 200


def test_signed_envelope_verifies(tmp_path: Path) -> None:
    """The signature in the envelope verifies against the canonical
    payload using the daemon's public key."""
    service = _make_signing_service(tmp_path)
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log, signing_service=service)
    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointResponse(body={"x": 1}, status=200),
        server_state=None,
    )
    log.close()

    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    canonical = json.dumps(entry["payload"], sort_keys=True, separators=(",", ":"))
    # Pad base64url and decode.
    sig_b64 = entry["signature"]
    padded = sig_b64 + "=" * (-len(sig_b64) % 4)
    signature = base64.urlsafe_b64decode(padded)
    assert service.verify(canonical.encode("utf-8"), signature) is True


def test_unsigned_hook_emits_flat_payload(tmp_path: Path) -> None:
    """Without a signing service, mod_audit emits the M9 v1 flat shape."""
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log)  # signing_service=None
    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointResponse(body={"x": 1}, status=200),
        server_state=None,
    )
    log.close()
    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    # No envelope keys; fields are at the top level.
    assert "kid" not in entry
    assert "signature" not in entry
    assert "payload" not in entry
    assert entry["method"] == "BOOK"
    assert entry["outcome"] == "ok"


def test_signing_distinguishes_payloads(tmp_path: Path) -> None:
    """Two different responses produce different signatures."""
    service = _make_signing_service(tmp_path)
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log, signing_service=service)
    hook.after_dispatch(
        _spec(), _ctx(),
        EndpointResponse(body={"x": 1}, status=200), server_state=None,
    )
    hook.after_dispatch(
        _spec(), _ctx(),
        EndpointResponse(body={"x": 2}, status=200), server_state=None,
    )
    log.close()
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["signature"] != b["signature"]


# ---------------------------------------------------------------------------
# install() wiring.
# ---------------------------------------------------------------------------


def test_install_wires_signing_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_signing_service(tmp_path)
    monkeypatch.setenv("AGTP_AUDIT_ENABLED", "1")
    monkeypatch.setenv("AGTP_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("AGTP_AUDIT_SIGN_RECEIPTS", "1")

    from server.hooks import HookRegistry
    from mod_audit import install

    class _State:
        hook_registry = HookRegistry()
        signing_service = service

    state = _State()
    install(state)
    hook = state.hook_registry.all()[0]
    assert hook.signing_service is service


def test_install_warns_when_signing_requested_but_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGTP_AUDIT_ENABLED", "1")
    monkeypatch.setenv("AGTP_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("AGTP_AUDIT_SIGN_RECEIPTS", "1")

    from server.hooks import HookRegistry
    from mod_audit import install

    class _State:
        hook_registry = HookRegistry()
        signing_service = None  # no signing service

    state = _State()
    install(state)
    captured = capsys.readouterr()
    assert "no signing service" in captured.err
    # Hook still registered, but without signing.
    hook = state.hook_registry.all()[0]
    assert hook.signing_service is None
