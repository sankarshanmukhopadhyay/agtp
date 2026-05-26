"""
Tests for the OAuth / trust-anchor CLI flags on
``agtp-agent install`` and ``agtp-agent register``.

Covers Pattern 1 (default — no OAuth wired anywhere),
Pattern 2 (--oauth-validator preconfigures policies.oauth on the
generated AgentDocument), and Pattern 3 (--trust-anchor copies a
Genesis-issuer trust-anchors file into the agents directory under
the conventional name).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.identity import from_dict as agent_doc_from_dict
from tools.agtp_agent import main as cli_main


def _run(*argv: str) -> int:
    return cli_main(list(argv))


def _make_genesis(tmp_path: Path, name: str = "lauren") -> Path:
    """Quick fixture: produce a self-signed Genesis via the CLI."""
    rc = _run(
        "new", "--name", name, "--owner", "nomotic.inc",
        "--out-dir", str(tmp_path), "--force",
    )
    assert rc == 0
    return tmp_path / f"{name}.genesis.json"


# ---------------------------------------------------------------------------
# Pattern 1: no OAuth flags → no policies block on the AgentDocument.
# ---------------------------------------------------------------------------


def test_register_without_oauth_flags_produces_pattern1_document(
    tmp_path: Path,
) -> None:
    """The baseline: an operator running ``register`` with no
    OAuth-related flags produces an AgentDocument with an empty
    policies dict. That dict is elided when the document is
    serialized — Pattern 1 documents look exactly like they did
    before the OAuth composition work landed.
    """
    agents = tmp_path / "agents"
    agents.mkdir()
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--agents-dir", str(agents),
        "--methods", "DISCOVER,DESCRIBE,QUERY",
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    # Wire-format invariant: empty policies dict is not serialized.
    assert "policies" not in raw, (
        "Pattern 1 documents must not carry a policies key on the wire"
    )
    # In-memory shape: the loader still tolerates the missing key
    # and produces an AgentDocument with policies={}.
    doc = agent_doc_from_dict(raw)
    assert doc.policies == {}


# ---------------------------------------------------------------------------
# Pattern 2: --oauth-validator preconfigures policies.oauth.
# ---------------------------------------------------------------------------


def test_install_with_oauth_validator_writes_policies_oauth_block(
    tmp_path: Path,
) -> None:
    """--oauth-validator is the minimal Pattern 2 invocation: it
    pins a validator name and switches enabled=True. The generated
    AgentDocument carries the canonical policies.oauth shape that
    server.methods._resolve_oauth_config consumes."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--methods", "PURCHASE,DISCOVER",
        "--oauth-validator", "noop",
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    assert "policies" in raw
    oauth = raw["policies"]["oauth"]
    assert oauth["enabled"] is True
    assert oauth["validator"] == "noop"
    # Optional fields elided when unset.
    assert "required_on_methods" not in oauth
    assert "principal_id_claim" not in oauth
    assert "validator_config" not in oauth


def test_install_oauth_required_on_methods_normalized_uppercase(
    tmp_path: Path,
) -> None:
    """--oauth-required-on accepts a comma-separated list and
    uppercases each entry (AGTP method names are canonically
    uppercase; tolerate lowercase input)."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--methods", "PURCHASE,write,QUERY",
        "--oauth-validator", "noop",
        "--oauth-required-on", "purchase,write",
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    assert raw["policies"]["oauth"]["required_on_methods"] == [
        "PURCHASE", "WRITE",
    ]


def test_install_oauth_principal_id_claim_carried_through(
    tmp_path: Path,
) -> None:
    """--oauth-principal-id-claim overrides the default 'sub' claim
    that the validator lifts onto request.acting_principal_id."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--oauth-validator", "noop",
        "--oauth-principal-id-claim", "email",
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    assert raw["policies"]["oauth"]["principal_id_claim"] == "email"


def test_install_oauth_config_parses_json_object(tmp_path: Path) -> None:
    """--oauth-config takes a JSON blob that is passed verbatim
    into the validator's config (e.g. JWTValidator's public_key)."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--oauth-validator", "jwt",
        "--oauth-config",
        '{"public_key": "-----BEGIN PUBLIC KEY-----\\nABC\\n-----END PUBLIC KEY-----", "allowed_algs": ["EdDSA"]}',
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    cfg = raw["policies"]["oauth"]["validator_config"]
    assert cfg["public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert cfg["allowed_algs"] == ["EdDSA"]


def test_install_oauth_config_rejects_malformed_json(tmp_path: Path) -> None:
    """A malformed JSON blob fails fast at the CLI boundary —
    rather than waiting for the daemon to crash at boot."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    with pytest.raises(SystemExit, match="not valid JSON"):
        _run(
            "install",
            "--genesis", str(work / "lauren.genesis.json"),
            "--agents-dir", str(agents),
            "--oauth-validator", "jwt",
            "--oauth-config", "{not valid json",
        )


def test_install_oauth_config_rejects_non_object_json(tmp_path: Path) -> None:
    """A valid JSON value that isn't an object (an array, a string,
    a number) is refused — validator configs are always dicts."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    with pytest.raises(SystemExit, match="must be a JSON object"):
        _run(
            "install",
            "--genesis", str(work / "lauren.genesis.json"),
            "--agents-dir", str(agents),
            "--oauth-validator", "jwt",
            "--oauth-config", '["this", "is", "an", "array"]',
        )


def test_register_threads_oauth_flags_end_to_end(tmp_path: Path) -> None:
    """The end-to-end register command propagates OAuth flags
    through to the install step — operators bringing up a fresh
    agent under Pattern 2 do so in one invocation."""
    agents = tmp_path / "agents"
    agents.mkdir()
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--agents-dir", str(agents),
        "--methods", "PURCHASE",
        "--oauth-validator", "noop",
        "--oauth-required-on", "PURCHASE",
        "--oauth-principal-id-claim", "sub",
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    oauth = raw["policies"]["oauth"]
    assert oauth["enabled"] is True
    assert oauth["validator"] == "noop"
    assert oauth["required_on_methods"] == ["PURCHASE"]
    assert oauth["principal_id_claim"] == "sub"


def test_oauth_only_flags_without_validator_are_ignored(tmp_path: Path) -> None:
    """The OAuth-prefixed flags (other than --oauth-validator) are
    ignored when --oauth-validator itself is not supplied. This is
    the same Pattern 1 protection: an operator who forgets the
    validator flag does not silently get an enabled=False half-
    populated policies block."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--oauth-required-on", "PURCHASE",
        "--oauth-principal-id-claim", "email",
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    assert "policies" not in raw


# ---------------------------------------------------------------------------
# Pattern 3: --trust-anchor copies the anchors file into the agents dir.
# ---------------------------------------------------------------------------


def _write_anchor_file(path: Path) -> None:
    payload = {
        "anchors": [
            {"type": "key", "name": "primary",
             "value": "FXJ-X2hL3_DUMMY_KEY_FOR_TESTS_aBcDeFgHiJkLmN"},
            {"type": "oidc", "name": "enterprise-idp",
             "discovery_url":
                 "https://idp.example/.well-known/openid-configuration",
             "trusted_issuer": "https://idp.example"},
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_install_with_trust_anchor_copies_anchor_file(
    tmp_path: Path,
) -> None:
    """--trust-anchor copies the JSON anchors file into the agents
    dir under the conventional name 'trust-anchors.json' so the
    daemon's loader picks it up at boot."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    anchors_src = work / "my-anchors.json"
    _write_anchor_file(anchors_src)

    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--trust-anchor", str(anchors_src),
    )
    assert rc == 0
    anchors_dst = agents / "trust-anchors.json"
    assert anchors_dst.exists()
    data = json.loads(anchors_dst.read_text(encoding="utf-8"))
    assert data["anchors"][0]["name"] == "primary"
    assert data["anchors"][1]["type"] == "oidc"


def test_register_with_trust_anchor_threads_through(tmp_path: Path) -> None:
    """End-to-end: --trust-anchor on `register` flows through to
    the install step."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    anchors_src = work / "my-anchors.json"
    _write_anchor_file(anchors_src)
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--agents-dir", str(agents),
        "--trust-anchor", str(anchors_src),
    )
    assert rc == 0
    assert (agents / "trust-anchors.json").exists()


def test_trust_anchor_missing_file_fails_fast(tmp_path: Path) -> None:
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    with pytest.raises(SystemExit, match="trust-anchor file not found"):
        _run(
            "install",
            "--genesis", str(work / "lauren.genesis.json"),
            "--agents-dir", str(agents),
            "--trust-anchor", str(work / "does-not-exist.json"),
        )


def test_trust_anchor_malformed_json_fails_fast(tmp_path: Path) -> None:
    """A malformed anchors file is refused at CLI time so the
    operator sees the error immediately — rather than the daemon
    silently treating the broken file as 'no anchors configured'."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    bad = work / "bad.json"
    bad.write_text("not valid json {", encoding="utf-8")
    with pytest.raises(SystemExit, match="not valid JSON"):
        _run(
            "install",
            "--genesis", str(work / "lauren.genesis.json"),
            "--agents-dir", str(agents),
            "--trust-anchor", str(bad),
        )


def test_trust_anchor_refuses_overwrite_without_force(tmp_path: Path) -> None:
    """The conventional file name means a second install would
    naturally collide with the first — block the collision unless
    --force is set."""
    work = tmp_path / "work"
    agents = tmp_path / "agents"
    work.mkdir(); agents.mkdir()
    _make_genesis(work)
    anchors_src = work / "my-anchors.json"
    _write_anchor_file(anchors_src)
    rc = _run(
        "install",
        "--genesis", str(work / "lauren.genesis.json"),
        "--agents-dir", str(agents),
        "--trust-anchor", str(anchors_src),
        "--force",
    )
    assert rc == 0
    # Second install without --force MUST refuse to clobber.
    with pytest.raises(SystemExit, match="already exists"):
        _run(
            "install",
            "--genesis", str(work / "lauren.genesis.json"),
            "--agents-dir", str(agents),
            "--trust-anchor", str(anchors_src),
        )


# ---------------------------------------------------------------------------
# Combined Pattern 2 + Pattern 3 (operationally common).
# ---------------------------------------------------------------------------


def test_register_with_both_oauth_and_trust_anchor(tmp_path: Path) -> None:
    """Enterprise deployment shape: OAuth gates outbound traffic
    AND the registrar issuing the agent's Genesis is federated via
    OIDC. Both layers wire in a single invocation."""
    agents = tmp_path / "agents"
    agents.mkdir()
    anchors_src = tmp_path / "anchors.json"
    _write_anchor_file(anchors_src)
    rc = _run(
        "register",
        "--name", "lauren",
        "--owner", "nomotic.inc",
        "--agents-dir", str(agents),
        "--methods", "PURCHASE",
        "--oauth-validator", "noop",
        "--oauth-required-on", "PURCHASE",
        "--trust-anchor", str(anchors_src),
    )
    assert rc == 0
    raw = json.loads((agents / "lauren.agent.json").read_text(encoding="utf-8"))
    assert raw["policies"]["oauth"]["enabled"] is True
    assert (agents / "trust-anchors.json").exists()
