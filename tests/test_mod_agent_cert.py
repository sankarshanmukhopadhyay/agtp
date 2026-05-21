"""
Tests for mod_agent_cert — the operational module that enforces
Agent-Cert extension constraints at dispatch time.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agtp.handlers import EndpointContext
from core import wire
from mod_agent_cert import AgentCertHook, install
from server.hooks import HookRegistry


def _ctx(
    *,
    agent_verified: bool = True,
    extensions: dict | None = None,
    authority_scope: list | None = None,
    zone_header: str | None = None,
    agent_id: str = "a" * 64,
) -> EndpointContext:
    headers = {}
    if zone_header is not None:
        headers["agtp-zone-id"] = zone_header
    return EndpointContext(
        input={},
        agent_id=agent_id,
        agent_verified=agent_verified,
        agent_cert_extensions=extensions or {},
        authority_scope=authority_scope or [],
        headers=headers,
    )


def _body(resp: wire.AGTPResponse) -> dict:
    return json.loads(resp.body_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Pass-through paths.
# ---------------------------------------------------------------------------


def test_no_verified_cert_is_passthrough() -> None:
    """No mTLS → the hook does nothing. The daemon's regular gates
    still run, but mod_agent_cert is a no-op for header-only identity."""
    hook = AgentCertHook()
    ctx = _ctx(agent_verified=False)
    assert hook.before_dispatch(None, ctx, None) is None


def test_no_extensions_is_passthrough() -> None:
    """A verified transport-only cert (no AGTP extensions) means there's
    nothing for this hook to enforce."""
    hook = AgentCertHook()
    ctx = _ctx(agent_verified=True, extensions={})
    assert hook.before_dispatch(None, ctx, None) is None


def test_authority_scope_within_commitment_passes() -> None:
    """Claims that are subset of the cert's commitment pass through."""
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"authority_scopes": ["bookings:write", "ledger:read"]},
        authority_scope=["bookings:write"],
    )
    assert hook.before_dispatch(None, ctx, None) is None


def test_empty_authority_scope_passes() -> None:
    """No claimed scopes = no enforcement to do."""
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"authority_scopes": ["bookings:write"]},
        authority_scope=[],
    )
    assert hook.before_dispatch(None, ctx, None) is None


# ---------------------------------------------------------------------------
# 455 Scope Violation.
# ---------------------------------------------------------------------------


def test_scope_outside_commitment_returns_455() -> None:
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"authority_scopes": ["bookings:write"]},
        authority_scope=["bookings:write", "admin:*"],
    )
    resp = hook.before_dispatch(None, ctx, None)
    assert resp is not None
    assert isinstance(resp, wire.AGTPResponse)
    assert resp.status_code == 455
    body = _body(resp)
    assert body["error"]["code"] == "scope-outside-commitment"
    assert body["error"]["outside_commitment"] == ["admin:*"]
    assert body["error"]["committed"] == ["bookings:write"]


def test_empty_commitment_with_any_claim_returns_455() -> None:
    """A cert that commits to no scopes refuses every Authority-Scope
    claim. Important: this distinguishes "no extension" (passthrough)
    from "explicit empty commitment" (refuse all claims)."""
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"authority_scopes": []},
        authority_scope=["any:scope"],
    )
    resp = hook.before_dispatch(None, ctx, None)
    assert resp is not None
    assert resp.status_code == 455


# ---------------------------------------------------------------------------
# 457 Zone Violation.
# ---------------------------------------------------------------------------


def test_zone_mismatch_returns_457() -> None:
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"governance_zone": "zone:finance"},
        zone_header="zone:engineering",
    )
    resp = hook.before_dispatch(None, ctx, None)
    assert resp is not None
    assert isinstance(resp, wire.AGTPResponse)
    assert resp.status_code == 457
    body = _body(resp)
    assert body["error"]["target_zone"] == "zone:finance"
    assert body["error"]["request_zone"] == "zone:engineering"


def test_zone_match_passes() -> None:
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"governance_zone": "zone:finance"},
        zone_header="zone:finance",
    )
    assert hook.before_dispatch(None, ctx, None) is None


def test_zone_absent_in_request_passes() -> None:
    """Requests without AGTP-Zone-ID don't trip the cert's zone pin
    — zone declaration is opt-in at the request layer."""
    hook = AgentCertHook()
    ctx = _ctx(
        extensions={"governance_zone": "zone:finance"},
        zone_header=None,
    )
    assert hook.before_dispatch(None, ctx, None) is None


# ---------------------------------------------------------------------------
# install() registration.
# ---------------------------------------------------------------------------


def test_install_registers_one_hook() -> None:
    class FakeState:
        def __init__(self) -> None:
            self.hook_registry = HookRegistry()
    state = FakeState()
    install(state)
    assert state.hook_registry.count() == 1
    assert isinstance(state.hook_registry.all()[0], AgentCertHook)
