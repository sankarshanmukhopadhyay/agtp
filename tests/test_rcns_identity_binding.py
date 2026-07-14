"""
Tests for ``[policies.rcns].require_verified_identity`` — the
identity-binding guard added alongside the F6 governance finding.

Background: RCNS's documented per-agent rate limit and idempotency
cache (docs/rcns.md, "Abuse mitigations") are keyed on
``agent_doc.agent_id``. Under the default ``[mtls].mode =
"disabled"`` posture, Agent-ID is a plain client-supplied header, so
an attacker rotating the header on every request bypasses the
per-agent ceiling entirely even though the documentation describes it
as binding. This flag lets an operator require a verified mTLS cert
before RCNS will negotiate at all, closing that specific bypass for
servers that also run with mTLS optional/required.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from server.config import (
    AuditConfig, RcnsConfig, ServerConfig, ServerInfo, ServerPolicy,
    SigningConfig, SynthesisConfig,
)
from server.mtls import VerifiedCert
from server.rcns_gate import reset_state_for_tests, try_rcns
from server.synthesis.runtime import SynthesisRuntime


@pytest.fixture(autouse=True)
def _reset_gate_state():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


def _config(*, require_verified_identity: bool) -> ServerConfig:
    return ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        policy=ServerPolicy(synthesis_enabled=True),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(
            enabled=True,
            min_trust_tier=3,
            max_negotiations_per_minute=100,
            require_verified_identity=require_verified_identity,
        ),
        audit=AuditConfig(),
        signing=SigningConfig(),
    )


def _doc(*, agent_id: str = "a" * 64) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["DISCOVER", "QUERY"], scopes=["rcns:negotiate"],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
        trust_tier=1,
    )


def _state(config: ServerConfig) -> Any:
    state = MagicMock()
    state.config = config
    state.synthesis_runtime = SynthesisRuntime()
    state.endpoint_registry = None
    return state


def _req(*, verified: bool, headers: Optional[Dict[str, str]] = None) -> wire.AGTPRequest:
    base_headers: Dict[str, str] = {
        "Agent-ID": "a" * 64, "Content-Length": "2", "Allow-RCNS": "true",
    }
    if headers:
        base_headers.update(headers)
    request = wire.AGTPRequest(
        method="QUERY", path="/things",
        headers=base_headers, body_bytes=b"{}",
    )
    if verified:
        now = datetime.now(timezone.utc)
        request.verified_cert = VerifiedCert(
            agent_id="a" * 64,
            fingerprint="f" * 64,
            not_before=now,
            not_after=now,
            subject_common_name="lauren",
        )
    else:
        request.verified_cert = None
    return request


def test_programmatic_dataclass_default_is_compatibility_false() -> None:
    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        rcns=RcnsConfig(enabled=True, min_trust_tier=3),
    )
    assert cfg.rcns.require_verified_identity is False


def test_disabled_by_default_unverified_request_still_negotiates() -> None:
    """Baseline: with the flag off (the default), an unverified
    request negotiates normally — no behavior change for existing
    deployments that haven't opted in."""
    cfg = _config(require_verified_identity=False)
    resp = try_rcns(
        _req(verified=False), _state(cfg), _doc(),
        method="QUERY", path="/things",
    )
    assert resp is not None
    assert resp.status_code != 464


def test_enabled_refuses_unverified_request_with_464() -> None:
    cfg = _config(require_verified_identity=True)
    resp = try_rcns(
        _req(verified=False), _state(cfg), _doc(),
        method="QUERY", path="/things",
    )
    assert resp is not None
    assert resp.status_code == 464
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "identity-unverified"
    assert "RCNS-Attempt-Id" in resp.headers


def test_enabled_admits_verified_request() -> None:
    cfg = _config(require_verified_identity=True)
    resp = try_rcns(
        _req(verified=True), _state(cfg), _doc(),
        method="QUERY", path="/things",
    )
    assert resp is not None
    assert resp.status_code != 464


def test_enabled_refusal_precedes_scope_and_trust_tier_checks() -> None:
    """The identity guard runs before Locks 3/4 — an unverified,
    scope-lacking agent gets 'identity-unverified', not
    'rcns-scope-required', so the caller learns the more
    fundamental problem first."""
    cfg = _config(require_verified_identity=True)
    doc = AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(methods=["QUERY"], scopes=[]),
        scopes_accepted=[], issued_at="now", issuer="self",
        trust_tier=1,
    )
    resp = try_rcns(
        _req(verified=False), _state(cfg), doc,
        method="QUERY", path="/things",
    )
    assert resp is not None
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "identity-unverified"


if __name__ == "__main__":
    import unittest
    unittest.main()
