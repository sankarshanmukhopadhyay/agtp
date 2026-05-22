"""
Tests for INSPECT read ACL (Tier 2.1).

Three modes: public (current behavior), agent_only, operator_only.
Tested in isolation against handle_inspect rather than spinning up a
TCP listener, since the ACL logic lives entirely in the handler.
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


def _stage(
    tmp_path: Path,
    *,
    read_acl: str = "public",
    read_acl_operator_keys=None,
):
    """Set up an agent + signing + record store, then produce one
    audit record. Returns (registry, agent_id, config, doc, audit_id)."""
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    audit_root = tmp_path / "audit"

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
            lifecycle_root=str(audit_root / "lifecycle"),
            read_acl=read_acl,
            read_acl_operator_keys=read_acl_operator_keys or [],
        ),
        signing=SigningConfig(enabled=True),
    )
    cfg.signing_service = _make_signing_service(tmp_path)
    reg = AgentRegistry(agent_dir)
    reg.config = cfg
    doc = reg.lookup(aid)

    # Produce one audit record.
    req = wire.AGTPRequest(method="DESCRIBE", headers={"Agent-ID": aid})
    resp = wire.AGTPResponse(
        status_code=200, status_text="OK", headers={}, body_bytes=b"{}",
    )
    _finalize_response(resp, req, cfg, principal_id="chris")
    audit_id = resp.headers["Audit-ID"]
    return reg, aid, cfg, doc, audit_id


def _inspect(aid: str, body: dict, *, verified_cert: VerifiedCert | None = None) -> wire.AGTPRequest:
    raw = json.dumps(body).encode("utf-8")
    req = wire.AGTPRequest(
        method="INSPECT",
        headers={"Agent-ID": aid, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )
    if verified_cert is not None:
        req.verified_cert = verified_cert
    return req


# ---------------------------------------------------------------------------
# public mode (default) — current behavior.
# ---------------------------------------------------------------------------


def test_public_allows_any_caller(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(tmp_path)
    # A different agent calling INSPECT against this record.
    other = "f" * 64
    resp = handle_inspect(
        _inspect(other, {"target": "audit", "audit_id": audit_id}),
        reg, _make_other_doc(other),
    )
    assert resp.status_code == 200


def _make_other_doc(agent_id: str):
    from core.identity import AgentDocument, RequiresDeclaration
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name="other",
        principal="p", principal_id="p", description="",
        status="active", skills=[], requires=RequiresDeclaration(),
        scopes_accepted=[], issued_at="x", issuer="self",
    )


# ---------------------------------------------------------------------------
# agent_only mode — only the record owner can read.
# ---------------------------------------------------------------------------


def test_agent_only_allows_owner(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(tmp_path, read_acl="agent_only")
    resp = handle_inspect(
        _inspect(aid, {"target": "audit", "audit_id": audit_id}),
        reg, doc,
    )
    assert resp.status_code == 200


def test_agent_only_refuses_cross_agent(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(tmp_path, read_acl="agent_only")
    other = "f" * 64
    resp = handle_inspect(
        _inspect(other, {"target": "audit", "audit_id": audit_id}),
        reg, _make_other_doc(other),
    )
    assert resp.status_code == 403
    assert b"inspect-acl-cross-agent" in resp.body_bytes


def test_agent_only_refuses_anonymous(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(tmp_path, read_acl="agent_only")
    # Empty agent_doc (server-level request).
    from core.identity import AgentDocument, RequiresDeclaration
    anon = AgentDocument(
        agtp_version="1.0", agent_id="", name="x", principal="p",
        principal_id="p", description="", status="active", skills=[],
        requires=RequiresDeclaration(), scopes_accepted=[],
        issued_at="x", issuer="x",
    )
    resp = handle_inspect(
        _inspect("", {"target": "audit", "audit_id": audit_id}),
        reg, anon,
    )
    assert resp.status_code == 401
    assert b"inspect-acl-anonymous" in resp.body_bytes


def test_agent_only_chain_head_check(tmp_path: Path) -> None:
    """The ACL applies to chain_head too — pre-checked before the
    store lookup."""
    reg, aid, cfg, doc, audit_id = _stage(tmp_path, read_acl="agent_only")
    other = "f" * 64
    resp = handle_inspect(
        _inspect(other, {"target": "chain_head", "agent_id": aid}),
        reg, _make_other_doc(other),
    )
    assert resp.status_code == 403


def test_agent_only_lifecycle_check(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(tmp_path, read_acl="agent_only")
    other = "f" * 64
    resp = handle_inspect(
        _inspect(other, {"target": "lifecycle", "agent_id": aid}),
        reg, _make_other_doc(other),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# operator_only mode — verified cert key must be on the ACL.
# ---------------------------------------------------------------------------


def _make_verified_cert(agent_id_hex: str) -> VerifiedCert:
    """Construct a VerifiedCert with the supplied agent_id (the
    sha256 of the cert's public key). Tests don't actually verify
    the cert, they just stash a VerifiedCert on the request."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc)
    return VerifiedCert(
        agent_id=agent_id_hex,
        fingerprint="x" * 64,
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=1),
        subject_common_name="test",
    )


def test_operator_only_refuses_without_cert(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(
        tmp_path, read_acl="operator_only",
        read_acl_operator_keys=["op1" + "0" * 61],
    )
    resp = handle_inspect(
        _inspect(aid, {"target": "audit", "audit_id": audit_id}),
        reg, doc,
    )
    assert resp.status_code == 401
    assert b"inspect-acl-no-cert" in resp.body_bytes


def test_operator_only_refuses_unlisted_cert(tmp_path: Path) -> None:
    reg, aid, cfg, doc, audit_id = _stage(
        tmp_path, read_acl="operator_only",
        read_acl_operator_keys=["op1" + "0" * 61],
    )
    cert = _make_verified_cert("intruder" + "0" * 56)  # not on ACL
    resp = handle_inspect(
        _inspect(
            aid, {"target": "audit", "audit_id": audit_id},
            verified_cert=cert,
        ),
        reg, doc,
    )
    assert resp.status_code == 403
    assert b"inspect-acl-operator-not-listed" in resp.body_bytes


def test_operator_only_allows_listed_cert(tmp_path: Path) -> None:
    op_key = "op1" + "0" * 61
    reg, aid, cfg, doc, audit_id = _stage(
        tmp_path, read_acl="operator_only",
        read_acl_operator_keys=[op_key],
    )
    cert = _make_verified_cert(op_key)
    resp = handle_inspect(
        _inspect(
            aid, {"target": "audit", "audit_id": audit_id},
            verified_cert=cert,
        ),
        reg, doc,
    )
    assert resp.status_code == 200


def test_operator_only_with_empty_acl_refuses_all(tmp_path: Path) -> None:
    """Fail-safe: operator_only + empty ACL list = nobody passes."""
    reg, aid, cfg, doc, audit_id = _stage(
        tmp_path, read_acl="operator_only",
        read_acl_operator_keys=[],
    )
    cert = _make_verified_cert("any" + "0" * 61)
    resp = handle_inspect(
        _inspect(
            aid, {"target": "audit", "audit_id": audit_id},
            verified_cert=cert,
        ),
        reg, doc,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Config validation.
# ---------------------------------------------------------------------------


def test_unknown_acl_mode_rejected_at_load(tmp_path: Path) -> None:
    from server.config import load as load_config
    p = tmp_path / "cfg.toml"
    p.write_text(
        '[server]\nserver_id = "t"\noperator = "o"\ncontact = "c"\n'
        '[audit]\nread_acl = "bananaphone"\n'
    )
    with pytest.raises(ValueError):
        load_config(p)
