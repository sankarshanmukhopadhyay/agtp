"""
Tests for RCNS-3 — RCNS dispatcher gate.

Covers the four-lock gate, both delivery modes (confirm-first and
optimistic), structured 464 refusals, contract scoping (404 ➜
404 vs 461 vs 464 ``contract-not-yours``), per-agent rate
limiting, idempotency cache, Attribution-Record extension fields,
and the contract-hash canonicalization.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from core import wire
from core.endpoint import EndpointSpec
from core.identity import AgentDocument, RequiresDeclaration
from server.config import (
    AgentsConfig, AuditConfig, GatewayConfig, MtlsConfig,
    RcnsConfig, ServerConfig, ServerInfo, ServerPolicy,
    SigningConfig, SynthesisConfig,
)
from server.rcns_gate import (
    contract_hash, parse_allow_rcns, reset_state_for_tests, try_rcns,
)
from server.synthesis.plan import CompositionStep, SynthesisPlan
from server.synthesis.runtime import SynthesisRuntime


# ---------------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_gate_state():
    """Each test starts with a clean rate-limiter + idempotency
    cache. RCNS-3 holds module-level state so we have to scrub
    it between tests."""
    reset_state_for_tests()
    yield
    reset_state_for_tests()


def _config(
    *,
    rcns_enabled: bool = True,
    min_trust_tier: int = 3,
    max_negotiations: int = 100,
    idempotency_window: int = 60,
) -> ServerConfig:
    return ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        policy=ServerPolicy(synthesis_enabled=True),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(
            enabled=rcns_enabled,
            min_trust_tier=min_trust_tier,
            max_negotiations_per_minute=max_negotiations,
            idempotency_window_seconds=idempotency_window,
        ),
        audit=AuditConfig(),
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
            methods=["DISCOVER", "RECONCILE", "QUERY"],
            scopes=scopes if scopes is not None else ["rcns:negotiate"],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
        trust_tier=trust_tier,
    )


def _state(config: ServerConfig, *, runtime: Optional[SynthesisRuntime] = None) -> Any:
    state = MagicMock()
    state.config = config
    state.synthesis_runtime = runtime or SynthesisRuntime()
    state.endpoint_registry = None
    return state


def _req(
    *,
    method: str = "RECONCILE",
    path: str = "/accounts",
    headers: Dict[str, str] | None = None,
) -> wire.AGTPRequest:
    base_headers: Dict[str, str] = {
        "Agent-ID": "a" * 64, "Content-Length": "2",
    }
    if headers:
        base_headers.update(headers)
    return wire.AGTPRequest(
        method=method, path=path,
        headers=base_headers, body_bytes=b"{}",
    )


# ---------------------------------------------------------------------------
# Allow-RCNS header parser.
# ---------------------------------------------------------------------------


def test_parse_allow_rcns_recognizes_true() -> None:
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": "true"})) == "true"


def test_parse_allow_rcns_recognizes_optimistic() -> None:
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": "optimistic"})) == "optimistic"


def test_parse_allow_rcns_is_case_insensitive() -> None:
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": "TRUE"})) == "true"
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": "OPTIMISTIC"})) == "optimistic"


def test_parse_allow_rcns_returns_none_for_unknown_values() -> None:
    """A typo or unexpected value falls through; we don't want
    'maybe' or 'false' to accidentally fire negotiation."""
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": "yes"})) is None
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": "false"})) is None
    assert parse_allow_rcns(_req(headers={"Allow-RCNS": ""})) is None


def test_parse_allow_rcns_returns_none_when_missing() -> None:
    assert parse_allow_rcns(_req()) is None


# ---------------------------------------------------------------------------
# Lock 1 — server config.
# ---------------------------------------------------------------------------


def test_lock1_rcns_disabled_falls_through_silently() -> None:
    """When the server hasn't opted into RCNS, the gate returns None
    so the dispatcher hands back the ordinary 404. We don't advertise
    the mechanism via a 464 here — the server simply doesn't know
    about it."""
    cfg = _config(rcns_enabled=False)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        _state(cfg),
        _doc(),
        method="RECONCILE", path="/accounts",
    )
    assert resp is None


def test_lock1_no_config_at_all_falls_through() -> None:
    """server_state without a config (legacy / test stub) must not
    panic — it just falls through to 404."""
    state = MagicMock()
    state.config = None
    state.synthesis_runtime = SynthesisRuntime()
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state,
        _doc(),
        method="RECONCILE", path="/accounts",
    )
    assert resp is None


# ---------------------------------------------------------------------------
# Lock 2 — caller intent (Allow-RCNS header).
# ---------------------------------------------------------------------------


def test_lock2_missing_header_falls_through() -> None:
    """No Allow-RCNS header → silent 404 fall-through. Plain 404 is
    the right behavior here because the caller didn't ask for
    negotiation."""
    cfg = _config(rcns_enabled=True)
    resp = try_rcns(
        _req(),  # no Allow-RCNS
        _state(cfg),
        _doc(),
        method="RECONCILE", path="/accounts",
    )
    assert resp is None


# ---------------------------------------------------------------------------
# Lock 3 — agent capability scope.
# ---------------------------------------------------------------------------


def test_lock3_missing_scope_returns_262() -> None:
    """An agent without ``rcns:negotiate`` in its scopes gets a
    structured 262 telling it what to add."""
    cfg = _config(rcns_enabled=True)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        _state(cfg),
        _doc(scopes=[]),  # no rcns:negotiate
        method="RECONCILE", path="/accounts",
    )
    assert resp is not None
    assert resp.status_code == 262
    body = json.loads(resp.body_bytes)
    assert body["error"]["type"] == "scope-required"
    assert "rcns:negotiate" in body["error"]["details"]["missing_scopes"]
    assert body["error"]["details"]["code"] == "rcns-scope-required"


# ---------------------------------------------------------------------------
# Lock 4 — trust posture.
# ---------------------------------------------------------------------------


def test_lock4_trust_tier_below_minimum_returns_464() -> None:
    """min_trust_tier = 1 admits only Tier 1; a Tier 3 caller is
    refused with a structured trust-tier-insufficient reason."""
    cfg = _config(rcns_enabled=True, min_trust_tier=1)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        _state(cfg),
        _doc(trust_tier=3),
        method="RECONCILE", path="/accounts",
    )
    assert resp is not None
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "trust-tier-insufficient"
    assert body["error"]["details"]["min_trust_tier"] == 1
    assert body["error"]["details"]["agent_trust_tier"] == 3


def test_lock4_trust_tier_at_or_below_minimum_passes() -> None:
    """min_trust_tier = 2 admits Tier 1 and Tier 2. Verifies the
    direction of the comparison."""
    cfg = _config(rcns_enabled=True, min_trust_tier=2)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state,
        _doc(trust_tier=2),  # at threshold
        method="QUERY", path="/things",
    )
    # Composition succeeds via PassthroughPolicy since QUERY is a
    # built-in method — 461 not 464.
    assert resp is not None
    assert resp.status_code == 461


# ---------------------------------------------------------------------------
# Confirm-first path: 461 RCNS Contract Available.
# ---------------------------------------------------------------------------


def test_confirm_first_returns_461_with_contract_preview() -> None:
    cfg = _config(rcns_enabled=True)
    state = _state(cfg)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state,
        _doc(),
        method="QUERY", path="/things",
    )
    assert resp is not None
    assert resp.status_code == 461
    body = json.loads(resp.body_bytes)
    assert "proposed_synthesis_id" in body
    assert body["contract"]["method"] == "QUERY"
    assert body["contract"]["path"] == "/things"
    assert body["contract"]["policy_name"] == "passthrough"


def test_confirm_first_synthesis_id_is_executable_by_originator() -> None:
    """After receiving a 461 preview, the originating agent can
    present the synthesis_id and execute. The contract-scoping check
    accepts the originator (RCNS-3 contract scoping is "your own id
    works")."""
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    preview = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="QUERY", path="/things",
    )
    sid = json.loads(preview.body_bytes)["proposed_synthesis_id"]
    # The runtime now holds the plan; originating_agent_id matches.
    assert runtime.originating_agent_id(sid) == "a" * 64
    assert runtime.negotiation_origin(sid) == "rcns-confirmed"


# ---------------------------------------------------------------------------
# Optimistic path: response carries Contract-Synthesized header.
# ---------------------------------------------------------------------------


def test_optimistic_executes_inline_and_sets_response_header() -> None:
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()

    # Stub step_dispatcher so the runtime can execute the plan.
    def _stub_dispatcher(req, _state, _doc):
        body_bytes = json.dumps({"result": "ok"}).encode("utf-8")
        return wire.AGTPResponse(
            status_code=200, status_text="OK",
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body_bytes))},
            body_bytes=body_bytes,
        )

    runtime.step_dispatcher = _stub_dispatcher
    state = _state(cfg, runtime=runtime)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "optimistic"}),
        state, _doc(),
        method="QUERY", path="/things",
    )
    assert resp is not None
    # Synthesis executed inline; runtime returns 200 on plan success.
    assert resp.status_code == 200
    assert "Contract-Synthesized" in resp.headers
    sid = resp.headers["Contract-Synthesized"]
    assert sid.startswith("syn-")
    # Origin tagged as optimistic.
    assert runtime.negotiation_origin(sid) == "rcns-optimistic"


def test_optimistic_stashes_attribution_extras() -> None:
    """Optimistic dispatch surfaces synthesis_id / contract_hash /
    negotiation_origin to _finalize_response via _attribution_extra
    so the Attribution-Record carries them."""
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    runtime.step_dispatcher = lambda req, _s, _d: wire.AGTPResponse(
        status_code=200, status_text="OK", headers={}, body_bytes=b"{}",
    )
    state = _state(cfg, runtime=runtime)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "optimistic"}),
        state, _doc(),
        method="QUERY", path="/things",
    )
    extras = getattr(resp, "_attribution_extra", None)
    assert isinstance(extras, dict)
    assert extras["synthesis_id"].startswith("syn-")
    assert extras["negotiation_origin"] == "rcns-optimistic"
    assert len(extras["contract_hash"]) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Composition refusal: 464 composition-impossible.
# ---------------------------------------------------------------------------


def test_composition_impossible_returns_464() -> None:
    """When no policy can fulfill the proposal, the gate returns 464
    with a structured composition-impossible reason."""
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    # Empty policies = passthrough only; an unknown method won't
    # match anything in REGISTRY either, so composition fails.
    state = _state(cfg, runtime=runtime)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="FROBNICATE", path="/no/such/thing",
    )
    assert resp is not None
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "composition-impossible"
    assert body["error"]["method"] == "FROBNICATE"
    assert body["error"]["path"] == "/no/such/thing"


def test_synthesis_runtime_exception_returns_464_synthesis_error() -> None:
    """When the synthesis runtime crashes mid-composition, the gate
    surfaces 464 ``synthesis-error`` so the operator can find it in
    the audit record rather than a 500."""
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()

    def _crash(*_args, **_kwargs):
        raise RuntimeError("policy boom")

    runtime.attempt_synthesis = _crash  # type: ignore[method-assign]
    state = _state(cfg, runtime=runtime)
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="RECONCILE", path="/accounts",
    )
    assert resp is not None
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "synthesis-error"
    assert body["error"]["details"]["exception"] == "RuntimeError"


def test_missing_synthesis_runtime_returns_synthesis_error() -> None:
    """The gate is enabled but the server forgot to wire up a runtime
    — return a structured 464 (not a 500) so operators see the
    misconfig."""
    cfg = _config(rcns_enabled=True)
    state = _state(cfg)
    state.synthesis_runtime = None
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, _doc(),
        method="RECONCILE", path="/accounts",
    )
    assert resp is not None
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "synthesis-error"


# ---------------------------------------------------------------------------
# Per-agent rate limit.
# ---------------------------------------------------------------------------


def test_rate_limit_returns_429_with_rcns_scope() -> None:
    """After N negotiations in a minute the gate returns 429 with
    ``error.scope = 'rcns'`` so the caller distinguishes negotiation
    throttling from ordinary throttling."""
    cfg = _config(rcns_enabled=True, max_negotiations=2)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)

    # Two successful negotiations.
    for path in ("/a", "/b"):
        resp = try_rcns(
            _req(headers={"Allow-RCNS": "true"}, path=path),
            state, _doc(),
            method="QUERY", path=path,
        )
        assert resp.status_code == 461

    # Third should be rate-limited.
    resp = try_rcns(
        _req(headers={"Allow-RCNS": "true"}, path="/c"),
        state, _doc(),
        method="QUERY", path="/c",
    )
    assert resp.status_code == 429
    body = json.loads(resp.body_bytes)
    assert body["error"]["scope"] == "rcns"


def test_rate_limit_zero_means_unlimited() -> None:
    """A limit of 0 disables rate-limiting entirely (matches the
    convention used elsewhere in the codebase for "no limit")."""
    cfg = _config(rcns_enabled=True, max_negotiations=0)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    for path in (f"/a{i}" for i in range(20)):
        resp = try_rcns(
            _req(headers={"Allow-RCNS": "true"}, path=path),
            state, _doc(),
            method="QUERY", path=path,
        )
        assert resp.status_code != 429


# ---------------------------------------------------------------------------
# Idempotency key.
# ---------------------------------------------------------------------------


def test_idempotency_key_short_circuits_second_negotiation() -> None:
    """Same RCNS-Idempotency-Key from the same agent returns the same
    synthesis_id; the runtime is consulted but composition is not
    re-run."""
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    headers = {"Allow-RCNS": "true", "RCNS-Idempotency-Key": "deadbeef"}

    r1 = try_rcns(
        _req(headers=headers),
        state, _doc(),
        method="QUERY", path="/things",
    )
    r2 = try_rcns(
        _req(headers=headers),
        state, _doc(),
        method="QUERY", path="/things",
    )
    sid1 = json.loads(r1.body_bytes)["proposed_synthesis_id"]
    sid2 = json.loads(r2.body_bytes)["proposed_synthesis_id"]
    assert sid1 == sid2


def test_idempotency_key_scoped_to_agent_id() -> None:
    """Two different agents using the same idempotency key get
    different synthesis_ids — the cache is keyed by (agent_id, key)."""
    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    headers = {"Allow-RCNS": "true", "RCNS-Idempotency-Key": "deadbeef"}

    a1 = _doc(agent_id="a" * 64)
    a2 = _doc(agent_id="b" * 64)
    r1 = try_rcns(_req(headers=headers), state, a1, method="QUERY", path="/things")
    r2 = try_rcns(_req(headers=headers), state, a2, method="QUERY", path="/things")
    sid1 = json.loads(r1.body_bytes)["proposed_synthesis_id"]
    sid2 = json.loads(r2.body_bytes)["proposed_synthesis_id"]
    assert sid1 != sid2


def test_idempotency_window_zero_disables_caching() -> None:
    cfg = _config(rcns_enabled=True, idempotency_window=0)
    runtime = SynthesisRuntime()
    state = _state(cfg, runtime=runtime)
    headers = {"Allow-RCNS": "true", "RCNS-Idempotency-Key": "deadbeef"}
    r1 = try_rcns(_req(headers=headers), state, _doc(), method="QUERY", path="/things")
    r2 = try_rcns(_req(headers=headers), state, _doc(), method="QUERY", path="/things")
    sid1 = json.loads(r1.body_bytes)["proposed_synthesis_id"]
    sid2 = json.loads(r2.body_bytes)["proposed_synthesis_id"]
    assert sid1 != sid2


# ---------------------------------------------------------------------------
# Contract scoping (the dispatcher-side check, exercised end-to-end).
# ---------------------------------------------------------------------------


def test_contract_scoping_refuses_different_agent() -> None:
    """A synthesis_id presented by an agent other than the originator
    is refused with 464 ``contract-not-yours``. The check runs in
    server.methods._dispatch_inner (RCNS-3); we exercise via the
    dispatcher."""
    from server.methods import dispatch

    cfg = _config(rcns_enabled=True)
    runtime = SynthesisRuntime()
    runtime.step_dispatcher = lambda req, _s, _d: wire.AGTPResponse(
        status_code=200, status_text="OK", headers={}, body_bytes=b"{}",
    )
    state = _state(cfg, runtime=runtime)

    # Originator negotiates a contract.
    originator = _doc(agent_id="a" * 64)
    preview = try_rcns(
        _req(headers={"Allow-RCNS": "true"}),
        state, originator,
        method="QUERY", path="/things",
    )
    sid = json.loads(preview.body_bytes)["proposed_synthesis_id"]

    # A different agent tries to use the synthesis_id.
    attacker = _doc(agent_id="b" * 64)
    body_bytes = json.dumps({}).encode("utf-8")
    attacker_request = wire.AGTPRequest(
        method="QUERY", path="/things",
        headers={
            "Agent-ID": attacker.agent_id,
            "Synthesis-Id": sid,
            "Content-Length": str(len(body_bytes)),
        },
        body_bytes=body_bytes,
    )
    resp = dispatch(attacker_request, state, attacker, config=cfg)
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "contract-not-yours"
    assert body["error"]["details"]["presenter_agent_id"] == attacker.agent_id


# ---------------------------------------------------------------------------
# Contract hash canonicalization.
# ---------------------------------------------------------------------------


def test_contract_hash_is_canonical() -> None:
    """Two contracts with the same logical shape but different key
    ordering produce the same hash."""
    h1 = contract_hash({"method": "Q", "path": "/x", "v": 1})
    h2 = contract_hash({"v": 1, "path": "/x", "method": "Q"})
    assert h1 == h2


def test_contract_hash_is_sha256_hex() -> None:
    h = contract_hash({"method": "Q", "path": "/x"})
    assert len(h) == 64
    int(h, 16)  # must be valid hex


def test_contract_hash_changes_with_content() -> None:
    h1 = contract_hash({"method": "Q", "path": "/x"})
    h2 = contract_hash({"method": "Q", "path": "/y"})
    assert h1 != h2


# ---------------------------------------------------------------------------
# Runtime side-table cleanup on expire.
# ---------------------------------------------------------------------------


def test_expire_clears_originator_and_contract_hash() -> None:
    """When a synthesis is expired the side-tables drain so the
    metadata can't leak into a new entry that happens to reuse the
    id (defensive — synthesis_ids are 96 bits but cleanup hygiene
    matters)."""
    runtime = SynthesisRuntime()
    plan = SynthesisPlan(
        proposed_method=EndpointSpec(
            name="QUERY", path="/x", description="",
            required_params=[], optional_params=[],
            namespace="rcns", category="negotiated", error_codes=[],
        ),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
    )
    sid = runtime.instantiate(
        plan,
        originating_agent_id="a" * 64,
        contract_hash="x" * 64,
        negotiation_origin="rcns-confirmed",
    )
    assert runtime.originating_agent_id(sid) == "a" * 64
    runtime.expire(sid, reason="test")
    assert runtime.originating_agent_id(sid) is None
    assert runtime.contract_hash(sid) is None
    assert runtime.negotiation_origin(sid) == "propose-explicit"  # default


# ---------------------------------------------------------------------------
# RcnsConfig validation.
# ---------------------------------------------------------------------------


def test_rcns_config_rejects_invalid_trust_tier() -> None:
    with pytest.raises(ValueError, match="min_trust_tier"):
        RcnsConfig(min_trust_tier=0)
    with pytest.raises(ValueError, match="min_trust_tier"):
        RcnsConfig(min_trust_tier=4)


def test_rcns_config_rejects_negative_rate_limit() -> None:
    with pytest.raises(ValueError, match="max_negotiations"):
        RcnsConfig(max_negotiations_per_minute=-1)


def test_rcns_config_rejects_invalid_policy_change_value() -> None:
    with pytest.raises(ValueError, match="on_policy_change"):
        RcnsConfig(on_policy_change="ignore")


def test_rcns_config_loads_from_toml(tmp_path: Path) -> None:
    """The [policies.rcns] block parses into RcnsConfig with the
    declared values."""
    from server import config as cfg_module
    f = tmp_path / "agtp-server.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "test"
contact = "x"

[policies.rcns]
enabled = true
min_trust_tier = 2
max_negotiations_per_minute = 25
idempotency_window_seconds = 120
on_policy_change = "invalidate"
""",
        encoding="utf-8",
    )
    loaded = cfg_module.load(f)
    assert loaded.rcns.enabled is True
    assert loaded.rcns.min_trust_tier == 2
    assert loaded.rcns.max_negotiations_per_minute == 25
    assert loaded.rcns.idempotency_window_seconds == 120
    assert loaded.rcns.on_policy_change == "invalidate"


def test_rcns_config_defaults_when_block_absent(tmp_path: Path) -> None:
    """Configs without [policies.rcns] load cleanly with all defaults
    — RCNS is off, lockstep with the rest of the dispatcher."""
    from server import config as cfg_module
    f = tmp_path / "agtp-server.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "test"
contact = "x"
""",
        encoding="utf-8",
    )
    loaded = cfg_module.load(f)
    assert loaded.rcns.enabled is False
    assert loaded.rcns.min_trust_tier == 1
