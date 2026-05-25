"""
Tests for the INSPECT built-in handler — Phase 6's read surface for
Attribution-Record JWSes and chain heads.

Exercises the handler in isolation by:
  1. Producing a real signed JWS via _finalize_response.
  2. Calling handle_inspect directly with the configured ServerState.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import wire
from core.genesis import AgentGenesis, public_key_pem, utc_now_iso
from server.config import (
    AuditConfig, ServerConfig, ServerInfo, SigningConfig,
)
from server.main import AgentRegistry, _finalize_response
from server.methods import handle_inspect
from server.signing import SigningService


def _make_signing_service(tmp_path: Path) -> SigningService:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "signing.key"
    path.write_bytes(pem)
    return SigningService.from_key_path(str(path))


def _stage_agent(tmp_path: Path) -> tuple[AgentRegistry, str, "ServerConfig"]:
    """Set up a registry with one agent + signing + records config.
    Returns (registry, agent_id, config)."""
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    audit_root = tmp_path / "audit"

    # Build an Agent Genesis + matching AgentDocument so the chain
    # store can key under a real Agent-ID.
    agent_key = Ed25519PrivateKey.generate()
    pub_pem = public_key_pem(agent_key.public_key())
    g = AgentGenesis(
        name="lauren", owner_id="nomotic.inc", principal_id="chris",
        agent_public_key=pub_pem, issued_at=utc_now_iso(),
        issuer="self", issuer_public_key=pub_pem,
    )
    g.sign(agent_key)
    aid = g.canonical_agent_id()

    (agent_dir / "lauren.agent.json").write_text(json.dumps({
        "agtp_version": "v0.0.6", "agent_id": aid, "name": "lauren",
        "principal": "c", "principal_id": "c", "description": "",
        "status": "active", "skills": [],
        "requires": {
            "methods": ["INSPECT", "DESCRIBE"],
            "scopes": [], "wildcards": False,
        },
        "scopes_accepted": [], "issued_at": "now", "issuer": "self",
    }))
    (agent_dir / "lauren.genesis.json").write_text(g.to_pretty_json())

    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        audit=AuditConfig(
            attribution_records_enabled=True,
            chain_head_root=str(audit_root / "chain_heads"),
            records_root=str(audit_root / "records"),
        ),
        signing=SigningConfig(enabled=True),
    )
    cfg.signing_service = _make_signing_service(tmp_path)
    reg = AgentRegistry(agent_dir)
    reg.config = cfg
    return reg, aid, cfg


def _issue_record(reg, aid, cfg) -> str:
    """Drive _finalize_response once. Returns the audit_id stamped."""
    req = wire.AGTPRequest(
        method="DESCRIBE",
        headers={"Agent-ID": aid, "Request-ID": "req-1"},
    )
    resp = wire.AGTPResponse(
        status_code=200, status_text="OK", headers={}, body_bytes=b"{}",
    )
    _finalize_response(
        resp, req, cfg,
        principal_id="chris", owner_id="nomotic.inc",
    )
    return resp.headers["Audit-ID"]


def _inspect_request(aid: str, body: dict) -> wire.AGTPRequest:
    raw = json.dumps(body).encode("utf-8")
    return wire.AGTPRequest(
        method="INSPECT",
        headers={"Agent-ID": aid, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------


def test_inspect_audit_returns_jws_payload(tmp_path: Path) -> None:
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    audit_id = _issue_record(reg, aid, cfg)

    req = _inspect_request(aid, {"target": "audit", "audit_id": audit_id})
    resp = handle_inspect(req, reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "audit"
    assert body["audit_id"] == audit_id
    assert body["jws"].count(".") == 2  # JWS Compact form
    assert body["header"]["alg"] == "EdDSA"
    assert body["payload"]["status"] == 200
    assert body["payload"]["agent_id"] == aid


def test_inspect_chain_head_returns_latest(tmp_path: Path) -> None:
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    audit_id_1 = _issue_record(reg, aid, cfg)
    audit_id_2 = _issue_record(reg, aid, cfg)
    # Second issuance updates the head.
    assert audit_id_2 != audit_id_1

    req = _inspect_request(aid, {"target": "chain_head", "agent_id": aid})
    resp = handle_inspect(req, reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "chain_head"
    assert body["audit_id"] == audit_id_2


def test_inspect_chain_walks_via_previous(tmp_path: Path) -> None:
    """Chain walkers fetch one record, follow previous_audit_id, and
    fetch again. The INSPECT handler must surface previous_audit_id
    so the walker can chain back."""
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    first = _issue_record(reg, aid, cfg)
    second = _issue_record(reg, aid, cfg)

    # Fetch newest record; payload references first as previous.
    req = _inspect_request(aid, {"target": "audit", "audit_id": second})
    resp = handle_inspect(req, reg, doc)
    body = json.loads(resp.body_bytes)
    assert body["payload"]["previous_audit_id"] == first

    # First record has no predecessor — represented by the 64-zero
    # chain-head sentinel per AGTP-IDENTIFIERS.
    req2 = _inspect_request(aid, {"target": "audit", "audit_id": first})
    resp2 = handle_inspect(req2, reg, doc)
    body2 = json.loads(resp2.body_bytes)
    assert body2["payload"]["previous_audit_id"] == "0" * 64


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_unknown_audit_id_returns_404(tmp_path: Path) -> None:
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    req = _inspect_request(aid, {"target": "audit", "audit_id": "a" * 64})
    resp = handle_inspect(req, reg, doc)
    assert resp.status_code == 404
    assert b"audit-record-not-found" in resp.body_bytes


def test_unknown_chain_head_returns_404(tmp_path: Path) -> None:
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    req = _inspect_request(aid, {"target": "chain_head", "agent_id": "b" * 64})
    resp = handle_inspect(req, reg, doc)
    assert resp.status_code == 404
    assert b"chain-head-not-found" in resp.body_bytes


def test_missing_audit_id_returns_400(tmp_path: Path) -> None:
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    req = _inspect_request(aid, {"target": "audit"})
    resp = handle_inspect(req, reg, doc)
    assert resp.status_code == 400


def test_unknown_target_returns_422(tmp_path: Path) -> None:
    reg, aid, cfg = _stage_agent(tmp_path)
    doc = reg.lookup(aid)
    req = _inspect_request(aid, {"target": "whatever"})
    resp = handle_inspect(req, reg, doc)
    assert resp.status_code == 422
    assert b"unknown-inspect-target" in resp.body_bytes
