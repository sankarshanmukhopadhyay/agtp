"""
Tests for Phase 8: identity-lifecycle methods + lifecycle stream.

Covers:
  * AuditLifecycleStore — append, read_all, malformed agent_id, etc.
  * ACTIVATE / DEACTIVATE / REVOKE handlers — state transitions,
    no-op when already in target state, signed lifecycle event
    emission.
  * INSPECT target=lifecycle — returns the full event stream.
  * [audit].mode = scitt — boot-time refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import wire
from core.genesis import AgentGenesis, public_key_pem, utc_now_iso
from server.audit_lifecycle import AuditLifecycleStore, default_lifecycle_root
from server.config import (
    AuditConfig, ServerConfig, ServerInfo, SigningConfig,
)
from server.main import AgentRegistry
from server.methods import (
    handle_activate, handle_deactivate, handle_deprecate,
    handle_inspect, handle_reinstate, handle_revoke,
)
from server.signing import SigningService


AGENT_HEX = "a" * 64


# ---------------------------------------------------------------------------
# AuditLifecycleStore.
# ---------------------------------------------------------------------------


def test_lifecycle_store_returns_empty_for_unknown(tmp_path: Path) -> None:
    store = AuditLifecycleStore(tmp_path)
    assert store.read_all(AGENT_HEX) == []


def test_lifecycle_store_appends_and_reads(tmp_path: Path) -> None:
    store = AuditLifecycleStore(tmp_path)
    store.append(AGENT_HEX, "jws-1")
    store.append(AGENT_HEX, "jws-2")
    assert store.read_all(AGENT_HEX) == ["jws-1", "jws-2"]


def test_lifecycle_store_isolates_per_agent(tmp_path: Path) -> None:
    store = AuditLifecycleStore(tmp_path)
    store.append("a" * 64, "a-jws")
    store.append("b" * 64, "b-jws")
    assert store.read_all("a" * 64) == ["a-jws"]
    assert store.read_all("b" * 64) == ["b-jws"]


def test_lifecycle_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = AuditLifecycleStore(tmp_path)
    with pytest.raises(ValueError):
        store.append("../escape", "x")
    assert store.read_all("../escape") == []


def test_lifecycle_store_root_created_lazily(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    store = AuditLifecycleStore(root)
    assert not root.exists()
    store.append(AGENT_HEX, "x")
    assert root.exists()


def test_default_lifecycle_root_structure() -> None:
    root = default_lifecycle_root()
    assert root.name == "lifecycle"
    assert root.parent.name == "audit"


# ---------------------------------------------------------------------------
# Lifecycle handler fixtures.
# ---------------------------------------------------------------------------


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


def _stage(tmp_path: Path):
    """Set up a registry with one agent + paired Genesis + signing +
    full audit config. Returns (registry, agent_id, config, doc)."""
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
            "methods": ["ACTIVATE", "DEACTIVATE", "REVOKE", "INSPECT"],
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
        ),
        signing=SigningConfig(enabled=True),
    )
    cfg.signing_service = _make_signing_service(tmp_path)
    reg = AgentRegistry(agent_dir)
    reg.config = cfg
    return reg, aid, cfg, reg.lookup(aid)


def _req(method: str, agent_id: str, body: dict | None = None) -> wire.AGTPRequest:
    raw = json.dumps(body or {}).encode("utf-8")
    return wire.AGTPRequest(
        method=method,
        headers={"Agent-ID": agent_id, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


# ---------------------------------------------------------------------------
# State transitions.
# ---------------------------------------------------------------------------


def test_deactivate_transitions_active_to_suspended(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    assert doc.status == "active"
    resp = handle_deactivate(_req("DEACTIVATE", aid), reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["previous_status"] == "active"
    assert body["status"] == "suspended"
    assert body["event_type"] == "deactivate"
    assert doc.status == "suspended"


def test_activate_transitions_suspended_to_active(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    doc.status = "suspended"
    resp = handle_activate(_req("ACTIVATE", aid), reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["previous_status"] == "suspended"
    assert body["status"] == "active"
    assert doc.status == "active"


def test_revoke_transitions_to_retired(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    resp = handle_revoke(_req("REVOKE", aid, {"reason": "compromise"}), reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["previous_status"] == "active"
    assert body["status"] == "retired"
    assert body["reason"] == "compromise"
    assert doc.status == "retired"


def test_noop_when_already_in_target_state(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    # Agent starts active; ACTIVATE again should be a noop.
    resp = handle_activate(_req("ACTIVATE", aid), reg, doc)
    body = json.loads(resp.body_bytes)
    assert body["noop"] is True
    assert body["status"] == "active"


def test_reinstate_restores_revoked_to_active(tmp_path: Path) -> None:
    """REINSTATE preserves the Agent-ID — per AGTP-LOG §2 Genesis is
    permanent. The agent restored is the same agent."""
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_revoke(_req("REVOKE", aid, {"reason": "compromised"}), reg, doc)
    assert doc.status == "retired"

    resp = handle_reinstate(_req("REINSTATE", aid, {"reason": "key-rotated"}), reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["previous_status"] == "retired"
    assert body["status"] == "active"
    assert body["event_type"] == "reinstate"
    assert doc.status == "active"
    # Agent-ID unchanged.
    assert doc.agent_id == aid


def test_deprecate_keeps_agent_operational(tmp_path: Path) -> None:
    """DEPRECATE is a soft retirement signal — status changes but the
    agent stays addressable (clients SHOULD migrate)."""
    reg, aid, cfg, doc = _stage(tmp_path)
    resp = handle_deprecate(_req("DEPRECATE", aid, {"reason": "v2-available"}), reg, doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["previous_status"] == "active"
    assert body["status"] == "deprecated"
    assert body["event_type"] == "deprecate"
    assert doc.status == "deprecated"


def test_reinstate_from_deprecated(tmp_path: Path) -> None:
    """Deprecated → active also works."""
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_deprecate(_req("DEPRECATE", aid), reg, doc)
    resp = handle_reinstate(_req("REINSTATE", aid), reg, doc)
    assert resp.status_code == 200
    assert json.loads(resp.body_bytes)["previous_status"] == "deprecated"
    assert doc.status == "active"


# ---------------------------------------------------------------------------
# Persistent status across daemon restart (Phase 8 T2.2).
# ---------------------------------------------------------------------------


def test_lifecycle_persists_status_to_disk(tmp_path: Path) -> None:
    """REVOKE writes the updated status to {name}.agent.json so a
    daemon restart picks up the new state."""
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_revoke(_req("REVOKE", aid, {"reason": "test"}), reg, doc)
    assert doc.status == "retired"

    # Verify on disk.
    agent_path = reg.agent_paths[aid]
    on_disk = json.loads(agent_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "retired"

    # Restart simulation: fresh AgentRegistry pointing at the same dir.
    reg2 = AgentRegistry(reg.agents_dir)
    doc2 = reg2.lookup(aid)
    assert doc2 is not None
    assert doc2.status == "retired"


def test_lifecycle_persist_survives_reinstate(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_deactivate(_req("DEACTIVATE", aid), reg, doc)
    handle_reinstate(_req("REINSTATE", aid), reg, doc)
    # On-disk reflects the final state.
    on_disk = json.loads(reg.agent_paths[aid].read_text(encoding="utf-8"))
    assert on_disk["status"] == "active"


def test_lifecycle_noop_does_not_rewrite_file(tmp_path: Path) -> None:
    """The noop branch returns 200 noop=true but should not touch
    the agent.json file (nothing to commit)."""
    reg, aid, cfg, doc = _stage(tmp_path)
    agent_path = reg.agent_paths[aid]
    mtime_before = agent_path.stat().st_mtime
    # Active → ACTIVATE is noop.
    import time as _t
    _t.sleep(0.02)
    handle_activate(_req("ACTIVATE", aid), reg, doc)
    mtime_after = agent_path.stat().st_mtime
    assert mtime_before == mtime_after


def test_persist_returns_false_for_in_memory_only_agent(tmp_path: Path) -> None:
    """An agent not loaded from disk (test fixture, in-memory
    construction) can't be persisted — persist() returns False
    rather than raising."""
    reg = AgentRegistry(tmp_path)
    from core.identity import AgentDocument, RequiresDeclaration
    aid = "a" * 64
    reg.agents[aid] = AgentDocument(
        agtp_version="1.0", agent_id=aid, name="x", principal="p",
        principal_id="p", description="", status="active", skills=[],
        requires=RequiresDeclaration(), scopes_accepted=[],
        issued_at="t", issuer="self",
    )
    # No entry in agent_paths.
    assert reg.persist(aid) is False


# ---------------------------------------------------------------------------
# Lifecycle event emission.
# ---------------------------------------------------------------------------


def test_each_transition_emits_signed_event(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_deactivate(_req("DEACTIVATE", aid, {"reason": "maintenance"}), reg, doc)
    handle_activate(_req("ACTIVATE", aid), reg, doc)
    handle_revoke(_req("REVOKE", aid, {"reason": "end-of-life"}), reg, doc)

    lifecycle_path = Path(cfg.audit.lifecycle_root) / f"{aid}.jsonl"
    assert lifecycle_path.exists()
    lines = lifecycle_path.read_text(encoding="ascii").strip().splitlines()
    assert len(lines) == 3
    # Each line is a 3-segment JWS Compact form.
    for line in lines:
        assert line.count(".") == 2


def test_noop_does_not_emit_event(tmp_path: Path) -> None:
    """No state change → no audit entry. Otherwise idempotent re-runs
    pollute the lifecycle stream."""
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_activate(_req("ACTIVATE", aid), reg, doc)  # noop
    lifecycle_path = Path(cfg.audit.lifecycle_root) / f"{aid}.jsonl"
    assert not lifecycle_path.exists() or lifecycle_path.read_text() == ""


def test_emitted_event_signature_verifies(tmp_path: Path) -> None:
    """The lifecycle JWS verifies against the daemon's signing key —
    same key material as Attribution-Record."""
    from server.signing import verify_attribution_record
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_revoke(_req("REVOKE", aid, {"reason": "x"}), reg, doc)

    lifecycle_path = Path(cfg.audit.lifecycle_root) / f"{aid}.jsonl"
    line = lifecycle_path.read_text(encoding="ascii").strip()
    payload = verify_attribution_record(line, cfg.signing_service.public_key)
    extra = payload["extra"]
    assert extra["event_type"] == "revoke"
    assert extra["previous_status"] == "active"
    assert extra["new_status"] == "retired"
    assert extra["reason"] == "x"


# ---------------------------------------------------------------------------
# INSPECT target=lifecycle.
# ---------------------------------------------------------------------------


def test_inspect_lifecycle_returns_full_stream(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    handle_deactivate(_req("DEACTIVATE", aid), reg, doc)
    handle_activate(_req("ACTIVATE", aid), reg, doc)
    handle_revoke(_req("REVOKE", aid), reg, doc)

    ireq = _req("INSPECT", aid, {"target": "lifecycle", "agent_id": aid})
    iresp = handle_inspect(ireq, reg, doc)
    assert iresp.status_code == 200
    body = json.loads(iresp.body_bytes)
    assert body["event_count"] == 3
    assert [ev["payload"]["extra"]["event_type"] for ev in body["events"]] == [
        "deactivate", "activate", "revoke",
    ]


def test_inspect_lifecycle_for_unknown_agent_returns_empty(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    ireq = _req("INSPECT", aid, {"target": "lifecycle", "agent_id": "f" * 64})
    iresp = handle_inspect(ireq, reg, doc)
    assert iresp.status_code == 200
    body = json.loads(iresp.body_bytes)
    assert body["event_count"] == 0
    assert body["events"] == []


def test_inspect_lifecycle_missing_agent_id_returns_400(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage(tmp_path)
    ireq = _req("INSPECT", aid, {"target": "lifecycle"})
    iresp = handle_inspect(ireq, reg, doc)
    assert iresp.status_code == 400
    assert b"missing-agent-id" in iresp.body_bytes


# ---------------------------------------------------------------------------
# [audit].mode validation.
# ---------------------------------------------------------------------------


def test_unknown_audit_mode_is_refused(tmp_path: Path) -> None:
    from server.main import run
    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        audit=AuditConfig(mode="bananaphone"),
    )
    with pytest.raises(RuntimeError):
        run(host="127.0.0.1", port=0, agents_dir=tmp_path / "agents", config=cfg)
