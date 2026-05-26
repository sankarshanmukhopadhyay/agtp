"""
Tests for tools.agtp_agent — the unified agent-lifecycle CLI.

Covers the four subcommands (new / cert / install / register) and
the end-to-end property that matters operationally: the files the
CLI writes are exactly the files the daemon's AgentRegistry can
load to bring an agent online.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from core.genesis import parse_genesis
from core.identity import from_dict as agent_doc_from_dict
from tools.agtp_agent import main as cli_main


# ---------------------------------------------------------------------------
# `new` — keypair + Genesis.
# ---------------------------------------------------------------------------


def _run(*argv: str) -> int:
    return cli_main(list(argv))


def test_new_self_signed_writes_keypair_and_genesis(tmp_path: Path) -> None:
    """The minimal new-agent invocation produces three files: the
    Ed25519 private key (PEM), the matching public key (PEM), and
    the self-signed Genesis. The Agent-ID derives from the canonical
    Genesis hash and the Genesis verifies its own signature."""
    rc = _run(
        "new",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--out-dir", str(tmp_path),
    )
    assert rc == 0
    key_path = tmp_path / "lauren.key"
    pub_path = tmp_path / "lauren.pub"
    genesis_path = tmp_path / "lauren.genesis.json"
    assert key_path.exists()
    assert pub_path.exists()
    assert genesis_path.exists()

    genesis_json = json.loads(genesis_path.read_text(encoding="utf-8"))
    genesis = parse_genesis(genesis_json)
    # Self-signed: issuer is 'self'; signature verifies.
    assert genesis.issuer == "self"
    genesis.verify()
    # Defaults: tier 2 / self-signed verification path.
    assert genesis.trust_tier == 2
    assert genesis.verification_path == "self-signed"


def test_new_propagates_principal_and_archetype(tmp_path: Path) -> None:
    rc = _run(
        "new",
        "--name", "ada",
        "--owner", "nomotic.inc",
        "--principal", "chris@nomotic.ai",
        "--archetype", "analyst",
        "--zone", "zone:rnd",
        "--out-dir", str(tmp_path),
    )
    assert rc == 0
    genesis_json = json.loads(
        (tmp_path / "ada.genesis.json").read_text(encoding="utf-8"),
    )
    genesis = parse_genesis(genesis_json)
    assert genesis.principal_id == "chris@nomotic.ai"
    assert genesis.archetype == "analyst"
    assert genesis.governance_zone == "zone:rnd"


def test_new_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """An existing key file in the output directory blocks the
    second invocation unless --force is set."""
    rc = _run(
        "new", "--name", "lauren", "--owner", "x", "--out-dir", str(tmp_path),
    )
    assert rc == 0
    with pytest.raises(SystemExit, match="already exists"):
        _run(
            "new", "--name", "lauren", "--owner", "x",
            "--out-dir", str(tmp_path),
        )
    # --force allows the rewrite.
    rc = _run(
        "new", "--name", "lauren", "--owner", "x",
        "--out-dir", str(tmp_path), "--force",
    )
    assert rc == 0


def test_new_via_registrar_posts_public_key(tmp_path: Path) -> None:
    """When --registrar is supplied, the CLI POSTs the public key
    to that URL and writes the returned Genesis. We mock urlopen to
    return a synthesized response."""
    import urllib.request

    # Build a faux registrar response: a Genesis signed by a
    # different key (the "registrar") instead of self-signed.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from core.genesis import AgentGenesis, public_key_pem, utc_now_iso

    registrar_key = Ed25519PrivateKey.generate()
    registrar_pub_pem = public_key_pem(registrar_key.public_key())

    posted_payloads: List[dict] = []

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self) -> bytes: return self._body

    def _fake_urlopen(req, *args, **kwargs):
        # Inspect the posted payload, then synthesize a signed
        # Genesis from the public key in the payload.
        body = json.loads(req.data.decode("utf-8"))
        posted_payloads.append(body)
        genesis = AgentGenesis(
            name=body["name"],
            owner_id=body["owner_id"],
            principal_id=body["principal_id"],
            agent_public_key=body["agent_public_key"],
            issued_at=utc_now_iso(),
            issuer="registrar.example",
            issuer_public_key=registrar_pub_pem,
            trust_tier=body.get("trust_tier", 2),
            verification_path=body.get("verification_path", "self-signed"),
        )
        genesis.sign(registrar_key)
        return _FakeResponse(genesis.to_pretty_json().encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        rc = _run(
            "new",
            "--name", "lauren",
            "--owner", "nomotic.inc",
            "--principal", "chris@nomotic.ai",
            "--registrar", "https://registrar.example.com/issue",
            "--out-dir", str(tmp_path),
        )
    assert rc == 0
    # The registrar received the public key the CLI minted.
    assert len(posted_payloads) == 1
    posted = posted_payloads[0]
    assert posted["name"] == "lauren"
    assert posted["owner_id"] == "nomotic.inc"
    assert posted["principal_id"] == "chris@nomotic.ai"
    # The written Genesis carries the registrar's signature, not
    # 'self'.
    genesis_json = json.loads(
        (tmp_path / "lauren.genesis.json").read_text(encoding="utf-8"),
    )
    genesis = parse_genesis(genesis_json)
    assert genesis.issuer == "registrar.example"
    genesis.verify()


# ---------------------------------------------------------------------------
# `install` — AgentDocument + placement.
# ---------------------------------------------------------------------------


def _make_genesis(tmp_path: Path, name: str = "lauren") -> Path:
    """Quick fixture: produce a self-signed Genesis via the CLI."""
    rc = _run("new", "--name", name, "--owner", "nomotic.inc",
              "--out-dir", str(tmp_path), "--force")
    assert rc == 0
    return tmp_path / f"{name}.genesis.json"


def test_install_generates_agent_document(tmp_path: Path) -> None:
    """The install subcommand produces a valid AgentDocument JSON
    that the AgentDocument loader accepts cleanly."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    genesis_path = _make_genesis(work)

    rc = _run(
        "install",
        "--genesis", str(genesis_path),
        "--agents-dir", str(agents),
        "--methods", "DISCOVER,DESCRIBE,QUERY",
        "--scopes", "bookings:write",
        "--skills", "coffee,scheduling",
        "--description", "Reception agent",
    )
    assert rc == 0
    doc_path = agents / "lauren.agent.json"
    assert doc_path.exists()
    data = json.loads(doc_path.read_text(encoding="utf-8"))
    doc = agent_doc_from_dict(data)
    assert doc.name == "lauren"
    assert doc.description == "Reception agent"
    assert doc.skills == ["coffee", "scheduling"]
    assert doc.requires.methods == ["DISCOVER", "DESCRIBE", "QUERY"]
    assert doc.requires.scopes == ["bookings:write"]
    # owner_id and trust posture inherit from the Genesis.
    assert doc.owner_id == "nomotic.inc"
    assert doc.trust_tier == 2
    assert doc.verification_path == "self-signed"


def test_install_copies_genesis_to_agents_dir(tmp_path: Path) -> None:
    """The Genesis flows into the agents dir alongside the
    AgentDocument so the daemon's loader sees both."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    genesis_path = _make_genesis(work)

    rc = _run(
        "install",
        "--genesis", str(genesis_path),
        "--agents-dir", str(agents),
    )
    assert rc == 0
    assert (agents / "lauren.genesis.json").exists()
    assert (agents / "lauren.agent.json").exists()


def test_install_refuses_unverifiable_genesis(tmp_path: Path) -> None:
    """A Genesis whose signature doesn't verify is refused at install
    time — the daemon would also refuse it, but catching at the
    install boundary gives a better error."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    genesis_path = _make_genesis(work)
    # Corrupt the signature.
    data = json.loads(genesis_path.read_text(encoding="utf-8"))
    data["signature"] = "AAAA" + (data["signature"] or "x" * 80)[4:]
    genesis_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with pytest.raises(SystemExit, match="signature"):
        _run(
            "install",
            "--genesis", str(genesis_path),
            "--agents-dir", str(agents),
        )


def test_install_with_merchant_role(tmp_path: Path) -> None:
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)

    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--role", "merchant",
        "--methods", "PURCHASE,QUOTE",
    )
    assert rc == 0
    doc = agent_doc_from_dict(
        json.loads((agents / "lauren.agent.json").read_text())
    )
    assert doc.role == "merchant"
    assert "PURCHASE" in doc.requires.methods


def test_install_with_wildcards_flag(tmp_path: Path) -> None:
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)

    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--wildcards",
    )
    assert rc == 0
    doc = agent_doc_from_dict(
        json.loads((agents / "lauren.agent.json").read_text())
    )
    assert doc.requires.wildcards is True


# ---------------------------------------------------------------------------
# `register` — end-to-end.
# ---------------------------------------------------------------------------


def test_register_produces_files_daemon_can_load(tmp_path: Path) -> None:
    """The key operational invariant: running `register` produces
    a state in the agents directory that AgentRegistry loads
    cleanly. Wires the full lifecycle without an actual daemon
    instance — the in-process AgentRegistry is the same loader
    agtpd uses at boot."""
    agents = tmp_path / "agents"
    agents.mkdir()

    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--principal", "chris@nomotic.ai",
        "--agents-dir", str(agents),
        "--methods", "DISCOVER,DESCRIBE,QUERY",
        "--scopes", "bookings:write",
        "--skills", "coffee",
        "--description", "Reception agent",
    )
    assert rc == 0

    # AgentRegistry is what agtpd uses at boot.
    from server.main import AgentRegistry
    reg = AgentRegistry(agents)
    assert len(reg.agents) == 1
    aid = next(iter(reg.agents))
    doc = reg.agents[aid]
    assert doc.name == "lauren"
    assert doc.principal_id == "chris@nomotic.ai"
    assert doc.owner_id == "nomotic.inc"
    # Genesis loaded alongside (cryptographic identity binding).
    assert aid in reg.geneses
    assert reg.geneses[aid].canonical_agent_id() == aid


def test_register_preserves_keypair_in_agents_dir(tmp_path: Path) -> None:
    """When --with-cert is NOT supplied, register defensively
    copies the keypair to agents_dir so the operator doesn't lose
    the private key when the temp work dir is cleaned up."""
    agents = tmp_path / "agents"
    agents.mkdir()
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--agents-dir", str(agents),
    )
    assert rc == 0
    assert (agents / "lauren.key").exists()
    assert (agents / "lauren.pub").exists()


def test_register_can_be_rerun_with_force(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "x",
        "--agents-dir", str(agents),
    )
    assert rc == 0
    # Second run without --force collides on the existing AgentDocument.
    with pytest.raises(SystemExit, match="already exists"):
        _run(
            "register",
            "--name", "lauren",
            "--owner", "x",
            "--agents-dir", str(agents),
        )
    # With --force, succeeds (mints a fresh keypair → new Agent-ID).
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "x",
        "--agents-dir", str(agents),
        "--force",
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# argparse surface.
# ---------------------------------------------------------------------------


def test_cli_help_lists_all_subcommands(capsys) -> None:
    """The top-level --help lists new, cert, install, register so
    operators discover them."""
    with pytest.raises(SystemExit):
        _run("--help")
    captured = capsys.readouterr()
    for sub in ("new", "cert", "install", "register"):
        assert sub in captured.out


def test_register_requires_name_owner_agents_dir() -> None:
    with pytest.raises(SystemExit):
        _run("register")  # nothing
    with pytest.raises(SystemExit):
        _run("register", "--name", "x")  # missing --owner
    with pytest.raises(SystemExit):
        # missing --agents-dir
        _run("register", "--name", "x", "--owner", "y")
