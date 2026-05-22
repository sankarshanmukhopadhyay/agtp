"""
Tests for RCNS-1 — Endpoint tier formalization.

Covers:
  * core.endpoint_tiers — TIER_A_RESERVED_ENDPOINTS membership,
    classify_tier across all four outcomes (A / B / C / unregistered),
    tier_a_inventory merging reserved + registered builtins.
  * core.status — 461 / 464 wire codes + helpers + reason vocabulary.
  * DISCOVER / index entries carry tier="A".
  * server/builtins.py handlers attach the __agtp_builtin__ marker so
    the classifier picks them up as Tier A.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from core import status, wire
from core.endpoint_tiers import (
    BUILTIN_HANDLER_MARKER,
    TIER_APPLICATION,
    TIER_A_RESERVED_ENDPOINTS,
    TIER_NATIVE,
    TIER_RCNS,
    TIER_UNREGISTERED,
    classify_tier,
    tier_a_inventory,
)


# ---------------------------------------------------------------------------
# Reserved inventory.
# ---------------------------------------------------------------------------


def test_reserved_inventory_includes_discover_roots() -> None:
    """Every T4.1 reserved DISCOVER root is Tier A."""
    for path in ("/", "/methods", "/agents", "/tools", "/apis", "/genesis"):
        assert ("DISCOVER", path) in TIER_A_RESERVED_ENDPOINTS


def test_reserved_inventory_includes_proposals_poll() -> None:
    """The §7 async-PROPOSE poll surface is Tier A."""
    assert ("QUERY", "/proposals") in TIER_A_RESERVED_ENDPOINTS


def test_reserved_inventory_is_frozenset() -> None:
    """Mutability would let runtime code corrupt the protocol-reserved
    inventory. The constant must be a frozenset."""
    assert isinstance(TIER_A_RESERVED_ENDPOINTS, frozenset)


# ---------------------------------------------------------------------------
# classify_tier across the four outcomes.
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Minimal duck-typed registry for classifier tests."""

    def __init__(self) -> None:
        self._table: Dict[tuple, Any] = {}

    def register(self, method: str, path: str, handler: Any) -> None:
        self._table[(method.upper(), path)] = handler

    def lookup(self, method: str, path: str) -> Any:
        return self._table.get((method.upper(), path))

    def all_endpoints(self):
        for (m, p), h in self._table.items():
            # Mimic the production registry returning (spec, handler);
            # use a SimpleNamespace-style stand-in for the spec.
            class _S:
                pass
            s = _S()
            s.name = m
            s.path = p
            yield (s, h)


def _tier_a_handler() -> Any:
    """Closure carrying the builtin marker."""
    def h(_ctx):  # pragma: no cover — never invoked in these tests
        return None
    setattr(h, BUILTIN_HANDLER_MARKER, "test_builtin")
    return h


def _tier_b_handler() -> Any:
    """Closure without the builtin marker — plain Tier B."""
    def h(_ctx):  # pragma: no cover
        return None
    return h


def test_classify_reserved_pair_is_tier_a() -> None:
    """No registry needed — the reserved inventory alone resolves Tier A."""
    assert classify_tier("DISCOVER", "/agents") == TIER_NATIVE
    assert classify_tier("DISCOVER", "/") == TIER_NATIVE
    assert classify_tier("QUERY", "/proposals") == TIER_NATIVE


def test_classify_reserved_is_case_insensitive_on_method() -> None:
    """Lowercase method tokens normalize correctly."""
    assert classify_tier("discover", "/agents") == TIER_NATIVE


def test_classify_registered_builtin_is_tier_a() -> None:
    """A handler in the registry with the builtin marker → Tier A."""
    reg = _FakeRegistry()
    reg.register("RECONCILE", "/accounts", _tier_a_handler())
    assert classify_tier("RECONCILE", "/accounts", registry=reg) == TIER_NATIVE


def test_classify_registered_without_marker_is_tier_b() -> None:
    """A handler in the registry without the builtin marker → Tier B."""
    reg = _FakeRegistry()
    reg.register("DISCOVER", "/products", _tier_b_handler())
    assert classify_tier("DISCOVER", "/products", registry=reg) == TIER_APPLICATION


def test_classify_unregistered_is_unregistered() -> None:
    """Neither in reserved inventory nor in the registry → unregistered."""
    reg = _FakeRegistry()
    assert classify_tier("FETCH", "/unknown", registry=reg) == TIER_UNREGISTERED


def test_classify_unregistered_without_registry_is_unregistered() -> None:
    """Caller may omit the registry entirely; classifier still works for
    reserved pairs and returns ``unregistered`` for anything else."""
    assert classify_tier("FETCH", "/products") == TIER_UNREGISTERED


def test_classify_tier_c_when_synthesis_resolves() -> None:
    """RCNS-3 hook: a non-None synthesis_lookup that resolves the pair
    returns Tier C. Pre-RCNS-3 the hook is always None and step 4 is a
    no-op.

    Uses /reports as the negotiated path — /patterns and /contracts
    became Tier A in RCNS-4 so picking one of those would hit the
    reserved-inventory short-circuit before the synthesis lookup."""

    class _Synth:
        def resolve(self, method: str, path: str):
            if (method, path) == ("RECONCILE", "/reports"):
                return {"synthesis_id": "syn-fake"}
            return None

    reg = _FakeRegistry()
    assert classify_tier(
        "RECONCILE", "/reports",
        registry=reg, synthesis_lookup=_Synth(),
    ) == TIER_RCNS
    assert classify_tier(
        "RECONCILE", "/other",
        registry=reg, synthesis_lookup=_Synth(),
    ) == TIER_UNREGISTERED


def test_classify_reserved_wins_over_synthesis() -> None:
    """Reserved-inventory hits short-circuit before the synthesis lookup
    is consulted; a hypothetical synthesis at /agents must not get
    classified as Tier C."""
    class _Synth:
        def resolve(self, method, path):
            return {"synthesis_id": "fake"}

    assert classify_tier(
        "DISCOVER", "/agents",
        synthesis_lookup=_Synth(),
    ) == TIER_NATIVE


# ---------------------------------------------------------------------------
# tier_a_inventory merges reserved + registered builtins.
# ---------------------------------------------------------------------------


def test_inventory_returns_reserved_when_no_registry() -> None:
    inv = tier_a_inventory()
    for entry in TIER_A_RESERVED_ENDPOINTS:
        assert entry in inv


def test_inventory_includes_registered_builtins() -> None:
    """A registry entry with the builtin marker appears in the
    inventory alongside reserved pairs."""
    reg = _FakeRegistry()
    reg.register("FETCH", "/heartbeat", _tier_a_handler())
    inv = tier_a_inventory(reg)
    assert ("FETCH", "/heartbeat") in inv
    # Reserved pairs are still present.
    assert ("DISCOVER", "/agents") in inv


def test_inventory_excludes_tier_b_registrations() -> None:
    """Plain registry entries (no builtin marker) stay out of the
    inventory even though they're in the registry."""
    reg = _FakeRegistry()
    reg.register("DISCOVER", "/products", _tier_b_handler())
    inv = tier_a_inventory(reg)
    assert ("DISCOVER", "/products") not in inv


def test_inventory_is_sorted() -> None:
    inv = tier_a_inventory()
    assert inv == sorted(inv)


# ---------------------------------------------------------------------------
# Status code reservations (461 + 464).
# ---------------------------------------------------------------------------


def test_status_codes_reserved() -> None:
    assert status.RCNS_CONTRACT_AVAILABLE[0] == 461
    assert status.RCNS_NO_CONTRACT[0] == 464


def test_status_code_text_is_human_readable() -> None:
    assert status.RCNS_CONTRACT_AVAILABLE[1] == "RCNS Contract Available"
    assert status.RCNS_NO_CONTRACT[1] == "RCNS No Contract"


def test_rcns_refusal_reason_vocabulary() -> None:
    expected = {
        "rcns-disabled",
        "trust-tier-insufficient",
        "composition-impossible",
        "synthesis-error",
        "contract-not-yours",
        "contract-revoked",
    }
    assert status.ALL_RCNS_REFUSAL_REASONS == expected


def test_rcns_contract_available_helper() -> None:
    resp = status.rcns_contract_available(
        contract={
            "method": "DISCOVER",
            "path": "/patterns",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "array"},
            "plan_summary": "passthrough to embedded DISCOVER",
        },
        proposed_synthesis_id="syn-abc",
        expires_at="2026-05-23T00:00:00Z",
    )
    assert resp.status_code == 461
    body = json.loads(resp.body_bytes)
    assert body["proposed_synthesis_id"] == "syn-abc"
    assert body["contract"]["method"] == "DISCOVER"
    assert body["contract"]["path"] == "/patterns"
    assert body["expires_at"] == "2026-05-23T00:00:00Z"


def test_rcns_no_contract_helper_with_valid_reason() -> None:
    resp = status.rcns_no_contract(
        reason="composition-impossible",
        explanation="No composition policy returned a plan.",
        method="DISCOVER",
        path="/patterns",
    )
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "rcns-no-contract"
    assert body["error"]["reason"] == "composition-impossible"
    assert body["error"]["method"] == "DISCOVER"
    assert body["error"]["path"] == "/patterns"


@pytest.mark.parametrize("reason", sorted({
    "rcns-disabled",
    "trust-tier-insufficient",
    "composition-impossible",
    "synthesis-error",
    "contract-not-yours",
    "contract-revoked",
}))
def test_rcns_no_contract_accepts_every_documented_reason(reason: str) -> None:
    resp = status.rcns_no_contract(reason=reason, explanation="…")
    assert resp.status_code == 464
    assert json.loads(resp.body_bytes)["error"]["reason"] == reason


def test_rcns_no_contract_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError) as exc_info:
        status.rcns_no_contract(reason="invented", explanation="…")
    assert "expected one of" in str(exc_info.value)


def test_rcns_helpers_exported() -> None:
    """The new helpers and constants are in __all__ so callers can
    import them at the package surface."""
    assert "rcns_contract_available" in status.__all__
    assert "rcns_no_contract" in status.__all__
    assert "RCNS_CONTRACT_AVAILABLE" in status.__all__
    assert "RCNS_NO_CONTRACT" in status.__all__
    assert "ALL_RCNS_REFUSAL_REASONS" in status.__all__


# ---------------------------------------------------------------------------
# DISCOVER / index annotates entries with tier="A".
# ---------------------------------------------------------------------------


def test_discover_index_entries_carry_tier() -> None:
    """The bare-DISCOVER / index annotates each reserved root with
    tier="A" so callers can classify endpoints without hard-coding the
    Tier A inventory."""
    from core.identity import AgentDocument, RequiresDeclaration
    from server.methods import handle_discover

    doc = AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(methods=["DISCOVER"]),
        scopes_accepted=[], issued_at="now", issuer="self",
    )

    class _State:
        def list_ids(self): return [doc.agent_id]
        def lookup(self, aid): return doc if aid == doc.agent_id else None
        def lookup_genesis(self, _aid): return None

    raw = json.dumps({}).encode("utf-8")
    req = wire.AGTPRequest(
        method="DISCOVER", path="/",
        headers={"Agent-ID": doc.agent_id, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )
    resp = handle_discover(req, _State(), doc)
    body = json.loads(resp.body_bytes)
    assert body["target"] == "index"
    for entry in body["endpoints"]:
        assert entry["tier"] == "A", (
            f"entry {entry} missing tier='A' annotation"
        )


# ---------------------------------------------------------------------------
# server/builtins.py handlers attach the __agtp_builtin__ marker.
# ---------------------------------------------------------------------------


def test_builtin_handlers_attach_marker() -> None:
    """The two built-ins :mod:`server.builtins` ships must attach the
    BUILTIN_HANDLER_MARKER so :func:`classify_tier` returns 'A' when
    the registry resolves them."""
    from server import builtins as bi

    class _FakeReg:
        def render_manifest_section(self):
            return []

    h1 = bi.discover_methods(_FakeReg())
    assert hasattr(h1, BUILTIN_HANDLER_MARKER)
    assert getattr(h1, BUILTIN_HANDLER_MARKER) == "discover_methods"

    class _FakeStore:
        def lookup(self, _pid): return None
        def evaluation_started_at(self, _pid): return ""
        def max_evaluation_duration_str(self): return ""

    h2 = bi.query_proposal(_FakeStore())
    assert hasattr(h2, BUILTIN_HANDLER_MARKER)
    assert getattr(h2, BUILTIN_HANDLER_MARKER) == "query_proposal"
