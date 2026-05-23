"""
Tests for the RCNS-4 follow-up — on_policy_change sweep.

Covers:
  * SynthesisRuntime.sweep_for_policy_change in both modes.
  * Recipe-version drift detection (edit / replace / remove).
  * Passthrough contracts unaffected (no recipe lineage to drift).
  * REVOKE target=stale-contracts operator surface — ACL,
    per-call mode override, body shape, audit emission.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import wire
from core.endpoint import EndpointSpec
from core.identity import AgentDocument, RequiresDeclaration
from server.config import (
    AuditConfig, RcnsConfig, ServerConfig, ServerInfo,
    ServerPolicy, SigningConfig, SynthesisConfig,
)
from server.signing import SigningService
from server.synthesis.plan import (
    CompositionStep, ParameterSource, SynthesisPlan,
)
from server.synthesis.recipes import (
    Recipe, RecipeBasedPolicy, RecipePattern,
)
from server.synthesis.runtime import SynthesisRuntime


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_signing_service(tmp_path: Path) -> SigningService:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "signing.key"
    p.write_bytes(pem)
    return SigningService.from_key_path(str(p))


def _recipe(
    name: str = "r1",
    version: str = "1",
    target: str = "QUERY",
) -> Recipe:
    return Recipe(
        name=name, description=f"{name} desc",
        pattern=RecipePattern(name_exact="RECONCILE"),
        steps=[CompositionStep(method_name=target, parameter_source={})],
        version=version,
    )


def _instantiate(
    runtime: SynthesisRuntime,
    *,
    recipe_name: Optional[str] = "r1",
    recipe_version: Optional[str] = "1",
    agent_id: str = "a" * 64,
) -> str:
    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="RECONCILE", path="/accounts", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
        recipe_name=recipe_name,
        recipe_version=recipe_version,
    )
    return runtime.instantiate(
        plan,
        originating_agent_id=agent_id,
        contract_hash="hash-x",
        negotiation_origin="rcns-confirmed",
    )


# ---------------------------------------------------------------------------
# SynthesisRuntime.sweep_for_policy_change — direct behavior.
# ---------------------------------------------------------------------------


def test_sweep_grandfather_is_read_only() -> None:
    """Default mode reports drift but evicts nothing — contracts
    keep running on their captured versions."""
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])  # current version: "2"
    ])
    sid = _instantiate(runtime, recipe_version="1")  # captured: "1"
    records = runtime.sweep_for_policy_change(mode="grandfather")
    assert len(records) == 1
    assert records[0]["synthesis_id"] == sid
    assert records[0]["action"] == "grandfathered"
    assert records[0]["captured_version"] == "1"
    assert records[0]["current_version"] == "2"
    # Contract is still active.
    assert runtime.get(sid) is not None


def test_sweep_invalidate_evicts_drifted_contracts() -> None:
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    sid = _instantiate(runtime, recipe_version="1")
    records = runtime.sweep_for_policy_change(mode="invalidate")
    assert len(records) == 1
    assert records[0]["action"] == "evicted"
    # Contract is gone from the runtime.
    assert runtime.get(sid) is None


def test_sweep_leaves_matching_versions_alone() -> None:
    """A contract whose captured version matches the current
    recipe is left alone in both modes."""
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    sid = _instantiate(runtime, recipe_version="2")  # matches current
    records = runtime.sweep_for_policy_change(mode="invalidate")
    assert records == []
    assert runtime.get(sid) is not None


def test_sweep_detects_removed_recipe() -> None:
    """A contract bound to a recipe that's been removed entirely
    (no entry under its name in the current policy) is treated as
    drifted with current_version = None."""
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(name="other-recipe")])
    ])
    sid = _instantiate(runtime, recipe_name="r1", recipe_version="1")
    records = runtime.sweep_for_policy_change(mode="grandfather")
    assert len(records) == 1
    assert records[0]["current_version"] is None
    assert records[0]["captured_version"] == "1"


def test_sweep_skips_passthrough_contracts() -> None:
    """Passthrough syntheses (no recipe lineage) have no recipe
    version to drift against and are skipped by the sweep."""
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    _instantiate(
        runtime, recipe_name=None, recipe_version=None,
    )
    records = runtime.sweep_for_policy_change(mode="invalidate")
    assert records == []


def test_sweep_rejects_unknown_mode() -> None:
    runtime = SynthesisRuntime()
    with pytest.raises(ValueError, match="sweep mode"):
        runtime.sweep_for_policy_change(mode="ignore")


def test_sweep_returns_record_lineage_fields() -> None:
    """Records carry enough metadata for the operator to know
    which contracts were affected and why."""
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    sid = _instantiate(runtime, recipe_version="1")
    records = runtime.sweep_for_policy_change(mode="grandfather")
    r = records[0]
    assert r["synthesis_id"] == sid
    assert r["originating_agent_id"] == "a" * 64
    assert r["contract_hash"] == "hash-x"
    assert r["negotiation_origin"] == "rcns-confirmed"
    assert r["method"] == "RECONCILE"
    assert r["path"] == "/accounts"
    assert r["recipe_name"] == "r1"


# ---------------------------------------------------------------------------
# REVOKE target=stale-contracts — operator surface.
# ---------------------------------------------------------------------------


def _config(
    *,
    tmp_path: Optional[Path] = None,
    attribution_enabled: bool = False,
    on_policy_change: str = "grandfather",
) -> ServerConfig:
    audit = AuditConfig()
    if attribution_enabled and tmp_path is not None:
        audit = AuditConfig(
            attribution_records_enabled=True,
            chain_head_root=str(tmp_path / "audit/chain_heads"),
            records_root=str(tmp_path / "audit/records"),
            lifecycle_root=str(tmp_path / "audit/lifecycle"),
            mode="jws",
        )
    return ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        policy=ServerPolicy(synthesis_enabled=True),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(enabled=True, on_policy_change=on_policy_change),
        audit=audit,
        signing=SigningConfig(),
    )


def _operator_doc() -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id="o" * 64, name="operator",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["REVOKE"], scopes=["inspect:all"],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


def _non_operator_doc() -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id="b" * 64, name="rando",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(methods=["REVOKE"], scopes=[]),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


def _state(
    config: ServerConfig,
    runtime: SynthesisRuntime,
    *,
    signing_service: Optional[SigningService] = None,
) -> Any:
    state = MagicMock()
    state.config = config
    state.synthesis_runtime = runtime
    state.signing_service = signing_service
    state.methods_policy = config.policy.methods
    state.endpoint_registry = None
    return state


def _req(body: dict, *, agent_id: str) -> wire.AGTPRequest:
    raw = json.dumps(body).encode("utf-8")
    return wire.AGTPRequest(
        method="REVOKE", path="/",
        headers={"Agent-ID": agent_id, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


def test_revoke_stale_contracts_refuses_without_inspect_all() -> None:
    from server.methods import handle_revoke
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    _instantiate(runtime, recipe_version="1")
    state = _state(_config(), runtime)
    resp = handle_revoke(
        _req({"target": "stale-contracts"}, agent_id="b" * 64),
        state, _non_operator_doc(),
    )
    assert resp.status_code == 403
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "operator-scope-required"


def test_revoke_stale_contracts_grandfather_is_dry_run() -> None:
    from server.methods import handle_revoke
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    sid = _instantiate(runtime, recipe_version="1")
    state = _state(_config(on_policy_change="grandfather"), runtime)
    resp = handle_revoke(
        _req({"target": "stale-contracts"}, agent_id="o" * 64),
        state, _operator_doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["mode"] == "grandfather"
    assert body["stale_count"] == 1
    assert body["records"][0]["synthesis_id"] == sid
    assert body["records"][0]["action"] == "grandfathered"
    assert body["audit_ids"] == []  # grandfather → no events
    # Contract still active.
    assert runtime.get(sid) is not None


def test_revoke_stale_contracts_invalidate_evicts_and_audits(
    tmp_path: Path,
) -> None:
    from server.audit_lifecycle import AuditLifecycleStore
    from server.methods import handle_revoke

    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    sid = _instantiate(runtime, recipe_version="1")
    sig = _make_signing_service(tmp_path)
    state = _state(
        _config(
            tmp_path=tmp_path, attribution_enabled=True,
            on_policy_change="invalidate",
        ),
        runtime, signing_service=sig,
    )
    resp = handle_revoke(
        _req({"target": "stale-contracts"}, agent_id="o" * 64),
        state, _operator_doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["mode"] == "invalidate"
    assert body["stale_count"] == 1
    assert body["records"][0]["action"] == "evicted"
    assert len(body["audit_ids"]) == 1
    assert runtime.get(sid) is None  # evicted

    # Audit event landed on the originating agent's lifecycle stream
    # with the policy-change-invalidation reason.
    store = AuditLifecycleStore(tmp_path / "audit/lifecycle")
    lines = store.read_all("a" * 64)
    assert len(lines) == 1
    from server.signing import parse_attribution_record
    _, payload, _ = parse_attribution_record(lines[0])
    assert payload["extra"]["event_type"] == "rcns_release"
    assert payload["extra"]["reason"] == "policy-change-invalidation"
    assert payload["extra"]["actor_agent_id"] == "o" * 64
    assert payload["extra"]["synthesis_id"] == sid


def test_revoke_stale_contracts_per_call_mode_override() -> None:
    """An operator can force grandfather (dry-run) on a server
    normally configured to invalidate — useful for previewing."""
    from server.methods import handle_revoke
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    sid = _instantiate(runtime, recipe_version="1")
    state = _state(_config(on_policy_change="invalidate"), runtime)
    resp = handle_revoke(
        _req({
            "target": "stale-contracts", "mode": "grandfather",
        }, agent_id="o" * 64),
        state, _operator_doc(),
    )
    body = json.loads(resp.body_bytes)
    assert body["mode"] == "grandfather"
    # Despite server config saying "invalidate", per-call override
    # gave us a dry-run; contract is still alive.
    assert runtime.get(sid) is not None


def test_revoke_stale_contracts_rejects_invalid_mode() -> None:
    from server.methods import handle_revoke
    state = _state(_config(), SynthesisRuntime())
    resp = handle_revoke(
        _req({
            "target": "stale-contracts", "mode": "destroy-everything",
        }, agent_id="o" * 64),
        state, _operator_doc(),
    )
    assert resp.status_code == 400
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "invalid-mode"


def test_revoke_stale_contracts_with_no_drift_returns_empty() -> None:
    from server.methods import handle_revoke
    runtime = SynthesisRuntime(policies=[
        RecipeBasedPolicy([_recipe(version="2")])
    ])
    _instantiate(runtime, recipe_version="2")  # matches current
    state = _state(_config(on_policy_change="invalidate"), runtime)
    resp = handle_revoke(
        _req({"target": "stale-contracts"}, agent_id="o" * 64),
        state, _operator_doc(),
    )
    body = json.loads(resp.body_bytes)
    assert body["stale_count"] == 0
    assert body["records"] == []
    assert body["audit_ids"] == []
