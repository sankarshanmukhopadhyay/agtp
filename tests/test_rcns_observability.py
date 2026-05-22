"""
Tests for RCNS-4 — Observability + lifecycle.

Covers:
  * DISCOVER /patterns (Tier A) — RCNS posture + recipe inventory
    with policy/recipe versions.
  * DISCOVER /contracts (Tier A) — active syntheses scoped to
    caller; inspect:all reaches across.
  * INSPECT target=contract — detail view with plan / lineage /
    expiration; ACL refuses cross-agent without inspect:all.
  * INSPECT target=rcns-attempt — diagnostic ring buffer lookup;
    same ACL.
  * REVOKE target=contract — operator surface; expires the
    runtime entry and emits rcns_revoke audit event.
  * SUSPEND with synthesis_id emits rcns_release audit event.
  * RCNS-4 reserved roots are Tier A in the inventory and surface
    on DISCOVER /.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import wire
from core.endpoint import EndpointSpec
from core.endpoint_tiers import TIER_A_RESERVED_ENDPOINTS, classify_tier
from core.identity import AgentDocument, RequiresDeclaration
from core.path_grammar import DISCOVER_RESERVED_ROOTS
from server.config import (
    AgentsConfig, AuditConfig, GatewayConfig, MtlsConfig,
    RcnsConfig, ServerConfig, ServerInfo, ServerPolicy,
    SigningConfig, SynthesisConfig,
)
from server.rcns_gate import (
    contract_hash, lookup_attempt, reset_state_for_tests, try_rcns,
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
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_gate_state():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


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


def _config(
    *,
    rcns_enabled: bool = True,
    min_trust_tier: int = 3,
    tmp_path: Optional[Path] = None,
    attribution_enabled: bool = False,
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
        rcns=RcnsConfig(enabled=rcns_enabled, min_trust_tier=min_trust_tier),
        audit=audit,
        signing=SigningConfig(),
    )


def _doc(
    *,
    agent_id: str = "a" * 64,
    scopes: list | None = None,
    trust_tier: int = 1,
) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["DISCOVER", "INSPECT", "QUERY", "REVOKE", "SUSPEND"],
            scopes=scopes if scopes is not None else ["rcns:negotiate"],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
        trust_tier=trust_tier,
    )


def _state(
    config: ServerConfig,
    *,
    runtime: Optional[SynthesisRuntime] = None,
    signing_service: Optional[SigningService] = None,
) -> Any:
    state = MagicMock()
    state.config = config
    state.synthesis_runtime = runtime or SynthesisRuntime()
    state.endpoint_registry = None
    state.signing_service = signing_service
    state.lookup_genesis = lambda _aid: None
    return state


def _req(
    *, method: str = "DISCOVER", path: str = "/",
    body: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
    agent_id: str = "a" * 64,
) -> wire.AGTPRequest:
    raw = json.dumps(body or {}).encode("utf-8")
    h = {"Agent-ID": agent_id, "Content-Length": str(len(raw))}
    if headers:
        h.update(headers)
    return wire.AGTPRequest(
        method=method, path=path, headers=h, body_bytes=raw,
    )


# ---------------------------------------------------------------------------
# Reserved roots + tier inventory.
# ---------------------------------------------------------------------------


def test_patterns_and_contracts_are_reserved_roots() -> None:
    assert "patterns" in DISCOVER_RESERVED_ROOTS
    assert "contracts" in DISCOVER_RESERVED_ROOTS


def test_patterns_and_contracts_are_tier_a() -> None:
    assert ("DISCOVER", "/patterns") in TIER_A_RESERVED_ENDPOINTS
    assert ("DISCOVER", "/contracts") in TIER_A_RESERVED_ENDPOINTS
    assert classify_tier("DISCOVER", "/patterns") == "A"
    assert classify_tier("DISCOVER", "/contracts") == "A"


def test_bare_discover_lists_patterns_and_contracts() -> None:
    from server.methods import handle_discover
    doc = _doc()
    state = _state(_config())
    state.list_ids = lambda: [doc.agent_id]
    state.lookup = lambda aid: doc if aid == doc.agent_id else None
    resp = handle_discover(_req(path="/"), state, doc)
    body = json.loads(resp.body_bytes)
    paths = {e["path"] for e in body["endpoints"]}
    assert "/patterns" in paths
    assert "/contracts" in paths


# ---------------------------------------------------------------------------
# DISCOVER /patterns.
# ---------------------------------------------------------------------------


def test_discover_patterns_returns_posture_and_pattern_list() -> None:
    from server.methods import handle_discover
    cfg = _config(rcns_enabled=True, min_trust_tier=2)
    runtime = SynthesisRuntime(policies=[RecipeBasedPolicy([
        Recipe(
            name="r1", description="x",
            pattern=RecipePattern(
                name_exact="RECONCILE", path_regex="^/accounts/.*$",
            ),
            steps=[CompositionStep(method_name="QUERY", parameter_source={})],
            version="3",
        ),
    ])])
    state = _state(cfg, runtime=runtime)
    resp = handle_discover(_req(path="/patterns"), state, _doc())
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "patterns"
    assert body["rcns"]["enabled"] is True
    assert body["rcns"]["min_trust_tier"] == 2
    assert "confirm-first" in body["rcns"]["modes"]
    assert "optimistic" in body["rcns"]["modes"]
    # Recipe inventory + passthrough policy presence.
    kinds = {p["kind"] for p in body["patterns"]}
    assert "recipe" in kinds
    assert "policy" in kinds
    recipe_entry = next(p for p in body["patterns"] if p["kind"] == "recipe")
    assert recipe_entry["name"] == "r1"
    assert recipe_entry["version"] == "3"
    assert recipe_entry["match"]["path_regex"] == "^/accounts/.*$"


def test_discover_patterns_reflects_disabled_rcns() -> None:
    """RCNS disabled → endpoint still reachable (it's Tier A), but the
    posture surfaces enabled=false and modes=[]."""
    from server.methods import handle_discover
    cfg = _config(rcns_enabled=False)
    state = _state(cfg)
    resp = handle_discover(_req(path="/patterns"), state, _doc())
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["rcns"]["enabled"] is False
    assert body["rcns"]["modes"] == []


# ---------------------------------------------------------------------------
# DISCOVER /contracts.
# ---------------------------------------------------------------------------


def test_discover_contracts_lists_callers_active_syntheses() -> None:
    from server.methods import handle_discover
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/things", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
        recipe_name=None,
        recipe_version=None,
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="a" * 64,
        contract_hash="hashA", negotiation_origin="rcns-confirmed",
    )

    resp = handle_discover(_req(path="/contracts"), state, _doc())
    body = json.loads(resp.body_bytes)
    assert body["target"] == "contracts"
    assert body["scope"] == "self"
    assert body["contract_count"] == 1
    entry = body["contracts"][0]
    assert entry["synthesis_id"] == sid
    assert entry["originating_agent_id"] == "a" * 64
    assert entry["contract_hash"] == "hashA"
    assert entry["negotiation_origin"] == "rcns-confirmed"
    assert entry["method"] == "QUERY"
    assert entry["path"] == "/things"


def test_discover_contracts_hides_other_agents_contracts() -> None:
    """A contract whose originating_agent_id is not the caller's is
    omitted by default — scope='self' means caller-only visibility."""
    from server.methods import handle_discover
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    other = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="", required_params=[],
            optional_params=[], namespace="rcns", category="negotiated",
            error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    runtime.instantiate(
        other, originating_agent_id="b" * 64,  # different agent
        contract_hash="hashB", negotiation_origin="rcns-confirmed",
    )

    resp = handle_discover(_req(path="/contracts"), state, _doc())
    body = json.loads(resp.body_bytes)
    assert body["contract_count"] == 0


def test_discover_contracts_inspect_all_reaches_across() -> None:
    """A caller with inspect:all sees contracts owned by other
    agents AND unscoped legacy contracts (operator visibility)."""
    from server.methods import handle_discover
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="", required_params=[],
            optional_params=[], namespace="rcns", category="negotiated",
            error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    runtime.instantiate(
        plan, originating_agent_id="b" * 64,
        contract_hash="hash", negotiation_origin="rcns-confirmed",
    )

    operator = _doc(scopes=["rcns:negotiate", "inspect:all"])
    resp = handle_discover(_req(path="/contracts"), state, operator)
    body = json.loads(resp.body_bytes)
    assert body["scope"] == "all"
    assert body["contract_count"] == 1


# ---------------------------------------------------------------------------
# INSPECT target=contract.
# ---------------------------------------------------------------------------


def test_inspect_contract_returns_plan_and_lineage() -> None:
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="RECONCILE", path="/accounts", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
        recipe_name="recipe-x",
        recipe_version="2",
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="a" * 64,
        contract_hash="hash-x", negotiation_origin="rcns-optimistic",
    )

    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "contract", "synthesis_id": sid,
        }),
        state, _doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "contract"
    assert body["synthesis_id"] == sid
    assert body["contract_hash"] == "hash-x"
    assert body["recipe_name"] == "recipe-x"
    assert body["recipe_version"] == "2"
    assert body["negotiation_origin"] == "rcns-optimistic"
    assert body["method_proposed"] == "RECONCILE"
    assert body["path_proposed"] == "/accounts"
    assert "plan" in body


def test_inspect_contract_refuses_cross_agent_without_inspect_all() -> None:
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="b" * 64,  # different
        contract_hash="h", negotiation_origin="rcns-confirmed",
    )
    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "contract", "synthesis_id": sid,
        }),
        state, _doc(),  # agent_id = "a" * 64
    )
    assert resp.status_code == 403
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "contract-not-yours"


def test_inspect_contract_inspect_all_reaches_across() -> None:
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="b" * 64,
        contract_hash="h", negotiation_origin="rcns-confirmed",
    )
    operator = _doc(scopes=["rcns:negotiate", "inspect:all"])
    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "contract", "synthesis_id": sid,
        }),
        state, operator,
    )
    assert resp.status_code == 200


def test_inspect_contract_missing_synthesis_id_returns_400() -> None:
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    state = _state(cfg)
    resp = handle_inspect(
        _req(method="INSPECT", body={"target": "contract"}),
        state, _doc(),
    )
    assert resp.status_code == 400


def test_inspect_contract_unknown_id_returns_404() -> None:
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    state = _state(cfg)
    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "contract", "synthesis_id": "syn-bogus",
        }),
        state, _doc(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# INSPECT target=rcns-attempt.
# ---------------------------------------------------------------------------


def test_inspect_rcns_attempt_returns_diagnostic_for_failed_negotiation() -> None:
    """End-to-end: a failed RCNS negotiation records an attempt; the
    INSPECT target=rcns-attempt surface resolves it by id."""
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    # Trigger an attempted-but-failed negotiation (no policy can
    # fulfill FROBNICATE).
    fail_resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="FROBNICATE", path="/unknown",
    )
    assert fail_resp.status_code == 464
    attempt_id = fail_resp.headers["RCNS-Attempt-Id"]
    assert attempt_id

    # The diagnostic is now retrievable.
    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "rcns-attempt", "attempt_id": attempt_id,
        }),
        state, _doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["attempt_id"] == attempt_id
    assert body["method_attempted"] == "FROBNICATE"
    assert body["path_attempted"] == "/unknown"
    assert body["reason"] == "composition-impossible"
    assert "policies_tried" in body["details"]


def test_inspect_rcns_attempt_unknown_id_returns_404() -> None:
    from server.methods import handle_inspect
    state = _state(_config())
    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "rcns-attempt", "attempt_id": "rcns-bogus",
        }),
        state, _doc(),
    )
    assert resp.status_code == 404


def test_inspect_rcns_attempt_refuses_cross_agent_without_inspect_all() -> None:
    from server.methods import handle_inspect
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    # Agent A triggers a failure.
    other_doc = _doc(agent_id="b" * 64)
    fail = try_rcns(
        _req(headers={"Allow-RCNS": "true"}, agent_id="b" * 64),
        state, other_doc,
        method="FROBNICATE", path="/x",
    )
    attempt_id = fail.headers["RCNS-Attempt-Id"]

    # Agent B (the caller) tries to inspect Agent A's attempt
    # without inspect:all → 403.
    resp = handle_inspect(
        _req(method="INSPECT", body={
            "target": "rcns-attempt", "attempt_id": attempt_id,
        }),
        state, _doc(),  # agent_id = "a" * 64, no inspect:all
    )
    assert resp.status_code == 403


def test_lookup_attempt_returns_none_for_unknown() -> None:
    assert lookup_attempt("rcns-nope") is None


# ---------------------------------------------------------------------------
# REVOKE target=contract.
# ---------------------------------------------------------------------------


def test_revoke_contract_expires_synthesis_and_returns_200() -> None:
    from server.methods import handle_revoke
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/things", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="a" * 64,
        contract_hash="h", negotiation_origin="rcns-confirmed",
    )

    resp = handle_revoke(
        _req(method="REVOKE", body={
            "target": "contract", "synthesis_id": sid,
            "reason": "operator decision",
        }),
        state, _doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "contract"
    assert body["synthesis_id"] == sid
    assert body["reason"] == "operator decision"
    # Runtime no longer holds the plan.
    assert runtime.get(sid) is None


def test_revoke_contract_refuses_non_originator_without_inspect_all() -> None:
    from server.methods import handle_revoke
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="b" * 64,
        contract_hash="h", negotiation_origin="rcns-confirmed",
    )
    resp = handle_revoke(
        _req(method="REVOKE", body={
            "target": "contract", "synthesis_id": sid,
        }),
        state, _doc(),  # agent_id = "a"*64
    )
    assert resp.status_code == 403
    assert runtime.get(sid) is not None  # still active


def test_revoke_contract_inspect_all_can_revoke_others() -> None:
    from server.methods import handle_revoke
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="b" * 64,
        contract_hash="h", negotiation_origin="rcns-confirmed",
    )
    operator = _doc(scopes=["rcns:negotiate", "inspect:all"])
    resp = handle_revoke(
        _req(method="REVOKE", body={
            "target": "contract", "synthesis_id": sid,
        }),
        state, operator,
    )
    assert resp.status_code == 200
    assert runtime.get(sid) is None


def test_revoke_contract_unknown_synthesis_returns_404() -> None:
    from server.methods import handle_revoke
    state = _state(_config(rcns_enabled=True))
    resp = handle_revoke(
        _req(method="REVOKE", body={
            "target": "contract", "synthesis_id": "syn-bogus",
        }),
        state, _doc(),
    )
    assert resp.status_code == 404


def test_revoke_contract_emits_rcns_revoke_audit_event(tmp_path: Path) -> None:
    """When attribution-records are on, REVOKE target=contract
    writes an rcns_revoke event onto the originating agent's
    lifecycle stream."""
    from server.audit_lifecycle import AuditLifecycleStore
    from server.methods import handle_revoke

    cfg = _config(rcns_enabled=True, tmp_path=tmp_path, attribution_enabled=True)
    runtime = SynthesisRuntime()
    sig = _make_signing_service(tmp_path)
    state = _state(cfg, runtime=runtime, signing_service=sig)

    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/things", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="a" * 64,
        contract_hash="hash-x", negotiation_origin="rcns-confirmed",
    )

    resp = handle_revoke(
        _req(method="REVOKE", body={
            "target": "contract", "synthesis_id": sid,
            "reason": "incident response",
        }),
        state, _doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert "audit_id" in body

    # Lifecycle stream now has the event.
    store = AuditLifecycleStore(tmp_path / "audit/lifecycle")
    lines = store.read_all("a" * 64)
    assert len(lines) == 1
    # Parse the JWS payload to confirm the event_type and synthesis_id.
    from server.signing import parse_attribution_record
    _, payload, _ = parse_attribution_record(lines[0])
    assert payload["extra"]["event_type"] == "rcns_revoke"
    assert payload["extra"]["synthesis_id"] == sid
    assert payload["extra"]["reason"] == "incident response"


# ---------------------------------------------------------------------------
# SUSPEND emits rcns_release.
# ---------------------------------------------------------------------------


def test_suspend_with_synthesis_id_emits_rcns_release(tmp_path: Path) -> None:
    """Agent-side release: SUSPEND with synthesis_id clears the
    contract and emits an rcns_release event."""
    from server.audit_lifecycle import AuditLifecycleStore
    from server.methods import handle_suspend

    cfg = _config(rcns_enabled=True, tmp_path=tmp_path, attribution_enabled=True)
    runtime = SynthesisRuntime()
    sig = _make_signing_service(tmp_path)
    state = _state(cfg, runtime=runtime, signing_service=sig)

    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/things", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan, originating_agent_id="a" * 64,
        contract_hash="hash-y", negotiation_origin="rcns-confirmed",
    )

    resp = handle_suspend(
        _req(method="SUSPEND", body={
            "synthesis_id": sid, "reason": "done with it",
        }),
        state, _doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["synthesis_cleared"] == sid
    assert "rcns_release_audit_id" in body

    store = AuditLifecycleStore(tmp_path / "audit/lifecycle")
    lines = store.read_all("a" * 64)
    assert len(lines) == 1
    from server.signing import parse_attribution_record
    _, payload, _ = parse_attribution_record(lines[0])
    assert payload["extra"]["event_type"] == "rcns_release"
    assert payload["extra"]["synthesis_id"] == sid


def test_suspend_without_synthesis_id_emits_no_rcns_event() -> None:
    """The rcns_release event fires only when a synthesis is
    actually cleared; ordinary SUSPEND (session suspension) is
    unaffected."""
    from server.methods import handle_suspend
    state = _state(_config())
    resp = handle_suspend(
        _req(method="SUSPEND", body={}),
        state, _doc(),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["synthesis_cleared"] is None
    assert "rcns_release_audit_id" not in body


# ---------------------------------------------------------------------------
# RCNS-3 gate emits rcns_propose_accepted when attribution-records is on.
# ---------------------------------------------------------------------------


def test_rcns_gate_emits_propose_accepted_when_attribution_on(
    tmp_path: Path,
) -> None:
    """RCNS-4: a successful negotiation writes a durable lifecycle
    event so the contract is auditable beyond the in-memory runtime."""
    from server.audit_lifecycle import AuditLifecycleStore

    cfg = _config(rcns_enabled=True, tmp_path=tmp_path, attribution_enabled=True)
    runtime = SynthesisRuntime()
    sig = _make_signing_service(tmp_path)
    state = _state(cfg, runtime=runtime, signing_service=sig)

    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="QUERY", path="/things",
    )
    assert resp.status_code == 461

    store = AuditLifecycleStore(tmp_path / "audit/lifecycle")
    lines = store.read_all("a" * 64)
    assert len(lines) == 1
    from server.signing import parse_attribution_record
    _, payload, _ = parse_attribution_record(lines[0])
    assert payload["extra"]["event_type"] == "rcns_propose_accepted"
    assert payload["extra"]["method"] == "QUERY"
    assert payload["extra"]["path"] == "/things"


def test_rcns_gate_silent_when_attribution_off() -> None:
    """When attribution-records is off, the gate succeeds but no
    durable audit event is written — RCNS doesn't need the audit
    chain to function."""
    from server.audit_lifecycle import AuditLifecycleStore

    cfg = _config(rcns_enabled=True)  # attribution off
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="QUERY", path="/things",
    )
    assert resp.status_code == 461
    # No store configured; no lifecycle stream to check. Confirm the
    # contract is still in the runtime — the negotiation worked.
    sid = json.loads(resp.body_bytes)["proposed_synthesis_id"]
    assert runtime.get(sid) is not None


# ---------------------------------------------------------------------------
# Unknown INSPECT target.
# ---------------------------------------------------------------------------


def test_inspect_unknown_target_lists_all_options() -> None:
    """The 422 explanation should enumerate the full RCNS-4 set."""
    from server.methods import handle_inspect
    state = _state(_config())
    resp = handle_inspect(
        _req(method="INSPECT", body={"target": "made-up"}),
        state, _doc(),
    )
    assert resp.status_code == 422
    body = json.loads(resp.body_bytes)
    detail = body["error"]["detail"]
    for word in ("audit", "chain_head", "lifecycle", "contract", "rcns-attempt"):
        assert word in detail
