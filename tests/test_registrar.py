"""
Tests for the reference registrar (tools.registrar).

Covers:
  * RegistrarStore: keypair persistence, issuance, fetch, list.
  * HTTP server: GET /pubkey, GET /issued, GET /issued/{id},
    POST /issue (JSON and form), error paths.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http.server import ThreadingHTTPServer

from core.genesis import GenesisSignatureError, load_genesis_json, public_key_pem
from tools.registrar.server import _Handler
from tools.registrar.store import RegistrarStore


def _agent_pub() -> str:
    return public_key_pem(Ed25519PrivateKey.generate().public_key())


# ---------------------------------------------------------------------------
# RegistrarStore.
# ---------------------------------------------------------------------------


def test_store_creates_issuer_key_on_first_use(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path)
    assert (tmp_path / "registrar.key").exists()
    assert (tmp_path / "registrar.pub").exists()
    assert store.issuer_public_key_pem.startswith("-----BEGIN PUBLIC KEY-----")


def test_store_reuses_existing_key(tmp_path: Path) -> None:
    s1 = RegistrarStore(tmp_path)
    pem1 = s1.issuer_public_key_pem
    s2 = RegistrarStore(tmp_path)
    pem2 = s2.issuer_public_key_pem
    assert pem1 == pem2


def test_store_issue_persists_and_verifies(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path, issuer_id="registrar.example")
    g = store.issue(
        name="lauren",
        owner_id="nomotic.inc",
        principal_id="chris@nomotic.ai",
        agent_public_key_pem=_agent_pub(),
        trust_tier=2,
    )
    aid = g.canonical_agent_id()
    # On-disk file exists.
    on_disk = tmp_path / "issued" / f"{aid}.json"
    assert on_disk.exists()
    # Reload from disk: signature still verifies.
    reloaded = load_genesis_json(on_disk.read_text(encoding="utf-8"))
    reloaded.verify()
    assert reloaded.canonical_agent_id() == aid
    # Audit log has an entry.
    audit = (tmp_path / "issued.jsonl").read_text(encoding="utf-8").strip()
    assert aid in audit


def test_store_fetch_returns_none_for_unknown(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path)
    assert store.fetch("a" * 64) is None


def test_store_fetch_round_trip(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path)
    g = store.issue(
        name="x", owner_id="o", principal_id="p",
        agent_public_key_pem=_agent_pub(),
    )
    aid = g.canonical_agent_id()
    fetched = store.fetch(aid)
    assert fetched is not None
    assert fetched.canonical_agent_id() == aid


def test_store_fetch_refuses_path_traversal(tmp_path: Path) -> None:
    """A stray agent_id with path separators MUST NOT escape the
    issued/ directory."""
    store = RegistrarStore(tmp_path)
    # Place a file outside the issued/ tree that an attacker might
    # try to read via path-traversal.
    (tmp_path / "secret.json").write_text("nope")
    assert store.fetch("../secret") is None
    assert store.fetch("..\\secret") is None


def test_store_list_issued(tmp_path: Path) -> None:
    store = RegistrarStore(tmp_path)
    g1 = store.issue(
        name="a", owner_id="o", principal_id="p",
        agent_public_key_pem=_agent_pub(),
    )
    g2 = store.issue(
        name="b", owner_id="o", principal_id="p",
        agent_public_key_pem=_agent_pub(),
    )
    ids = store.list_issued()
    assert g1.canonical_agent_id() in ids
    assert g2.canonical_agent_id() in ids
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# HTTP server end-to-end.
# ---------------------------------------------------------------------------


@pytest.fixture
def running_server(tmp_path):
    """Spin up the registrar HTTP server on a free port for one test."""
    store = RegistrarStore(tmp_path, issuer_id="registrar.test")
    handler_cls = type("BoundHandler", (_Handler,), {"store": store})
    # Bind to port 0 → kernel assigns a free port.
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


def test_http_get_pubkey(running_server) -> None:
    base, store = running_server
    with urllib.request.urlopen(f"{base}/pubkey") as resp:
        assert resp.status == 200
        body = resp.read().decode("utf-8")
    assert body == store.issuer_public_key_pem


def test_http_get_form_renders(running_server) -> None:
    base, _ = running_server
    with urllib.request.urlopen(f"{base}/") as resp:
        body = resp.read().decode("utf-8")
    assert "<title>AGTP Registrar</title>" in body
    assert 'action="/issue"' in body


def test_http_post_issue_json(running_server) -> None:
    base, _ = running_server
    body = json.dumps({
        "name": "lauren",
        "owner_id": "nomotic.inc",
        "principal_id": "chris@nomotic.ai",
        "agent_public_key": _agent_pub(),
        "trust_tier": 2,
        "archetype": "analyst",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/issue",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 201
        result = json.loads(resp.read())
    assert result["name"] == "lauren"
    assert result["trust_tier"] == 2
    # The returned Genesis verifies (load + verify).
    reloaded = load_genesis_json(json.dumps(result))
    reloaded.verify()


def test_http_post_issue_rejects_missing_fields(running_server) -> None:
    base, _ = running_server
    req = urllib.request.Request(
        f"{base}/issue",
        data=b'{"name": "only"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_http_post_issue_rejects_unknown_archetype(running_server) -> None:
    base, _ = running_server
    body = json.dumps({
        "name": "x", "owner_id": "o",
        "agent_public_key": _agent_pub(),
        "archetype": "bogus",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/issue", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_http_get_issued_after_post(running_server) -> None:
    base, _ = running_server
    body = json.dumps({
        "name": "sarah", "owner_id": "example.com",
        "agent_public_key": _agent_pub(),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/issue", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    aid = load_genesis_json(json.dumps(result)).canonical_agent_id()

    # GET /issued lists the agent_id.
    with urllib.request.urlopen(f"{base}/issued") as resp:
        listing = json.loads(resp.read())
    assert aid in listing["issued"]

    # GET /issued/{aid} returns the same Genesis.
    with urllib.request.urlopen(f"{base}/issued/{aid}") as resp:
        fetched = json.loads(resp.read())
    assert fetched["name"] == "sarah"


def test_http_get_issued_unknown_returns_404(running_server) -> None:
    base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base}/issued/{'a' * 64}")
    assert exc_info.value.code == 404


def test_http_unknown_path_returns_404(running_server) -> None:
    base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base}/whatever")
    assert exc_info.value.code == 404
