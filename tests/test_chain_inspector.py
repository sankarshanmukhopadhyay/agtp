"""
Tests for tools.chain_inspector — the walker logic and the HTTP
front-end.

The walker drives invoke_method against a real-but-mocked AGTP
endpoint by patching ``invoke_method`` to return canned INSPECT
responses. That keeps the test from needing a live AGTP server but
exercises the full chain-walking control flow.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.core_client import FetchResult
from server.signing import SigningService
from tools.chain_inspector.walker import ChainStep, walk_chain


def _signing_service() -> SigningService:
    return SigningService(private_key=Ed25519PrivateKey.generate())


def _build_chain_record(
    svc: SigningService,
    *,
    agent_id: str,
    previous_audit_id: str = "",
    status: int = 200,
) -> Dict[str, Any]:
    """Build a signed Attribution-Record and return the dict shape
    INSPECT returns."""
    rec = svc.build_attribution_record(
        agent_id=agent_id,
        server_id="t.local",
        issued_at="2026-05-21T10:00:00Z",
        status=status,
        previous_audit_id=previous_audit_id,
    )
    return {
        "method": "INSPECT",
        "target": "audit",
        "audit_id": rec.audit_id,
        "jws": rec.jws,
        "header": {"alg": "EdDSA", "kid": svc.key_id, "typ": "JWT"},
        "payload": rec.payload,
        "issued_at": "2026-05-21T10:00:00Z",
    }


def _mock_invoke(records: Dict[str, Dict[str, Any]]):
    """Build a side_effect for invoke_method that looks up requests
    against the records dict keyed by audit_id."""
    def side_effect(uri, method, *, body=None, **kw):
        assert method == "INSPECT"
        aid = body["audit_id"]
        if aid not in records:
            return FetchResult(
                ok=True, status_code=404, status_text="Not Found",
                body_bytes=json.dumps({
                    "error": {"code": "audit-record-not-found"},
                }).encode("utf-8"),
            )
        return FetchResult(
            ok=True, status_code=200, status_text="OK",
            body_bytes=json.dumps(records[aid]).encode("utf-8"),
        )
    return side_effect


# ---------------------------------------------------------------------------
# walk_chain.
# ---------------------------------------------------------------------------


def test_walks_single_record_chain() -> None:
    svc = _signing_service()
    rec = _build_chain_record(svc, agent_id="a" * 64)
    records = {rec["audit_id"]: rec}

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=rec["audit_id"],
        )

    assert len(steps) == 1
    assert steps[0].audit_id == rec["audit_id"]
    assert steps[0].signed is True
    assert steps[0].verified is None  # no key supplied → not verified
    assert steps[0].previous_audit_id == ""


def test_walks_multi_record_chain_newest_first() -> None:
    svc = _signing_service()
    r1 = _build_chain_record(svc, agent_id="a" * 64)
    r2 = _build_chain_record(svc, agent_id="a" * 64, previous_audit_id=r1["audit_id"])
    r3 = _build_chain_record(svc, agent_id="a" * 64, previous_audit_id=r2["audit_id"])
    records = {r["audit_id"]: r for r in (r1, r2, r3)}

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=r3["audit_id"],
        )

    assert [s.audit_id for s in steps] == [r3["audit_id"], r2["audit_id"], r1["audit_id"]]


def test_walks_until_404() -> None:
    """When the next previous_audit_id isn't on this agent, the
    walker stops cleanly with a fetch_error step at the end."""
    svc = _signing_service()
    r1 = _build_chain_record(svc, agent_id="a" * 64, previous_audit_id="d" * 64)
    records = {r1["audit_id"]: r1}

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=r1["audit_id"],
        )

    assert len(steps) == 2
    assert steps[0].audit_id == r1["audit_id"]
    assert steps[1].audit_id == "d" * 64
    assert steps[1].fetch_error  # the 404 surfaces as fetch_error


def test_verifies_signature_when_key_supplied() -> None:
    svc = _signing_service()
    rec = _build_chain_record(svc, agent_id="a" * 64)
    records = {rec["audit_id"]: rec}

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=rec["audit_id"],
            issuer_public_key=svc.public_key,
        )

    assert steps[0].verified is True


def test_detects_invalid_signature() -> None:
    """A different key fails verification cleanly without crashing
    the walker."""
    svc = _signing_service()
    other_svc = _signing_service()
    rec = _build_chain_record(svc, agent_id="a" * 64)
    records = {rec["audit_id"]: rec}

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=rec["audit_id"],
            issuer_public_key=other_svc.public_key,
        )

    assert steps[0].verified is False


def test_cycle_detection() -> None:
    """A malformed/attacker-supplied chain that loops back must not
    hang the walker. Real signed chains can't cycle (the audit_id
    depends on the JWS which contains previous_audit_id), so cycles
    only arise when an attacker controls the INSPECT response —
    which the walker treats as adversarial input. Use alg:none
    records here so we can hand-craft a cyclic payload without
    fighting the signature."""
    svc = _signing_service()
    # Two records A and B where A.previous = B and B.previous = A.
    # Use unsigned (alg:none) so we can set previous_audit_id freely.
    a_aid = "a" * 64
    b_aid = "b" * 64
    rec_a = svc.build_unsigned_attribution_record(
        agent_id="agent",
        server_id="t.local",
        issued_at="t",
        status=200,
        previous_audit_id=b_aid,
    )
    rec_b = svc.build_unsigned_attribution_record(
        agent_id="agent",
        server_id="t.local",
        issued_at="t",
        status=200,
        previous_audit_id=a_aid,
    )
    # Override the records dict keys to use our fixed aids so the
    # cycle resolves cleanly; the walker reads previous_audit_id from
    # the payload, so what matters is the payload contents.
    records = {
        a_aid: {
            "jws": rec_a.jws, "header": {"alg": "none"},
            "payload": rec_a.payload, "audit_id": a_aid,
        },
        b_aid: {
            "jws": rec_b.jws, "header": {"alg": "none"},
            "payload": rec_b.payload, "audit_id": b_aid,
        },
    }

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=a_aid,
        )

    # Walker: A → B → (A again, in seen) → cycle step → break.
    assert len(steps) == 3
    assert steps[0].audit_id == a_aid
    assert steps[1].audit_id == b_aid
    assert "cycle" in steps[2].fetch_error.lower()


def test_max_steps_caps_runaway_chains() -> None:
    """An adversary-supplied huge chain stops at max_steps."""
    svc = _signing_service()
    # Build a long chain.
    records: Dict[str, Dict[str, Any]] = {}
    previous = ""
    last_aid = ""
    for _ in range(10):
        rec = _build_chain_record(
            svc, agent_id="a" * 64, previous_audit_id=previous,
        )
        records[rec["audit_id"]] = rec
        previous = rec["audit_id"]
        last_aid = rec["audit_id"]

    with mock.patch(
        "tools.chain_inspector.walker.invoke_method",
        side_effect=_mock_invoke(records),
    ):
        steps = walk_chain(
            agent_uri="agtp://lauren.example",
            start_audit_id=last_aid,
            max_steps=3,
        )

    assert len(steps) == 3
