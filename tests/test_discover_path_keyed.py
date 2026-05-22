"""
Tests for T4.1 — path-keyed DISCOVER dispatch.

Covers:
  * DISCOVER /methods, /agents, /tools, /apis, /genesis → same
    behavior as the legacy body-target form.
  * DISCOVER / (bare) → endpoint directory.
  * DISCOVER / with body target=... → legacy form, accepted with
    a one-shot stderr deprecation warning.
  * DISCOVER /methods with body target=agents → 400 conflict.
  * DISCOVER /products (custom path) → 460 (not registered).
  * path-grammar reserved-prefix rule (`validate_discover_path`).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from core import wire
from core.path_grammar import (
    DISCOVER_RESERVED_ROOTS,
    PathGrammarError,
    validate_discover_path,
)
from core.identity import AgentDocument, RequiresDeclaration


def _doc(agent_id: str = "a" * 64) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=["coffee"],
        requires=RequiresDeclaration(methods=["DISCOVER", "DESCRIBE"]),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


class _State:
    """Minimal duck-typed ServerState for the handler."""

    def __init__(self, doc: AgentDocument) -> None:
        self._doc = doc

    def list_ids(self):
        return [self._doc.agent_id]

    def lookup(self, aid):
        return self._doc if aid == self._doc.agent_id else None

    def lookup_genesis(self, aid):
        return None  # transport-only for these tests


def _req(path: str = "/", body: dict | None = None) -> wire.AGTPRequest:
    raw = json.dumps(body or {}).encode("utf-8")
    return wire.AGTPRequest(
        method="DISCOVER", path=path,
        headers={"Agent-ID": "a" * 64, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


# ---------------------------------------------------------------------------
# Path-keyed reserved roots.
# ---------------------------------------------------------------------------


def test_discover_methods_via_path() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/methods"), _State(doc), doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "methods"
    assert "embedded" in body
    assert "custom" in body


def test_discover_agents_via_path() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/agents"), _State(doc), doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "agents"
    assert body["item_count"] == 1


def test_discover_tools_via_path() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/tools"), _State(doc), doc)
    assert resp.status_code == 200
    assert json.loads(resp.body_bytes)["target"] == "tools"


def test_discover_apis_via_path() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/apis"), _State(doc), doc)
    assert resp.status_code == 200
    assert json.loads(resp.body_bytes)["target"] == "apis"


def test_discover_genesis_via_path_returns_404_without_genesis() -> None:
    """A transport-only agent has no Genesis loaded; the handler
    surfaces that as 404 ``genesis-not-found``."""
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/genesis"), _State(doc), doc)
    assert resp.status_code == 404
    assert b"genesis-not-found" in resp.body_bytes


# ---------------------------------------------------------------------------
# Bare path "/" → endpoint directory.
# ---------------------------------------------------------------------------


def test_discover_bare_returns_endpoint_directory() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/"), _State(doc), doc)
    assert resp.status_code == 200
    body = json.loads(resp.body_bytes)
    assert body["target"] == "index"
    paths = {ep["path"] for ep in body["endpoints"]}
    assert paths == {"/methods", "/agents", "/tools", "/apis", "/genesis"}
    assert body["endpoint_count"] == 5


# ---------------------------------------------------------------------------
# Legacy body-target shim.
# ---------------------------------------------------------------------------


def test_legacy_body_target_form_still_works() -> None:
    from server.methods import handle_discover
    import server.methods as mod
    # Reset the one-shot deprecation guard so we can observe the warning.
    mod._DISCOVER_LEGACY_WARNED.clear()
    doc = _doc()
    captured = io.StringIO()
    with mock.patch.object(sys, "stderr", captured):
        resp = handle_discover(
            _req("/", {"target": "methods"}), _State(doc), doc,
        )
    assert resp.status_code == 200
    assert json.loads(resp.body_bytes)["target"] == "methods"
    assert "legacy body-`target`" in captured.getvalue()


def test_legacy_warning_is_one_shot_per_agent() -> None:
    from server.methods import handle_discover
    import server.methods as mod
    mod._DISCOVER_LEGACY_WARNED.clear()
    doc = _doc()
    captured = io.StringIO()
    with mock.patch.object(sys, "stderr", captured):
        handle_discover(_req("/", {"target": "methods"}), _State(doc), doc)
        handle_discover(_req("/", {"target": "agents"}), _State(doc), doc)
        handle_discover(_req("/", {"target": "tools"}), _State(doc), doc)
    # Exactly one warning line for the agent, no matter how many calls.
    assert captured.getvalue().count("legacy body-`target`") == 1


# ---------------------------------------------------------------------------
# Conflict + 460 path.
# ---------------------------------------------------------------------------


def test_path_and_body_target_conflict_returns_400() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(
        _req("/methods", {"target": "agents"}), _State(doc), doc,
    )
    assert resp.status_code == 400
    assert b"discover-target-conflict" in resp.body_bytes


def test_path_and_body_target_match_passes() -> None:
    """When the body target matches the path target, no conflict — the
    caller is just being explicit."""
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(
        _req("/methods", {"target": "methods"}), _State(doc), doc,
    )
    assert resp.status_code == 200


def test_unknown_path_returns_460() -> None:
    from server.methods import handle_discover
    doc = _doc()
    resp = handle_discover(_req("/products"), _State(doc), doc)
    assert resp.status_code == 460
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "discover-unknown-path"
    assert "/methods" in str(body)


# ---------------------------------------------------------------------------
# Path-grammar reserved-prefix rule.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/products",
    "/projects",
    "/catalog",
    "/customers/active",
    "/v2/inventory",
    "/{tenant_id}/products",  # parameterized first segment is exempt
])
def test_custom_discover_paths_accepted(path: str) -> None:
    """Custom paths whose first segment doesn't shadow a reserved
    root pass validation."""
    validate_discover_path(path)  # raises on failure


@pytest.mark.parametrize("path", [
    "/agents-products",
    "/methodsv2",
    "/genesis-archive",
    "/toolset",  # starts with "tools"
    "/apispec",
])
def test_reserved_prefix_paths_refused(path: str) -> None:
    """Anything starting with a reserved-root name is refused —
    even ``/toolset`` (shadows "tools") and ``/methodsv2``."""
    with pytest.raises(PathGrammarError) as exc_info:
        validate_discover_path(path)
    assert exc_info.value.code == "discover-reserved-prefix"


@pytest.mark.parametrize("path", [
    "/agents",
    "/methods",
    "/tools",
    "/apis",
    "/genesis",
])
def test_exact_reserved_paths_accepted_by_grammar(path: str) -> None:
    """Exact-match reserved paths pass the path-grammar — they're
    the protocol routes the daemon handles directly."""
    validate_discover_path(path)  # no raise


def test_path_grammar_invariants_still_apply() -> None:
    """validate_discover_path runs validate_path first, so structural
    violations still surface."""
    with pytest.raises(PathGrammarError):
        validate_discover_path("no-leading-slash")
    with pytest.raises(PathGrammarError):
        validate_discover_path("/trailing/slash/")
    with pytest.raises(PathGrammarError):
        validate_discover_path("/fetch/orders")  # verb-in-path
