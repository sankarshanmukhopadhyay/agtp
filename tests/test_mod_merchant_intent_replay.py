"""
Tests for MerchantHook's Intent Assertion replay guard
(``jti_store`` / ``Intent-Assertion-Jti`` header) and
``mod_merchant.replay_store``.

Background: mod_merchant verified *who the counterparty is*
(Merchant-ID, manifest fingerprint) but never *whether this specific
Intent Assertion was already spent*. A captured or logged assertion
was fully replayable against the same merchant within its TTL. This
suite covers the reference in-memory store directly and the hook's
wiring of it.
"""

from __future__ import annotations

import json
import time

from agtp.handlers import EndpointContext
from core.identity import AgentDocument, RequiresDeclaration
from mod_merchant import MerchantHook
from mod_merchant.replay_store import InMemorySeenJtiStore


def _bare_doc(**overrides) -> AgentDocument:
    base = dict(
        agtp_version="1.0",
        agent_id="a" * 64,
        name="acme-merchant",
        principal="Acme Inc",
        principal_id="acme.inc",
        description="",
        status="active",
        skills=[],
        requires=RequiresDeclaration(methods=["PURCHASE", "QUOTE"]),
        scopes_accepted=[],
        issued_at="2026-05-21T00:00:00Z",
        issuer="self",
        role="merchant",
        trust_tier=1,
        verification_path="dns-anchored",
    )
    base.update(overrides)
    return AgentDocument(**base)


def _ctx_for(*, method="PURCHASE", agent_id="a" * 64, headers=None) -> EndpointContext:
    return EndpointContext(
        input={}, agent_id=agent_id, method=method, headers=headers or {},
    )


def _state_for(doc):
    class _State:
        def lookup(self, aid):
            return doc if aid == doc.agent_id else None
    return _State()


# ---------------------------------------------------------------------------
# InMemorySeenJtiStore, standalone.
# ---------------------------------------------------------------------------


def test_store_first_presentation_not_seen() -> None:
    store = InMemorySeenJtiStore()
    assert store.seen("jti-1") is False


def test_store_check_and_record_first_call_returns_false() -> None:
    store = InMemorySeenJtiStore()
    assert store.check_and_record("jti-1", ttl_seconds=60) is False


def test_store_check_and_record_second_call_returns_true() -> None:
    store = InMemorySeenJtiStore()
    store.check_and_record("jti-1", ttl_seconds=60)
    assert store.check_and_record("jti-1", ttl_seconds=60) is True


def test_store_expires_entries_after_ttl() -> None:
    store = InMemorySeenJtiStore()
    store.check_and_record("jti-1", ttl_seconds=1)
    time.sleep(1.2)
    assert store.seen("jti-1") is False
    # Post-expiry, the jti can be recorded again (a genuinely new
    # assertion is free to reuse the string once the old window has
    # closed — jti collision across time isn't the threat model;
    # replay within the assertion's own validity window is).
    assert store.check_and_record("jti-1", ttl_seconds=60) is False


def test_store_different_jtis_independent() -> None:
    store = InMemorySeenJtiStore()
    store.check_and_record("jti-1", ttl_seconds=60)
    assert store.check_and_record("jti-2", ttl_seconds=60) is False


# ---------------------------------------------------------------------------
# MerchantHook wiring.
# ---------------------------------------------------------------------------


def test_hook_without_store_does_not_check_replay() -> None:
    """Default behavior (jti_store=None) is unchanged — no replay
    check runs, matching the pre-existing hook behavior."""
    hook = MerchantHook()  # no jti_store
    doc = _bare_doc()
    headers = {"intent-assertion-jti": "reused-jti"}
    ctx1 = _ctx_for(agent_id=doc.agent_id, headers=headers)
    ctx2 = _ctx_for(agent_id=doc.agent_id, headers=headers)
    state = _state_for(doc)
    assert hook.before_dispatch(None, ctx1, state) is None
    assert hook.before_dispatch(None, ctx2, state) is None  # replayed, unchecked


def test_hook_with_store_allows_first_presentation() -> None:
    hook = MerchantHook(jti_store=InMemorySeenJtiStore())
    doc = _bare_doc()
    ctx = _ctx_for(
        agent_id=doc.agent_id,
        headers={"intent-assertion-jti": "jti-first"},
    )
    state = _state_for(doc)
    assert hook.before_dispatch(None, ctx, state) is None


def test_hook_with_store_refuses_replayed_jti() -> None:
    hook = MerchantHook(jti_store=InMemorySeenJtiStore())
    doc = _bare_doc()
    headers = {"intent-assertion-jti": "jti-replay-me"}
    state = _state_for(doc)

    first = hook.before_dispatch(
        None, _ctx_for(agent_id=doc.agent_id, headers=headers), state,
    )
    assert first is None

    second = hook.before_dispatch(
        None, _ctx_for(agent_id=doc.agent_id, headers=headers), state,
    )
    assert second is not None
    assert second.status_code == 458
    body = json.loads(second.body_bytes)
    assert body["error"]["reason"] == "intent-assertion-replayed"
    assert body["error"]["request_value"] == "jti-replay-me"


def test_hook_legacy_mode_does_not_trust_custom_ttl_header() -> None:
    hook = MerchantHook(jti_store=InMemorySeenJtiStore())
    doc = _bare_doc()
    state = _state_for(doc)
    headers = {
        "intent-assertion-jti": "jti-short-ttl",
        "intent-assertion-ttl-seconds": "1",
    }
    assert hook.before_dispatch(
        None, _ctx_for(agent_id=doc.agent_id, headers=headers), state,
    ) is None
    time.sleep(1.2)
    # Caller-controlled TTL hints are ignored; replay retention is
    # server policy in legacy mode and signed exp in verified mode.
    resp = hook.before_dispatch(
        None, _ctx_for(agent_id=doc.agent_id, headers=headers), state,
    )
    assert resp is not None
    assert resp.status_code == 458


def test_hook_without_jti_header_and_non_strict_passes_through() -> None:
    hook = MerchantHook(jti_store=InMemorySeenJtiStore(), strict=False)
    doc = _bare_doc()
    ctx = _ctx_for(agent_id=doc.agent_id, headers={})
    state = _state_for(doc)
    assert hook.before_dispatch(None, ctx, state) is None


def test_hook_without_jti_header_and_strict_refused() -> None:
    hook = MerchantHook(jti_store=InMemorySeenJtiStore(), strict=True)
    doc = _bare_doc()
    ctx = _ctx_for(
        agent_id=doc.agent_id,
        headers={
            "merchant-id": doc.agent_id,
            "merchant-manifest-fingerprint": doc.manifest_fingerprint(),
        },
    )
    state = _state_for(doc)
    resp = hook.before_dispatch(None, ctx, state)
    assert resp is not None
    assert resp.status_code == 458
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "missing-intent-assertion-jti-header"


def test_hook_replay_check_runs_after_merchant_id_check() -> None:
    """A mismatched Merchant-ID is still refused first (and with its
    own reason) even when jti replay checking is enabled — the
    counterparty check is the more fundamental one."""
    hook = MerchantHook(jti_store=InMemorySeenJtiStore())
    doc = _bare_doc()
    state = _state_for(doc)
    ctx = _ctx_for(
        agent_id=doc.agent_id,
        headers={
            "merchant-id": "f" * 64,
            "intent-assertion-jti": "jti-irrelevant",
        },
    )
    resp = hook.before_dispatch(None, ctx, state)
    assert resp is not None
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "merchant-id-mismatch"


if __name__ == "__main__":
    import unittest
    unittest.main()


def test_verified_assertion_binds_replay_to_signed_jti() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from agtp.intent import build_intent_assertion

    class _Daemon:
        def __init__(self, key):
            self.key = key
        def sign(self, data: bytes) -> bytes:
            return self.key.sign(data)

    key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    assertion = build_intent_assertion(
        daemon=_Daemon(key),
        issuer="buyer-agent",
        subject="principal-1",
        audience=doc.agent_id,
        amount="10.00",
        currency="USD",
        merchant_id=doc.agent_id,
        product_ref="sku:1",
        ttl_seconds=60,
    )
    hook = MerchantHook(
        jti_store=InMemorySeenJtiStore(),
        intent_public_key=key.public_key(),
        require_intent_assertion=True,
    )
    state = _state_for(doc)
    first_ctx = EndpointContext(
        input={"intent_assertion": assertion["jwt"]},
        agent_id=doc.agent_id,
        method="PURCHASE",
        headers={"intent-assertion-jti": assertion["jti"]},
    )
    assert hook.before_dispatch(None, first_ctx, state) is None

    # Rotating the untrusted header cannot bypass replay protection,
    # because the replay key is derived from the verified JWT payload.
    replay_ctx = EndpointContext(
        input={"intent_assertion": assertion["jwt"]},
        agent_id=doc.agent_id,
        method="PURCHASE",
        headers={"intent-assertion-jti": "attacker-rotated-value"},
    )
    resp = hook.before_dispatch(None, replay_ctx, state)
    assert resp is not None
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "intent-assertion-jti-mismatch"


def test_verified_assertion_replay_rejected_without_hint_header() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from agtp.intent import build_intent_assertion

    class _Daemon:
        def __init__(self, key): self.key = key
        def sign(self, data: bytes) -> bytes: return self.key.sign(data)

    key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    assertion = build_intent_assertion(
        daemon=_Daemon(key), issuer="buyer", subject="principal",
        audience=doc.agent_id, amount="1.00", currency="USD",
        merchant_id=doc.agent_id, product_ref="sku:2", ttl_seconds=60,
    )["jwt"]
    hook = MerchantHook(
        jti_store=InMemorySeenJtiStore(),
        intent_public_key=key.public_key(),
        require_intent_assertion=True,
    )
    state = _state_for(doc)
    ctx = lambda: EndpointContext(
        input={"intent_assertion": assertion}, agent_id=doc.agent_id,
        method="PURCHASE", headers={},
    )
    assert hook.before_dispatch(None, ctx(), state) is None
    resp = hook.before_dispatch(None, ctx(), state)
    assert resp is not None
    assert json.loads(resp.body_bytes)["error"]["reason"] == "intent-assertion-replayed"
