"""
Tests for [audit].lifecycle_auth (Tier 2.3).

Two modes:
  * open (default) — any caller can call ACTIVATE/DEACTIVATE/etc.
  * genesis_issuer — caller's verified cert key must match the
    agent's Genesis issuer_public_key fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import wire
from core.genesis import AgentGenesis, public_key_pem, utc_now_iso
from server.config import (
    AuditConfig, ServerConfig, ServerInfo, SigningConfig,
)
from server.main import AgentRegistry
from server.methods import handle_deactivate, handle_revoke
from server.mtls import VerifiedCert
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


def _stage(tmp_path: Path, *, lifecycle_auth: str = "open"):
    """Set up agent + paired Genesis + audit + lifecycle_auth mode.
    Returns (registry, agent_id, config, doc, issuer_key_fingerprint)."""
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    audit_root = tmp_path / "audit"

    # The Genesis is signed by a registrar key — that's the key our
    # genesis_issuer check expects on the caller's cert.
    registrar_key = Ed25519PrivateKey.generate()
    registrar_pub_pem = public_key_pem(registrar_key.public_key())
    agent_key = Ed25519PrivateKey.generate()
    agent_pub_pem = public_key_pem(agent_key.public_key())
    g = AgentGenesis(
        name="lauren", owner_id="nomotic.inc", principal_id="chris",
        agent_public_key=agent_pub_pem, issued_at=utc_now_iso(),
        issuer="registrar.example", issuer_public_key=registrar_pub_pem,
    )
    g.sign(registrar_key)
    aid = g.canonical_agent_id()

    (agent_dir / "lauren.agent.json").write_text(json.dumps({
        "agtp_version": "v0.0.6", "agent_id": aid, "name": "lauren",
        "principal": "c", "principal_id": "c", "description": "",
        "status": "active", "skills": [],
        "requires": {
            "methods": ["DEACTIVATE", "REVOKE"],
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
            lifecycle_root=str(audit_root / "lifecycle"),
            lifecycle_auth=lifecycle_auth,
        ),
        signing=SigningConfig(enabled=True),
    )
    cfg.signing_service = _make_signing_service(tmp_path)
    reg = AgentRegistry(agent_dir)
    reg.config = cfg
    doc = reg.lookup(aid)

    # Compute the issuer key's fingerprint the way our check does.
    raw = registrar_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    expected_fp = hashlib.sha256(raw).hexdigest()
    return reg, aid, cfg, doc, expected_fp


def _req(method: str, aid: str, *, verified_cert: VerifiedCert | None = None) -> wire.AGTPRequest:
    body = b"{}"
    req = wire.AGTPRequest(
        method=method,
        headers={"Agent-ID": aid, "Content-Length": str(len(body))},
        body_bytes=body,
    )
    if verified_cert is not None:
        req.verified_cert = verified_cert
    return req


def _cert(fingerprint_hex: str) -> VerifiedCert:
    now = datetime.now(tz=timezone.utc)
    return VerifiedCert(
        agent_id=fingerprint_hex,
        fingerprint="x" * 64,
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=1),
        subject_common_name="registrar",
    )


# ---------------------------------------------------------------------------
# open mode (default).
# ---------------------------------------------------------------------------


def test_open_mode_allows_any_caller(tmp_path: Path) -> None:
    reg, aid, cfg, doc, _ = _stage(tmp_path, lifecycle_auth="open")
    resp = handle_deactivate(_req("DEACTIVATE", aid), reg, doc)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# genesis_issuer mode.
# ---------------------------------------------------------------------------


def test_genesis_issuer_refuses_without_cert(tmp_path: Path) -> None:
    reg, aid, cfg, doc, _ = _stage(tmp_path, lifecycle_auth="genesis_issuer")
    resp = handle_deactivate(_req("DEACTIVATE", aid), reg, doc)
    assert resp.status_code == 401
    assert b"lifecycle-auth-no-cert" in resp.body_bytes


def test_genesis_issuer_refuses_wrong_cert(tmp_path: Path) -> None:
    reg, aid, cfg, doc, _ = _stage(tmp_path, lifecycle_auth="genesis_issuer")
    wrong_cert = _cert("ff" * 32)  # 64-hex, not the registrar's key
    resp = handle_deactivate(
        _req("DEACTIVATE", aid, verified_cert=wrong_cert),
        reg, doc,
    )
    assert resp.status_code == 403
    assert b"lifecycle-auth-wrong-issuer" in resp.body_bytes


def test_genesis_issuer_allows_matching_cert(tmp_path: Path) -> None:
    reg, aid, cfg, doc, issuer_fp = _stage(
        tmp_path, lifecycle_auth="genesis_issuer",
    )
    cert = _cert(issuer_fp)
    resp = handle_deactivate(
        _req("DEACTIVATE", aid, verified_cert=cert),
        reg, doc,
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["previous_status"] == "active"
    assert body["status"] == "suspended"


def test_genesis_issuer_refuses_agent_without_genesis(tmp_path: Path) -> None:
    """An agent with no loaded Genesis can't be lifecycle-managed
    under genesis_issuer mode."""
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    aid = "a" * 64
    (agent_dir / "x.agent.json").write_text(json.dumps({
        "agtp_version": "v0.0.6", "agent_id": aid, "name": "x",
        "principal": "p", "principal_id": "p", "description": "",
        "status": "active", "skills": [],
        "requires": {"methods": ["REVOKE"], "scopes": [], "wildcards": False},
        "scopes_accepted": [], "issued_at": "now", "issuer": "self",
    }))
    audit_root = tmp_path / "audit"
    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        audit=AuditConfig(
            attribution_records_enabled=True,
            chain_head_root=str(audit_root / "chain_heads"),
            records_root=str(audit_root / "records"),
            lifecycle_root=str(audit_root / "lifecycle"),
            lifecycle_auth="genesis_issuer",
        ),
    )
    reg = AgentRegistry(agent_dir)
    reg.config = cfg
    doc = reg.lookup(aid)
    cert = _cert("ff" * 32)
    resp = handle_revoke(
        _req("REVOKE", aid, verified_cert=cert),
        reg, doc,
    )
    assert resp.status_code == 403
    assert b"lifecycle-auth-no-genesis" in resp.body_bytes


# ---------------------------------------------------------------------------
# Config validation.
# ---------------------------------------------------------------------------


def test_unknown_lifecycle_auth_rejected_at_load(tmp_path: Path) -> None:
    from server.config import load as load_config
    p = tmp_path / "cfg.toml"
    p.write_text(
        '[server]\nserver_id = "t"\noperator = "o"\ncontact = "c"\n'
        '[audit]\nlifecycle_auth = "vibes-only"\n'
    )
    with pytest.raises(ValueError):
        load_config(p)
