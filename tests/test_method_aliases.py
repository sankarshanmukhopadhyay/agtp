"""
Tests for RCNS-5 — METHOD aliases.

Covers MethodsPolicy.aliases, the default legacy seed, TOML
loading semantics (including the empty-table opt-out), the
dispatcher gate's alias resolution, the requested_method audit
field, and the manifest surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from server import config as cfg_module
from server.config import (
    MethodsPolicy, _legacy_alias_seed,
    default_methods_policy, methods_policy_from_table,
)


# ---------------------------------------------------------------------------
# Default seed + resolve_alias.
# ---------------------------------------------------------------------------


def test_legacy_alias_seed_contains_five_http_verbs() -> None:
    seed = _legacy_alias_seed()
    assert seed == {
        "GET":    "FETCH",
        "POST":   "CREATE",
        "PUT":    "REPLACE",
        "DELETE": "REMOVE",
        "PATCH":  "MODIFY",
    }


def test_default_methods_policy_seeds_legacy_aliases() -> None:
    """A freshly-booted server (no config file) admits the five
    legacy HTTP verbs by default via the alias seed."""
    policy = default_methods_policy()
    assert policy.aliases == _legacy_alias_seed()


def test_resolve_alias_returns_target_for_seeded_verb() -> None:
    policy = default_methods_policy()
    assert policy.resolve_alias("GET") == "FETCH"
    assert policy.resolve_alias("get") == "FETCH"  # case-insensitive
    assert policy.resolve_alias("POST") == "CREATE"


def test_resolve_alias_returns_none_for_non_aliased_verb() -> None:
    policy = default_methods_policy()
    assert policy.resolve_alias("DISCOVER") is None
    assert policy.resolve_alias("FROBNICATE") is None


def test_resolve_alias_returns_none_for_identity_mapping() -> None:
    """A no-op alias (FOO -> FOO) is treated as "no alias" so the
    dispatcher doesn't spin its wheels rewriting to the same verb."""
    policy = MethodsPolicy(aliases={"DESCRIBE": "DESCRIBE"})
    assert policy.resolve_alias("DESCRIBE") is None


def test_resolve_alias_does_not_chain() -> None:
    """A -> B and B -> C still resolves A -> B (single-hop). Keeps
    operator-declared alias loops from misbehaving and matches the
    documented contract."""
    policy = MethodsPolicy(aliases={"WHOAMI": "DESCRIBE", "DESCRIBE": "INSPECT"})
    assert policy.resolve_alias("WHOAMI") == "DESCRIBE"
    assert policy.resolve_alias("DESCRIBE") == "INSPECT"


# ---------------------------------------------------------------------------
# TOML loading.
# ---------------------------------------------------------------------------


def test_toml_aliases_block_overrides_seed(tmp_path: Path) -> None:
    """Declaring [policies.methods.aliases] replaces the default seed
    entirely — operators get a clean slate when they declare aliases
    explicitly."""
    f = tmp_path / "s.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "o"
contact = "c"

[policies.methods.aliases]
WHOAMI  = "DESCRIBE"
LIST    = "DISCOVER"
""",
        encoding="utf-8",
    )
    cfg = cfg_module.load(f)
    # Only the operator-declared entries are present; legacy seed is
    # cleared because the block was explicit.
    assert cfg.policy.methods.aliases == {
        "WHOAMI": "DESCRIBE",
        "LIST":   "DISCOVER",
    }


def test_toml_empty_aliases_block_disables_legacy_seed(tmp_path: Path) -> None:
    """An explicitly-empty aliases table wipes the seed — operators
    who want strict 459 on legacy verbs declare this."""
    f = tmp_path / "s.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "o"
contact = "c"

[policies.methods.aliases]
""",
        encoding="utf-8",
    )
    cfg = cfg_module.load(f)
    assert cfg.policy.methods.aliases == {}


def test_toml_missing_aliases_block_keeps_legacy_seed(tmp_path: Path) -> None:
    """A config that doesn't mention aliases at all loads the legacy
    seed (the pre-explicit-block default)."""
    f = tmp_path / "s.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "o"
contact = "c"
""",
        encoding="utf-8",
    )
    cfg = cfg_module.load(f)
    assert cfg.policy.methods.aliases == _legacy_alias_seed()


def test_toml_alias_targeting_unknown_verb_warns_and_skips(
    tmp_path: Path,
) -> None:
    """An alias pointing at a verb that's not in the AGTP catalog
    (and not legacy) is skipped with a warning rather than blocking
    boot."""
    f = tmp_path / "s.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "o"
contact = "c"

[policies.methods.aliases]
WHOAMI  = "DESCRIBE"
BOGUS   = "BLAHBLAH"
""",
        encoding="utf-8",
    )
    with pytest.warns(Warning):
        cfg = cfg_module.load(f)
    # WHOAMI loaded; BOGUS dropped.
    assert "WHOAMI" in cfg.policy.methods.aliases
    assert "BOGUS" not in cfg.policy.methods.aliases


# ---------------------------------------------------------------------------
# Manifest exposure.
# ---------------------------------------------------------------------------


def test_aliases_surface_on_manifest() -> None:
    policy = default_methods_policy()
    wire_form = policy.to_wire()
    assert "aliases" in wire_form
    # Sorted for stable manifest output.
    assert list(wire_form["aliases"]) == sorted(wire_form["aliases"])
    assert wire_form["aliases"]["GET"] == "FETCH"


def test_empty_aliases_omits_from_manifest() -> None:
    """When the alias table is empty, the manifest omits the key
    rather than emitting an empty dict — keeps the manifest tidy."""
    policy = MethodsPolicy(allow_all=True, aliases={})
    assert "aliases" not in policy.to_wire()


# ---------------------------------------------------------------------------
# Dispatcher gate.
# ---------------------------------------------------------------------------


def _doc(
    methods: list | None = None,
    scopes: list | None = None,
) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=methods or ["DISCOVER", "FETCH", "DESCRIBE"],
            scopes=scopes or [],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


def _state(policy: MethodsPolicy) -> object:
    from unittest.mock import MagicMock
    state = MagicMock()
    state.methods_policy = policy
    state.config = None
    state.synthesis_runtime = None
    state.endpoint_registry = None
    return state


def _req(method: str, path: str = "/") -> wire.AGTPRequest:
    raw = json.dumps({}).encode("utf-8")
    return wire.AGTPRequest(
        method=method, path=path,
        headers={"Agent-ID": "a" * 64, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


def test_dispatcher_rewrites_alias_before_catalog_check() -> None:
    """A wire-level GET on the default-policy server resolves to FETCH
    in the dispatcher, passes the catalog check, and reaches the
    handler. Without the alias the catalog gate would fire 459."""
    from server.methods import dispatch
    policy = default_methods_policy()
    state = _state(policy)
    doc = _doc()
    resp = dispatch(_req("GET", "/whatever"), state, doc, config=None)
    # GET → FETCH; FETCH is in the catalog (so no 459) but not
    # registered as a method-only handler on this fixture, so the
    # method-only fallback returns 405 ``method-not-implemented``.
    assert resp.status_code == 405
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "method-not-implemented"
    # The dispatcher's response speaks the resolved verb.
    assert body["error"]["method"] == "FETCH"


def test_dispatcher_aliased_from_stashed_on_request() -> None:
    """When an alias fires, the original verb is stashed on the
    request for the audit chain to pick up as ``requested_method``."""
    from server.methods import dispatch
    policy = default_methods_policy()
    state = _state(policy)
    doc = _doc()
    req = _req("GET", "/whatever")
    dispatch(req, state, doc, config=None)
    assert getattr(req, "_aliased_from", None) == "GET"
    # Request method itself is rewritten in place so handlers
    # downstream of the gate see the resolved verb.
    assert req.method == "FETCH"


def test_dispatcher_non_aliased_verb_does_not_stash() -> None:
    """An ordinary AGTP verb (no alias) leaves the request unchanged."""
    from server.methods import dispatch
    policy = default_methods_policy()
    state = _state(policy)
    doc = _doc()
    req = _req("DISCOVER", "/")
    dispatch(req, state, doc, config=None)
    assert getattr(req, "_aliased_from", None) is None
    assert req.method == "DISCOVER"


def test_dispatcher_unknown_verb_with_no_alias_returns_459() -> None:
    """Verbs that are neither in the catalog nor in the alias table
    still surface the helpful 459 with close-match suggestions."""
    from server.methods import dispatch
    policy = default_methods_policy()
    state = _state(policy)
    doc = _doc()
    resp = dispatch(_req("FROBNICATE"), state, doc, config=None)
    assert resp.status_code == 459


def test_dispatcher_empty_alias_table_refuses_get() -> None:
    """An operator who declares aliases = {} explicitly opts out of
    the legacy seed. Wire-level GET now returns 459 like pre-RCNS-5."""
    from server.methods import dispatch
    policy = MethodsPolicy(allow_all=True, aliases={})
    state = _state(policy)
    doc = _doc()
    resp = dispatch(_req("GET"), state, doc, config=None)
    assert resp.status_code == 459
