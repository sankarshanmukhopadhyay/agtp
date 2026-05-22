"""
Tests for Phase 7 merchant infrastructure:

  * AgentGenesis.role and AgentDocument.role round-trip.
  * AgentDocument.manifest_fingerprint stability.
  * RegistrarStore.issue with role="merchant".
  * mod_merchant.MerchantHook: 458 paths and pass-through paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agtp.handlers import EndpointContext
from core import wire
from core.genesis import (
    AgentGenesis,
    load_genesis_json,
    public_key_pem,
    utc_now_iso,
)
from core.identity import AgentDocument, RequiresDeclaration
from mod_merchant import MerchantHook, install
from server.hooks import HookRegistry
from tools.registrar.store import RegistrarStore


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


def _ctx_for(
    *,
    method: str = "PURCHASE",
    agent_id: str = "a" * 64,
    headers: dict | None = None,
) -> EndpointContext:
    return EndpointContext(
        input={},
        agent_id=agent_id,
        method=method,
        headers=headers or {},
    )


def _make_registry_with(doc: AgentDocument):
    """Tiny duck-typed ServerState that supports lookup()."""
    class _State:
        def lookup(self, aid):
            return doc if aid == doc.agent_id else None
    return _State()


# ---------------------------------------------------------------------------
# AgentDocument role.
# ---------------------------------------------------------------------------
#
# NOTE: ``role`` is NOT a Genesis field. Genesis is immutable identity;
# role is a manifest-level capability that may change over an agent's
# life. Tests here cover the AgentDocument surface, the registrar
# Genesis flow (which doesn't take role), and mod_merchant which reads
# role from the AgentDocument at dispatch time.


def test_agent_document_role_defaults_to_agent() -> None:
    doc = _bare_doc(role="agent")
    assert doc.role == "agent"
    # role=agent is elided from the serialized form (default).
    assert "role" not in doc.to_dict()


def test_agent_document_role_merchant_emitted() -> None:
    doc = _bare_doc()
    assert doc.role == "merchant"
    assert doc.to_dict()["role"] == "merchant"


def test_agent_document_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        _bare_doc(role="overlord")


# ---------------------------------------------------------------------------
# manifest_fingerprint.
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    a = _bare_doc()
    b = _bare_doc()
    assert a.manifest_fingerprint() == b.manifest_fingerprint()
    assert len(a.manifest_fingerprint()) == 64


def test_fingerprint_changes_when_manifest_changes() -> None:
    a = _bare_doc()
    b = _bare_doc(name="something-else")
    assert a.manifest_fingerprint() != b.manifest_fingerprint()


# ---------------------------------------------------------------------------
# RegistrarStore with merchant role.
# ---------------------------------------------------------------------------


def test_registrar_issues_identity_only_genesis(tmp_path: Path) -> None:
    """The registrar issues identity-only Geneses — no role. Role is
    set on the AgentDocument after registration, allowing an agent to
    acquire merchant capabilities later without minting a new
    Agent-ID."""
    store = RegistrarStore(tmp_path, issuer_id="registrar.test")
    key = Ed25519PrivateKey.generate()
    g = store.issue(
        name="walmart", owner_id="walmart.inc",
        principal_id="walmart.inc",
        agent_public_key_pem=public_key_pem(key.public_key()),
        trust_tier=1, verification_path="dns-anchored",
    )
    g.verify()
    # The signed Genesis has no role field; identity is fully
    # captured by name/owner/principal/keys.
    assert "role" not in g.to_dict()


def test_legacy_genesis_with_role_field_loads_anyway(tmp_path: Path) -> None:
    """A pre-cleanup Genesis file in someone's filesystem might still
    carry role. The loader silently ignores it so old fixtures don't
    crash. Identity (the hash) is unaffected because role wasn't part
    of the canonical bytes for them either if they were never signed —
    if signed, they'd just become an unsigned Genesis with role in
    their payload, which we ignore on parse."""
    key = Ed25519PrivateKey.generate()
    pub = public_key_pem(key.public_key())
    legacy = {
        "agtp_genesis_version": "agtp-genesis/1",
        "name": "old", "owner_id": "o", "principal_id": "p",
        "agent_public_key": pub,
        "issued_at": utc_now_iso(),
        "issuer": "self", "issuer_public_key": pub,
        "role": "merchant",  # legacy field — ignored
    }
    g = load_genesis_json(json.dumps(legacy))
    assert not hasattr(g, "role")


# ---------------------------------------------------------------------------
# MerchantHook.
# ---------------------------------------------------------------------------


def test_non_purchase_passes_through() -> None:
    hook = MerchantHook()
    doc = _bare_doc()
    ctx = _ctx_for(method="QUOTE", agent_id=doc.agent_id)
    state = _make_registry_with(doc)
    assert hook.before_dispatch(None, ctx, state) is None


def test_purchase_against_agent_role_passes_through() -> None:
    """Agents that aren't merchants don't trigger the hook —
    the daemon's soft-deny gate handles those refusals."""
    hook = MerchantHook()
    doc = _bare_doc(role="agent")
    ctx = _ctx_for(method="PURCHASE", agent_id=doc.agent_id)
    state = _make_registry_with(doc)
    assert hook.before_dispatch(None, ctx, state) is None


def test_purchase_with_matching_merchant_id_passes() -> None:
    hook = MerchantHook()
    doc = _bare_doc()
    fp = doc.manifest_fingerprint()
    ctx = _ctx_for(
        method="PURCHASE", agent_id=doc.agent_id,
        headers={
            "merchant-id": doc.agent_id,
            "merchant-manifest-fingerprint": fp,
        },
    )
    state = _make_registry_with(doc)
    assert hook.before_dispatch(None, ctx, state) is None


def test_purchase_with_mismatched_merchant_id_refused() -> None:
    hook = MerchantHook()
    doc = _bare_doc()
    ctx = _ctx_for(
        method="PURCHASE", agent_id=doc.agent_id,
        headers={"merchant-id": "f" * 64},
    )
    state = _make_registry_with(doc)
    resp = hook.before_dispatch(None, ctx, state)
    assert resp is not None
    assert resp.status_code == 458
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "merchant-id-mismatch"


def test_purchase_with_mismatched_fingerprint_refused() -> None:
    hook = MerchantHook()
    doc = _bare_doc()
    ctx = _ctx_for(
        method="PURCHASE", agent_id=doc.agent_id,
        headers={
            "merchant-id": doc.agent_id,
            "merchant-manifest-fingerprint": "0" * 64,
        },
    )
    state = _make_registry_with(doc)
    resp = hook.before_dispatch(None, ctx, state)
    assert resp is not None
    assert resp.status_code == 458
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "merchant-manifest-fingerprint-mismatch"


def test_purchase_against_suspended_merchant_refused() -> None:
    hook = MerchantHook()
    doc = _bare_doc(status="suspended")
    ctx = _ctx_for(method="PURCHASE", agent_id=doc.agent_id)
    state = _make_registry_with(doc)
    resp = hook.before_dispatch(None, ctx, state)
    assert resp is not None
    assert resp.status_code == 458
    body = json.loads(resp.body_bytes)
    assert "merchant-not-active" in body["error"]["reason"]


def test_non_strict_mode_accepts_missing_merchant_id() -> None:
    """Default (non-strict) mode lets PURCHASE through when the
    legacy buyer didn't send Merchant-ID, with a stderr warning."""
    hook = MerchantHook(strict=False)
    doc = _bare_doc()
    ctx = _ctx_for(method="PURCHASE", agent_id=doc.agent_id)
    state = _make_registry_with(doc)
    assert hook.before_dispatch(None, ctx, state) is None


def test_strict_mode_refuses_missing_merchant_id() -> None:
    hook = MerchantHook(strict=True)
    doc = _bare_doc()
    ctx = _ctx_for(method="PURCHASE", agent_id=doc.agent_id)
    state = _make_registry_with(doc)
    resp = hook.before_dispatch(None, ctx, state)
    assert resp is not None
    assert resp.status_code == 458
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "missing-merchant-id-header"


def test_install_registers_one_hook() -> None:
    class FakeState:
        def __init__(self) -> None:
            self.hook_registry = HookRegistry()
    state = FakeState()
    install(state)
    assert state.hook_registry.count() == 1
    assert isinstance(state.hook_registry.all()[0], MerchantHook)
