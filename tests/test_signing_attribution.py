"""
End-to-end Attribution-Record + Audit-ID wire shape tests.

When the daemon is configured with [audit].attribution_records_enabled,
every response carries an ``Attribution-Record`` header containing a
JWS Compact Serialization (RFC 7515 §3.1) plus an ``Audit-ID``
header carrying ``sha256(jws)``. When ``[signing].enabled`` is true
the JWS is EdDSA-signed; otherwise it's an ``alg: none`` unsecured
JWS of the same shape.

We exercise the wire response by calling ``_finalize_response``
directly with a config carrying a real signing service — that's the
narrow function responsible for the headers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import wire
from server.audit_chain import AuditChainStore
from server.config import (
    AuditConfig, ServerConfig, ServerInfo, SigningConfig,
)
from server.main import _finalize_response
from server.signing import (
    SigningService,
    audit_id_for,
    parse_attribution_record,
    verify_attribution_record,
)


AGENT_HEX = "a" * 64


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
    tmp_path: Path,
    *,
    attribution_enabled: bool,
    signing_service: SigningService | None,
) -> ServerConfig:
    chain_root = tmp_path / "chain_heads"
    config = ServerConfig(
        server=ServerInfo(
            server_id="test.example.com",
            operator="test",
            contact="t@t",
        ),
        audit=AuditConfig(
            path="stderr",
            attribution_records_enabled=attribution_enabled,
            chain_head_root=str(chain_root),
        ),
        signing=SigningConfig(enabled=signing_service is not None),
    )
    if signing_service is not None:
        config.signing_service = signing_service
    return config


def _make_request(
    *,
    agent_id: str = AGENT_HEX,
    request_id: str = "req-1",
    task_id: str = "task-1",
    session_id: str = "sess-1",
) -> wire.AGTPRequest:
    return wire.AGTPRequest(
        method="DESCRIBE",
        headers={
            "Agent-ID": agent_id,
            "Request-ID": request_id,
            "Task-ID": task_id,
            "Session-ID": session_id,
        },
    )


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


def test_no_attribution_record_when_disabled(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    response = _make_response()
    _finalize_response(response, _make_request(), config)
    assert "Attribution-Record" not in response.headers
    assert "Audit-ID" not in response.headers


# ---------------------------------------------------------------------------
# Unsigned alg:none JWS when signing not loaded.
# ---------------------------------------------------------------------------


def test_unsigned_jws_when_signing_disabled(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=True, signing_service=None,
    )
    response = _make_response()
    _finalize_response(response, _make_request(), config)

    jws = response.headers["Attribution-Record"]
    parts = jws.split(".")
    assert len(parts) == 3
    assert parts[2] == ""  # alg:none has empty signature segment

    header, payload, _ = parse_attribution_record(jws)
    assert header["alg"] == "none"
    assert "kid" not in header
    assert payload["server_id"] == "test.example.com"

    # Audit-ID = sha256(JWS), even for unsigned records.
    assert response.headers["Audit-ID"] == audit_id_for(jws)


# ---------------------------------------------------------------------------
# Signed JWS when signing loaded.
# ---------------------------------------------------------------------------


def test_signed_jws_when_signing_enabled(tmp_path: Path) -> None:
    service = _make_signing_service(tmp_path)
    config = _make_config(
        tmp_path, attribution_enabled=True, signing_service=service,
    )
    response = _make_response()
    response.status_code = 263

    _finalize_response(
        response, _make_request(), config,
        principal_id="chris", owner_id="nomotic.inc",
    )

    jws = response.headers["Attribution-Record"]
    parts = jws.split(".")
    assert len(parts) == 3 and all(parts)

    payload = verify_attribution_record(jws, service.public_key)
    assert payload["server_id"] == "test.example.com"
    assert payload["status"] == 263
    assert payload["agent_id"] == AGENT_HEX
    assert payload["principal_id"] == "chris"
    assert payload["owner_id"] == "nomotic.inc"
    assert payload["request_id"] == "req-1"
    assert payload["task_id"] == "task-1"
    assert payload["session_id"] == "sess-1"
    assert "response_id" in payload
    assert "issued_at" in payload
    # First record for this agent: no predecessor.
    assert "previous_audit_id" not in payload

    # Audit-ID matches sha256(JWS).
    assert response.headers["Audit-ID"] == audit_id_for(jws)


# ---------------------------------------------------------------------------
# Per-agent chain.
# ---------------------------------------------------------------------------


def test_chain_links_consecutive_responses(tmp_path: Path) -> None:
    """The second response for the same agent points at the first
    response's Audit-ID via previous_audit_id."""
    service = _make_signing_service(tmp_path)
    config = _make_config(
        tmp_path, attribution_enabled=True, signing_service=service,
    )

    r1 = _make_response()
    _finalize_response(r1, _make_request(), config)
    audit_id_1 = r1.headers["Audit-ID"]

    r2 = _make_response()
    _finalize_response(r2, _make_request(), config)
    jws2 = r2.headers["Attribution-Record"]
    payload2 = verify_attribution_record(jws2, service.public_key)
    assert payload2["previous_audit_id"] == audit_id_1


def test_chains_are_per_agent(tmp_path: Path) -> None:
    """Different agents have independent chain heads."""
    service = _make_signing_service(tmp_path)
    config = _make_config(
        tmp_path, attribution_enabled=True, signing_service=service,
    )

    ra1 = _make_response()
    _finalize_response(ra1, _make_request(agent_id="a" * 64), config)
    rb1 = _make_response()
    _finalize_response(rb1, _make_request(agent_id="b" * 64), config)

    ra2 = _make_response()
    _finalize_response(ra2, _make_request(agent_id="a" * 64), config)
    payload_a2 = verify_attribution_record(
        ra2.headers["Attribution-Record"], service.public_key,
    )
    # Agent A's second record chains to Agent A's first, not Agent B's.
    assert payload_a2["previous_audit_id"] == ra1.headers["Audit-ID"]
    assert payload_a2["previous_audit_id"] != rb1.headers["Audit-ID"]


# ---------------------------------------------------------------------------
# Response-ID and Owner-ID stamping.
# ---------------------------------------------------------------------------


def test_response_id_is_synthesized_every_time(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    r1 = _make_response()
    r2 = _make_response()
    _finalize_response(r1, _make_request(), config)
    _finalize_response(r2, _make_request(), config)
    assert r1.headers["Response-ID"] != r2.headers["Response-ID"]
    assert r1.headers["Response-ID"].startswith("resp-")


def test_owner_id_stamped_when_supplied(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    response = _make_response()
    _finalize_response(response, _make_request(), config, owner_id="nomotic.inc")
    assert response.headers["Owner-ID"] == "nomotic.inc"


def test_owner_id_absent_when_not_supplied(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    response = _make_response()
    _finalize_response(response, _make_request(), config)
    assert "Owner-ID" not in response.headers


# ---------------------------------------------------------------------------
# Trust-Tier / Verification-Path / Trust-Warning headers (Tier-1 cleanup).
# ---------------------------------------------------------------------------


def test_trust_headers_stamped_when_known(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    response = _make_response()
    _finalize_response(
        response, _make_request(), config,
        trust_tier=1, verification_path="dns-anchored",
    )
    assert response.headers["Trust-Tier"] == "1"
    assert response.headers["Verification-Path"] == "dns-anchored"
    # No warning for Tier 1.
    assert "Trust-Warning" not in response.headers


def test_trust_warning_for_tier_2(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    response = _make_response()
    _finalize_response(
        response, _make_request(), config,
        trust_tier=2, verification_path="self-signed",
        trust_warning="verification-incomplete",
    )
    assert response.headers["Trust-Tier"] == "2"
    assert response.headers["Trust-Warning"] == "verification-incomplete"


def test_trust_headers_absent_when_not_supplied(tmp_path: Path) -> None:
    """Server-level responses (no agent_doc resolved) carry no trust
    headers — there's no agent to attribute trust to."""
    config = _make_config(
        tmp_path, attribution_enabled=False, signing_service=None,
    )
    response = _make_response()
    _finalize_response(response, _make_request(), config)
    assert "Trust-Tier" not in response.headers
    assert "Verification-Path" not in response.headers
    assert "Trust-Warning" not in response.headers


# ---------------------------------------------------------------------------
# attribution_extra plumbing.
# ---------------------------------------------------------------------------


def test_attribution_extra_rides_in_payload(tmp_path: Path) -> None:
    service = _make_signing_service(tmp_path)
    config = _make_config(
        tmp_path, attribution_enabled=True, signing_service=service,
    )
    response = _make_response()
    _finalize_response(
        response,
        _make_request(),
        config,
        attribution_extra={"intent_assertion_jti": "jti-1"},
    )
    payload = verify_attribution_record(
        response.headers["Attribution-Record"], service.public_key,
    )
    assert payload["extra"] == {"intent_assertion_jti": "jti-1"}


# ---------------------------------------------------------------------------
# Status differentiation still works (regression coverage).
# ---------------------------------------------------------------------------


def test_signed_record_changes_with_status(tmp_path: Path) -> None:
    """Different status codes produce different JWS signature segments."""
    service = _make_signing_service(tmp_path)
    config = _make_config(
        tmp_path, attribution_enabled=True, signing_service=service,
    )

    r200 = _make_response()
    _finalize_response(r200, _make_request(), config)
    r500 = _make_response()
    r500.status_code = 500
    _finalize_response(r500, _make_request(), config)

    sig_200 = r200.headers["Attribution-Record"].split(".")[2]
    sig_500 = r500.headers["Attribution-Record"].split(".")[2]
    assert sig_200 != sig_500
