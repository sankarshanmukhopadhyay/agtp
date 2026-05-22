"""
Tests for Phase 5: trust_tier / verification_path / trust_warning /
owner_id surfacing on AgentDocument + Genesis-derived fallback +
DISCOVER /agents listing.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.genesis import AgentGenesis, public_key_pem, utc_now_iso
from core.identity import (
    AgentDocument,
    DEFAULT_TRUST_TIER,
    DEFAULT_VERIFICATION_PATH,
    RequiresDeclaration,
    TIER_2_TRUST_WARNING,
    from_dict,
)


# ---------------------------------------------------------------------------
# AgentDocument dataclass behavior.
# ---------------------------------------------------------------------------


def _bare_doc(**overrides) -> AgentDocument:
    base = dict(
        agtp_version="1.0",
        agent_id="a" * 64,
        name="lauren",
        principal="chris",
        principal_id="chris",
        description="",
        status="active",
        skills=[],
        requires=RequiresDeclaration(),
        scopes_accepted=[],
        issued_at="2026-05-21T00:00:00Z",
        issuer="self",
    )
    base.update(overrides)
    return AgentDocument(**base)


def test_defaults_are_tier_2_self_signed_with_warning() -> None:
    """An AgentDocument with no trust fields lands at the conservative
    default: Tier 2, self-signed, with the spec-required warning
    populated automatically."""
    doc = _bare_doc()
    assert doc.trust_tier == DEFAULT_TRUST_TIER
    assert doc.verification_path == DEFAULT_VERIFICATION_PATH
    assert doc.trust_warning == TIER_2_TRUST_WARNING
    assert doc.owner_id == ""


def test_tier_1_drops_auto_warning() -> None:
    doc = _bare_doc(trust_tier=1, verification_path="dns-anchored")
    # The __post_init__ auto-populated the warning when tier defaulted
    # to 2; constructing directly with tier=1 means no warning.
    assert doc.trust_warning == ""


def test_explicit_trust_warning_wins_over_auto() -> None:
    doc = _bare_doc(trust_warning="custom-warning")
    assert doc.trust_warning == "custom-warning"


def test_invalid_tier_raises() -> None:
    with pytest.raises(ValueError):
        _bare_doc(trust_tier=9)


def test_invalid_verification_path_raises() -> None:
    with pytest.raises(ValueError):
        _bare_doc(verification_path="rumor-anchored")


# ---------------------------------------------------------------------------
# Serialization round-trip.
# ---------------------------------------------------------------------------


def test_round_trip_tier_2_omits_owner_id() -> None:
    """A Tier 2 doc without an owner serializes without the owner_id
    field (elided as empty)."""
    doc = _bare_doc()
    serialized = doc.to_dict()
    assert "trust_tier" in serialized
    assert "verification_path" in serialized
    assert "trust_warning" in serialized
    assert "owner_id" not in serialized
    reloaded = from_dict(serialized)
    assert reloaded.trust_tier == 2
    assert reloaded.trust_warning == TIER_2_TRUST_WARNING


def test_round_trip_tier_1_omits_trust_warning() -> None:
    doc = _bare_doc(
        trust_tier=1, verification_path="dns-anchored",
        owner_id="nomotic.inc",
    )
    serialized = doc.to_dict()
    assert "trust_warning" not in serialized
    assert serialized["owner_id"] == "nomotic.inc"
    reloaded = from_dict(serialized)
    assert reloaded.trust_tier == 1
    assert reloaded.trust_warning == ""


# ---------------------------------------------------------------------------
# Genesis-derived fallback in AgentRegistry.
# ---------------------------------------------------------------------------


def _stage_agent_with_genesis(
    tmp: Path,
    *,
    genesis_tier: int,
    genesis_path: str,
    agent_doc_overrides: dict | None = None,
) -> tuple[str, "AgentGenesis"]:
    """Drop a paired AgentDocument + Genesis into tmp/. Returns
    (agent_id, genesis)."""
    key = Ed25519PrivateKey.generate()
    pub_pem = public_key_pem(key.public_key())
    g = AgentGenesis(
        name="lauren", owner_id="nomotic.inc", principal_id="chris",
        agent_public_key=pub_pem, issued_at=utc_now_iso(),
        issuer="registrar.example", issuer_public_key=pub_pem,
        trust_tier=genesis_tier, verification_path=genesis_path,
    )
    g.sign(key)
    aid = g.canonical_agent_id()

    agent_doc = {
        "agtp_version": "v0.0.6",
        "agent_id": aid,
        "name": "lauren",
        "principal": "chris",
        "principal_id": "chris",
        "description": "",
        "status": "active",
        "skills": [],
        "requires": {"methods": ["DESCRIBE"], "scopes": [], "wildcards": False},
        "scopes_accepted": [],
        "issued_at": "now",
        "issuer": "self",
    }
    if agent_doc_overrides:
        agent_doc.update(agent_doc_overrides)
    (tmp / "lauren.agent.json").write_text(json.dumps(agent_doc))
    (tmp / "lauren.genesis.json").write_text(g.to_pretty_json())
    return aid, g


def test_registry_derives_tier_from_genesis_when_unset(tmp_path: Path) -> None:
    """AgentDocument that declares no tier inherits the Genesis's."""
    from server.main import AgentRegistry
    aid, _ = _stage_agent_with_genesis(
        tmp_path, genesis_tier=1, genesis_path="dns-anchored",
    )
    reg = AgentRegistry(tmp_path)
    doc = reg.lookup(aid)
    assert doc is not None
    assert doc.trust_tier == 1
    assert doc.verification_path == "dns-anchored"
    assert doc.owner_id == "nomotic.inc"
    # Tier 1 → no auto-warning.
    assert doc.trust_warning == ""


def test_explicit_agent_doc_tier_wins_over_genesis(tmp_path: Path) -> None:
    """When the AgentDocument declares trust_tier, the Genesis does
    not override it."""
    from server.main import AgentRegistry
    aid, _ = _stage_agent_with_genesis(
        tmp_path,
        genesis_tier=1, genesis_path="dns-anchored",
        agent_doc_overrides={
            "trust_tier": 2,
            "verification_path": "self-signed",
        },
    )
    reg = AgentRegistry(tmp_path)
    doc = reg.lookup(aid)
    assert doc is not None
    assert doc.trust_tier == 2
    assert doc.verification_path == "self-signed"
    # owner_id still flows from Genesis (not overridden in doc).
    assert doc.owner_id == "nomotic.inc"


def test_explicit_agent_doc_owner_id_wins(tmp_path: Path) -> None:
    from server.main import AgentRegistry
    aid, _ = _stage_agent_with_genesis(
        tmp_path,
        genesis_tier=1, genesis_path="dns-anchored",
        agent_doc_overrides={"owner_id": "override.example.com"},
    )
    reg = AgentRegistry(tmp_path)
    doc = reg.lookup(aid)
    assert doc.owner_id == "override.example.com"


def test_no_genesis_keeps_defaults(tmp_path: Path) -> None:
    """An AgentDocument without a paired Genesis keeps the
    schema defaults (Tier 2, self-signed)."""
    from server.main import AgentRegistry
    aid = "b" * 64
    agent_doc = {
        "agtp_version": "v0.0.6",
        "agent_id": aid,
        "name": "transport-only",
        "principal": "p", "principal_id": "p", "description": "",
        "status": "active", "skills": [],
        "requires": {"methods": ["DESCRIBE"], "scopes": [], "wildcards": False},
        "scopes_accepted": [], "issued_at": "now", "issuer": "self",
    }
    (tmp_path / "transport-only.agent.json").write_text(json.dumps(agent_doc))
    reg = AgentRegistry(tmp_path)
    doc = reg.lookup(aid)
    assert doc is not None
    assert doc.trust_tier == 2
    assert doc.verification_path == "self-signed"
    assert doc.owner_id == ""
    assert doc.trust_warning == TIER_2_TRUST_WARNING


# ---------------------------------------------------------------------------
# DISCOVER target=agents listing shape.
# ---------------------------------------------------------------------------


def test_discover_agents_listing_includes_trust(tmp_path: Path) -> None:
    """The DISCOVER /agents listing carries trust_tier,
    verification_path, and optional trust_warning / owner_id per entry."""
    from core import wire
    from server.main import AgentRegistry
    from server.methods import handle_discover

    aid, _ = _stage_agent_with_genesis(
        tmp_path, genesis_tier=1, genesis_path="dns-anchored",
    )
    reg = AgentRegistry(tmp_path)
    doc = reg.lookup(aid)

    # Build a minimal DISCOVER request.
    body = json.dumps({"target": "agents"}).encode("utf-8")
    req = wire.AGTPRequest(
        method="DISCOVER",
        headers={"Agent-ID": aid, "Content-Length": str(len(body))},
        body_bytes=body,
    )
    resp = handle_discover(req, reg, doc)
    assert resp.status_code == 200
    payload = json.loads(resp.body_bytes)
    assert len(payload["items"]) == 1
    entry = payload["items"][0]
    assert entry["trust_tier"] == 1
    assert entry["verification_path"] == "dns-anchored"
    assert entry["owner_id"] == "nomotic.inc"
    # Tier 1 → no trust_warning.
    assert "trust_warning" not in entry


def test_discover_agents_listing_carries_tier_2_warning(tmp_path: Path) -> None:
    """Tier 2 entries surface the trust_warning that the spec requires."""
    from core import wire
    from server.main import AgentRegistry
    from server.methods import handle_discover

    # AgentDocument with no Genesis → Tier 2 by default → warning auto-set.
    aid = "c" * 64
    agent_doc = {
        "agtp_version": "v0.0.6",
        "agent_id": aid,
        "name": "trust-tier-2",
        "principal": "p", "principal_id": "p", "description": "",
        "status": "active", "skills": [],
        "requires": {"methods": ["DESCRIBE"], "scopes": [], "wildcards": False},
        "scopes_accepted": [], "issued_at": "now", "issuer": "self",
    }
    (tmp_path / "t2.agent.json").write_text(json.dumps(agent_doc))
    reg = AgentRegistry(tmp_path)
    doc = reg.lookup(aid)

    body = json.dumps({"target": "agents"}).encode("utf-8")
    req = wire.AGTPRequest(
        method="DISCOVER",
        headers={"Agent-ID": aid, "Content-Length": str(len(body))},
        body_bytes=body,
    )
    resp = handle_discover(req, reg, doc)
    payload = json.loads(resp.body_bytes)
    entry = payload["items"][0]
    assert entry["trust_tier"] == 2
    assert entry["trust_warning"] == TIER_2_TRUST_WARNING
    # No owner_id (no Genesis).
    assert "owner_id" not in entry
