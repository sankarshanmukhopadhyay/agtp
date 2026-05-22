"""
Tests for Tier 3.2 — registrar-signed AgentDocuments.

  * AgentDocument.sign_manifest / verify_manifest_signature
    round-trip.
  * Daemon refuses to load an agent with a tampered manifest.
  * RegistrarStore.sign_manifest produces a document that
    verifies against the registrar's published public key.
  * POST /sign-manifest end-to-end via the reference HTTP server.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.genesis import public_key_pem
from core.identity import AgentDocument, RequiresDeclaration, from_dict
from server.main import AgentRegistry
from tools.registrar.server import _Handler
from tools.registrar.store import RegistrarStore


def _bare_doc(**overrides) -> AgentDocument:
    base = dict(
        agtp_version="1.0",
        agent_id="a" * 64,
        name="lauren",
        principal="Chris Hood",
        principal_id="chris",
        description="",
        status="active",
        skills=[],
        requires=RequiresDeclaration(),
        scopes_accepted=[],
        issued_at="2026-05-21T00:00:00Z",
        issuer="self",
    )
    base.update(overrides)
    return AgentDocument(**base)


# ---------------------------------------------------------------------------
# sign_manifest / verify_manifest_signature.
# ---------------------------------------------------------------------------


def test_sign_and_verify_round_trip() -> None:
    key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    doc.manifest_issuer = "registrar.example"
    doc.manifest_issuer_public_key = public_key_pem(key.public_key())
    doc.sign_manifest(key)
    assert doc.manifest_signature  # populated
    doc.verify_manifest_signature()  # no raise


def test_sign_requires_issuer_public_key() -> None:
    key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    # No manifest_issuer_public_key set.
    with pytest.raises(ValueError):
        doc.sign_manifest(key)


def test_verify_rejects_tampered_manifest() -> None:
    key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    doc.manifest_issuer = "registrar.example"
    doc.manifest_issuer_public_key = public_key_pem(key.public_key())
    doc.sign_manifest(key)
    # Mutate a field after signing.
    doc.trust_tier = 1
    with pytest.raises(ValueError):
        doc.verify_manifest_signature()


def test_verify_rejects_wrong_key() -> None:
    sig_key = Ed25519PrivateKey.generate()
    other_key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    doc.manifest_issuer = "registrar.example"
    doc.manifest_issuer_public_key = public_key_pem(sig_key.public_key())
    doc.sign_manifest(sig_key)
    # Replace the embedded public key with a different one; signature
    # no longer validates against the new key.
    doc.manifest_issuer_public_key = public_key_pem(other_key.public_key())
    with pytest.raises(ValueError):
        doc.verify_manifest_signature()


def test_verify_no_signature_raises() -> None:
    doc = _bare_doc()
    with pytest.raises(ValueError):
        doc.verify_manifest_signature()


# ---------------------------------------------------------------------------
# JSON round-trip including signature.
# ---------------------------------------------------------------------------


def test_signed_manifest_survives_json_round_trip() -> None:
    key = Ed25519PrivateKey.generate()
    doc = _bare_doc()
    doc.manifest_issuer = "registrar.example"
    doc.manifest_issuer_public_key = public_key_pem(key.public_key())
    doc.sign_manifest(key)
    # Persist + reload.
    serialized = json.dumps(doc.to_dict())
    reloaded = from_dict(json.loads(serialized))
    reloaded.verify_manifest_signature()
    assert reloaded.manifest_issuer == "registrar.example"
    assert reloaded.manifest_signature == doc.manifest_signature


def test_unsigned_manifest_omits_signature_fields_in_json() -> None:
    doc = _bare_doc()
    out = doc.to_dict()
    assert "manifest_signature" not in out
    assert "manifest_issuer" not in out
    assert "manifest_issuer_public_key" not in out


# ---------------------------------------------------------------------------
# AgentRegistry: refuses to load agents with bad signatures.
# ---------------------------------------------------------------------------


def _stage_signed_agent(tmp_path: Path, *, key: Ed25519PrivateKey,
                       agent_id: str = "a" * 64, **overrides) -> Path:
    doc = _bare_doc(agent_id=agent_id, **overrides)
    doc.manifest_issuer = "registrar.example"
    doc.manifest_issuer_public_key = public_key_pem(key.public_key())
    doc.sign_manifest(key)
    p = tmp_path / "lauren.agent.json"
    p.write_text(json.dumps(doc.to_dict(), indent=2), encoding="utf-8")
    return p


def test_registry_loads_valid_signed_manifest(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    aid = "a" * 64
    _stage_signed_agent(tmp_path, key=key, agent_id=aid)
    reg = AgentRegistry(tmp_path)
    assert reg.lookup(aid) is not None


def test_registry_skips_tampered_signed_manifest(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    aid = "a" * 64
    p = _stage_signed_agent(tmp_path, key=key, agent_id=aid)
    # Tamper with the on-disk file: bump trust_tier WITHOUT
    # re-signing.
    data = json.loads(p.read_text(encoding="utf-8"))
    data["trust_tier"] = 1
    p.write_text(json.dumps(data), encoding="utf-8")
    reg = AgentRegistry(tmp_path)
    assert reg.lookup(aid) is None  # refused


# ---------------------------------------------------------------------------
# RegistrarStore.sign_manifest.
# ---------------------------------------------------------------------------


def test_registrar_sign_manifest_round_trip(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path, issuer_id="registrar.example")
    doc = _bare_doc()
    signed = store.sign_manifest(doc.to_dict())
    # Reload as AgentDocument and verify the embedded signature.
    reloaded = from_dict(signed)
    reloaded.verify_manifest_signature()
    assert reloaded.manifest_issuer == "registrar.example"
    assert reloaded.manifest_issuer_public_key == store.issuer_public_key_pem


def test_registrar_sign_strips_operator_supplied_signature(tmp_path: Path) -> None:
    """If the operator passes in a doc with stale manifest_* fields,
    the registrar overrides them with its own — operators can't
    forge issuance with a previously-issued signature."""
    store = RegistrarStore(tmp_path, issuer_id="registrar.example")
    doc = _bare_doc()
    # Pretend the operator slipped in fake signature material.
    forged = doc.to_dict()
    forged["manifest_issuer"] = "evil.example"
    forged["manifest_issuer_public_key"] = "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----"
    forged["manifest_signature"] = "f" * 86
    signed = store.sign_manifest(forged)
    # The registrar's identity won.
    assert signed["manifest_issuer"] == "registrar.example"
    assert signed["manifest_issuer_public_key"] == store.issuer_public_key_pem


def test_registrar_sign_refuses_malformed_doc(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path)
    with pytest.raises(ValueError):
        store.sign_manifest({"name": "incomplete"})


# ---------------------------------------------------------------------------
# HTTP POST /sign-manifest.
# ---------------------------------------------------------------------------


@pytest.fixture
def running_registrar(tmp_path):
    store = RegistrarStore(tmp_path, issuer_id="registrar.test")
    handler_cls = type("BoundHandler", (_Handler,), {"store": store})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f"http://127.0.0.1:{port}", store)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_sign_manifest_returns_signed_doc(running_registrar) -> None:
    base, store = running_registrar
    doc = _bare_doc()
    body = json.dumps(doc.to_dict()).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/sign-manifest", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        signed = json.loads(resp.read())
    assert signed["manifest_issuer"] == "registrar.test"
    reloaded = from_dict(signed)
    reloaded.verify_manifest_signature()


def test_http_sign_manifest_rejects_non_json(running_registrar) -> None:
    base, _ = running_registrar
    req = urllib.request.Request(
        f"{base}/sign-manifest",
        data=b"name=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 415


def test_http_sign_manifest_rejects_garbage_body(running_registrar) -> None:
    base, _ = running_registrar
    req = urllib.request.Request(
        f"{base}/sign-manifest",
        data=b'{"name": "incomplete"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
