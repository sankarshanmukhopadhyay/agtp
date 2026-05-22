"""
Tests for RCNS-2 — Endpoint-keyed PROPOSE + recipe versioning.

Covers:
  * Wrapped ``endpoint`` body form is accepted and produces 263.
  * Legacy top-level ``name`` form still works (migration shim).
  * Mutual exclusivity: body carrying both ``name`` AND ``endpoint``
    returns 400.
  * RecipePattern matches on path_exact / path_regex.
  * Recipe version field defaults to "1" and propagates onto the
    synthesized plan.
  * SynthesisRuntime.resolve(method, path) finds active plans for
    use by core.endpoint_tiers.classify_tier (the RCNS-3 hook).
  * 263 body carries top-level ``method`` and ``path`` so callers
    don't have to dig.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from core import wire
from core.endpoint import EndpointSpec, ParamSpec
from core.endpoint_tiers import TIER_RCNS, classify_tier
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


def _spec(name: str = "RECONCILE", path: str | None = "/accounts") -> EndpointSpec:
    return EndpointSpec(
        name=name,
        path=path,
        description=f"proposal for {name}",
        required_params=[],
        optional_params=[],
        namespace="proposal",
        category="custom",
        error_codes=[400],
    )


def _recipe(
    name: str = "recon-recipe",
    pattern: RecipePattern | None = None,
    target: str = "QUERY",
    version: str = "1",
) -> Recipe:
    return Recipe(
        name=name,
        description="reconcile via query",
        pattern=pattern or RecipePattern(name_exact="RECONCILE"),
        steps=[CompositionStep(method_name=target, parameter_source={})],
        output_aggregation="last",
        version=version,
    )


def _available(*names: str) -> list:
    return [_spec(name=n, path=None) for n in names]


# ---------------------------------------------------------------------------
# RecipePattern path matching.
# ---------------------------------------------------------------------------


def test_pattern_path_exact_matches_proposal_path() -> None:
    p = RecipePattern(name_exact="RECONCILE", path_exact="/accounts")
    assert p.matches(_spec("RECONCILE", "/accounts"))
    assert not p.matches(_spec("RECONCILE", "/orders"))


def test_pattern_path_exact_treats_none_as_root() -> None:
    """Method-only proposals (path=None) compare as '/'."""
    p = RecipePattern(name_exact="RECONCILE", path_exact="/")
    assert p.matches(_spec("RECONCILE", None))


def test_pattern_path_regex_matches() -> None:
    p = RecipePattern(name_exact="RECONCILE", path_regex=r"^/accounts/\w+$")
    assert p.matches(_spec("RECONCILE", "/accounts/abc"))
    assert not p.matches(_spec("RECONCILE", "/accounts"))
    assert not p.matches(_spec("RECONCILE", "/orders/abc"))


def test_pattern_without_path_filter_matches_any_path() -> None:
    """The legacy behavior — a method-only pattern matches every path."""
    p = RecipePattern(name_exact="RECONCILE")
    assert p.matches(_spec("RECONCILE", "/accounts"))
    assert p.matches(_spec("RECONCILE", "/anywhere"))
    assert p.matches(_spec("RECONCILE", None))


# ---------------------------------------------------------------------------
# Recipe versioning.
# ---------------------------------------------------------------------------


def test_recipe_version_defaults_to_one() -> None:
    r = _recipe()
    assert r.version == "1"


def test_recipe_version_required_to_be_non_empty_string() -> None:
    with pytest.raises(ValueError, match="version"):
        Recipe(
            name="x", description="", pattern=RecipePattern(name_exact="X"),
            steps=[CompositionStep(method_name="Q", parameter_source={})],
            version="",
        )


def test_compose_stamps_recipe_name_and_version() -> None:
    """A plan synthesized through a recipe captures the recipe's name
    and version so an operator editing the recipe afterwards doesn't
    silently mutate already-bound contracts."""
    policy = RecipeBasedPolicy([_recipe(version="3.7")])
    proposal = _spec("RECONCILE", "/accounts")
    plan = policy.compose(proposal, _available("QUERY"))
    assert plan is not None
    assert plan.recipe_name == "recon-recipe"
    assert plan.recipe_version == "3.7"
    assert plan.policy_name == "recipes"


def test_passthrough_plans_have_no_recipe_lineage() -> None:
    """Non-recipe origins (PassthroughPolicy) leave the new fields
    blank so the 263 response doesn't claim a lineage it doesn't have."""
    from server.synthesis.policies import PassthroughPolicy
    policy = PassthroughPolicy()
    proposal = _spec("QUERY", None)
    plan = policy.compose(proposal, [_spec("QUERY", None)])
    assert plan is not None
    assert plan.recipe_name is None
    assert plan.recipe_version is None


def test_recipe_pattern_with_path_loaded_from_toml(tmp_path: Path) -> None:
    """The TOML loader picks up path_exact / path_regex / version."""
    from server.synthesis.recipes import load_recipes
    f = tmp_path / "r.toml"
    f.write_text(
        """
[[recipe]]
name = "recon-on-accounts"
description = "RECONCILE only on /accounts/*"
version = "2"

[recipe.pattern]
name_exact = "RECONCILE"
path_regex = "^/accounts/.*$"

[[recipe.steps]]
method = "QUERY"
""",
        encoding="utf-8",
    )
    recipes = load_recipes(f)
    assert len(recipes) == 1
    r = recipes[0]
    assert r.version == "2"
    assert r.pattern.path_regex == "^/accounts/.*$"
    # Pattern fires on the constrained path, not on others.
    assert r.pattern.matches(_spec("RECONCILE", "/accounts/123"))
    assert not r.pattern.matches(_spec("RECONCILE", "/orders/123"))


def test_version_omitted_in_toml_defaults_to_one(tmp_path: Path) -> None:
    """Legacy recipes without a version field load as version='1' (the
    migration shim — pre-versioning recipes don't break)."""
    from server.synthesis.recipes import load_recipes
    f = tmp_path / "legacy.toml"
    f.write_text(
        """
[[recipe]]
name = "legacy-recon"
description = "no version field"

[recipe.pattern]
name_exact = "RECONCILE"

[[recipe.steps]]
method = "QUERY"
""",
        encoding="utf-8",
    )
    recipes = load_recipes(f)
    assert recipes[0].version == "1"


# ---------------------------------------------------------------------------
# SynthesisRuntime.resolve — the (method, path) lookup.
# ---------------------------------------------------------------------------


def _make_active_plan(method: str, path: str | None) -> SynthesisPlan:
    return SynthesisPlan(
        proposed_method=_spec(method, path),
        steps=[CompositionStep(method_name="QUERY", parameter_source={})],
        recipe_name="r",
        recipe_version="1.2",
    )


def test_resolve_finds_active_plan_by_method_and_path() -> None:
    rt = SynthesisRuntime()
    sid = rt.instantiate(_make_active_plan("RECONCILE", "/accounts"))
    rec = rt.resolve("RECONCILE", "/accounts")
    assert rec is not None
    assert rec["synthesis_id"] == sid
    assert rec["method"] == "RECONCILE"
    assert rec["path"] == "/accounts"
    assert rec["recipe_name"] == "r"
    assert rec["recipe_version"] == "1.2"


def test_resolve_is_method_case_insensitive() -> None:
    rt = SynthesisRuntime()
    rt.instantiate(_make_active_plan("RECONCILE", "/accounts"))
    assert rt.resolve("reconcile", "/accounts") is not None


def test_resolve_returns_none_when_no_active_plan_matches() -> None:
    rt = SynthesisRuntime()
    rt.instantiate(_make_active_plan("RECONCILE", "/accounts"))
    assert rt.resolve("RECONCILE", "/orders") is None
    assert rt.resolve("QUERY", "/accounts") is None


def test_resolve_treats_method_only_plan_as_path_root() -> None:
    """A plan whose proposed_method has no path resolves only against
    the path ``"/"``."""
    rt = SynthesisRuntime()
    rt.instantiate(_make_active_plan("QUERY", None))
    assert rt.resolve("QUERY", "/") is not None
    assert rt.resolve("QUERY", "/anywhere") is None


def test_classify_tier_returns_c_via_runtime_resolve() -> None:
    """End-to-end check that the RCNS-1 classifier hook fires when the
    runtime resolves an (method, path) pair to an active synthesis."""
    rt = SynthesisRuntime()
    rt.instantiate(_make_active_plan("RECONCILE", "/accounts"))
    assert classify_tier(
        "RECONCILE", "/accounts",
        synthesis_lookup=rt,
    ) == TIER_RCNS


# ---------------------------------------------------------------------------
# handle_propose — wrapped ``endpoint`` body form.
# ---------------------------------------------------------------------------


def _make_state(monkeypatch=None) -> Any:
    """A minimal ServerState stub good enough for handle_propose."""
    from core import methods as core_methods
    from server.config import (
        ServerConfig, ServerInfo, AuditConfig, ServerPolicy,
        SynthesisConfig, SigningConfig,
    )

    config = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        audit=AuditConfig(),
        policy=ServerPolicy(synthesis_enabled=True),
        synthesis=SynthesisConfig(),
        signing=SigningConfig(),
    )

    runtime = SynthesisRuntime()

    state = MagicMock()
    state.config = config
    state.synthesis_runtime = runtime
    state.proposal_store = None
    state.audit_path = "none"
    state.attribution_record_signer = None
    return state


def _doc():
    from core.identity import AgentDocument, RequiresDeclaration
    return AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(methods=["PROPOSE", "QUERY"]),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


def _req(body: Dict[str, Any]) -> wire.AGTPRequest:
    raw = json.dumps(body).encode("utf-8")
    return wire.AGTPRequest(
        method="PROPOSE", path="/",
        headers={"Agent-ID": "a" * 64, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


def test_legacy_name_form_still_works() -> None:
    """Pre-RCNS-2 PROPOSE bodies (top-level name + path) keep
    working — the migration shim preserves the existing contract."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({"name": "QUERY", "path": "/things"}),
        state, _doc(),
    )
    # 263 because PassthroughPolicy finds QUERY in the registry.
    assert resp.status_code == 263
    body = json.loads(resp.body_bytes)
    assert "synthesis_id" in body


def test_wrapped_endpoint_form_accepted() -> None:
    """The new RCNS-2 body shape — wrapped ``endpoint`` block — is
    accepted and produces the same 263 outcome."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({"endpoint": {"method": "QUERY", "path": "/things"}}),
        state, _doc(),
    )
    assert resp.status_code == 263
    body = json.loads(resp.body_bytes)
    assert "synthesis_id" in body


def test_wrapped_form_carries_method_and_path_in_263() -> None:
    """RCNS-2: the 263 body surfaces resolved (method, path) at the
    top level so callers don't have to dig into the endpoint
    subdict."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({"endpoint": {"method": "QUERY", "path": "/things"}}),
        state, _doc(),
    )
    body = json.loads(resp.body_bytes)
    assert body["method"] == "QUERY"
    assert body["path"] == "/things"


def test_legacy_form_carries_method_and_path_in_263() -> None:
    """The same top-level (method, path) annotation rides on responses
    to legacy-shaped requests."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({"name": "QUERY", "path": "/things"}),
        state, _doc(),
    )
    body = json.loads(resp.body_bytes)
    assert body["method"] == "QUERY"
    assert body["path"] == "/things"


def test_method_only_legacy_form_normalizes_path_to_root() -> None:
    """A pre-path-support proposal (no path) normalizes to path='/'
    in the 263 body."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(_req({"name": "QUERY"}), state, _doc())
    body = json.loads(resp.body_bytes)
    assert body["method"] == "QUERY"
    assert body["path"] == "/"


def test_body_with_both_name_and_endpoint_returns_400() -> None:
    """Mutual exclusivity: a body carrying both top-level ``name`` and
    wrapped ``endpoint`` is malformed."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({
            "name": "QUERY",
            "endpoint": {"method": "DISCOVER", "path": "/x"},
        }),
        state, _doc(),
    )
    assert resp.status_code == 400
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "bad-request"
    assert "name" in body["error"]["details"]["conflict"]
    assert "endpoint" in body["error"]["details"]["conflict"]


def test_endpoint_block_must_be_an_object() -> None:
    """``endpoint`` carrying a non-object value is malformed."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(_req({"endpoint": "QUERY"}), state, _doc())
    assert resp.status_code == 400
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "bad-request"
    assert body["error"]["details"]["field"] == "endpoint"


def test_endpoint_block_without_method_is_missing_required_field() -> None:
    """An ``endpoint`` block that doesn't carry ``method`` reduces to
    a missing-required-field 400 (the same surface as the legacy
    missing-name path)."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({"endpoint": {"path": "/x"}}),
        state, _doc(),
    )
    assert resp.status_code == 400
    body = json.loads(resp.body_bytes)
    assert body["error"]["issue"] == "missing-required-field"


def test_wrapped_form_carries_input_and_output_schemas() -> None:
    """Schemas declared inside the wrapped block are promoted to top
    level so the existing JSON-Schema well-formedness gate applies."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({
            "endpoint": {
                "method": "QUERY",
                "path": "/things",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "array"},
            },
        }),
        state, _doc(),
    )
    # 263 not 400 — schemas are well-formed; runtime composes via
    # passthrough.
    assert resp.status_code == 263


def test_wrapped_form_with_malformed_schema_still_400s() -> None:
    """Schemas promoted from inside ``endpoint`` go through the same
    well-formedness gate as legacy top-level schemas."""
    from server.methods import handle_propose
    state = _make_state()
    resp = handle_propose(
        _req({
            "endpoint": {
                "method": "QUERY",
                "input_schema": "not an object",
            },
        }),
        state, _doc(),
    )
    assert resp.status_code == 400
    body = json.loads(resp.body_bytes)
    assert body["error"]["issue"] == "malformed-schema"
