"""
Tests for core.genesis — the Agent Genesis schema, hashing,
signing, and cert-binding helper.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.genesis import (
    AgentGenesis,
    GENESIS_VERSION,
    GenesisFormatError,
    GenesisSignatureError,
    load_genesis_json,
    public_key_pem,
    utc_now_iso,
    verify_cert_genesis_binding,
)


def _make_genesis(
    *,
    name: str = "lauren",
    owner_id: str = "nomotic.inc",
    principal_id: str = "chris@nomotic.ai",
    issuer: str = "self",
    issuer_key: Ed25519PrivateKey | None = None,
    archetype: str | None = None,
    trust_tier: int = 2,
) -> tuple[AgentGenesis, Ed25519PrivateKey]:
    """Build, sign, and return a complete Genesis + the issuer key."""
    agent_key = Ed25519PrivateKey.generate()
    pub_pem = public_key_pem(agent_key.public_key())
    sign_key = issuer_key or agent_key
    issuer_pub = public_key_pem(sign_key.public_key())
    g = AgentGenesis(
        name=name,
        owner_id=owner_id,
        principal_id=principal_id,
        agent_public_key=pub_pem,
        archetype=archetype,
        issued_at=utc_now_iso(),
        issuer=issuer,
        issuer_public_key=issuer_pub,
        trust_tier=trust_tier,
    )
    g.sign(sign_key)
    return g, agent_key


# ---------------------------------------------------------------------------
# Canonical hash + signature.
# ---------------------------------------------------------------------------


def test_canonical_agent_id_is_deterministic() -> None:
    """The same Genesis content produces the same Agent-ID."""
    g, _ = _make_genesis()
    aid1 = g.canonical_agent_id()
    aid2 = g.canonical_agent_id()
    assert aid1 == aid2
    assert len(aid1) == 64
    assert all(c in "0123456789abcdef" for c in aid1)


def test_canonical_agent_id_excludes_signature_field() -> None:
    """The hash is computed over the Genesis with signature stripped,
    so re-signing the same Genesis (different signature) produces the
    same Agent-ID. Otherwise the hash and signature would be
    self-referential."""
    g, key = _make_genesis()
    aid1 = g.canonical_agent_id()
    # Sign again with the same key — signature bytes change because
    # Ed25519 is deterministic per (key, message), so they'll match.
    # Use a fresh key to perturb the signature.
    new_key = Ed25519PrivateKey.generate()
    g.issuer_public_key = public_key_pem(new_key.public_key())
    g.sign(new_key)
    # Hash should change because issuer_public_key changed; that's
    # consistent with our invariant (the hash excludes signature
    # only, not the rest of the document).
    aid2 = g.canonical_agent_id()
    assert aid1 != aid2

    # Reset to identical content, change only the signature field
    # directly: the hash must stay the same.
    g2, _ = _make_genesis()
    aid_pre = g2.canonical_agent_id()
    g2.signature = "tampered"
    aid_post = g2.canonical_agent_id()
    assert aid_pre == aid_post


def test_signature_verifies_round_trip() -> None:
    g, _ = _make_genesis()
    g.verify()  # raises on failure


def test_signature_tamper_is_rejected() -> None:
    g, _ = _make_genesis()
    # Mutate a field after signing; the signature no longer matches.
    g.name = "tampered"
    with pytest.raises(GenesisSignatureError):
        g.verify()


def test_unsigned_genesis_does_not_verify() -> None:
    g, _ = _make_genesis()
    g.signature = ""
    with pytest.raises(GenesisSignatureError):
        g.verify()


# ---------------------------------------------------------------------------
# Serialization round-trip.
# ---------------------------------------------------------------------------


def test_json_roundtrip_preserves_hash() -> None:
    g, _ = _make_genesis()
    aid = g.canonical_agent_id()
    text = g.to_pretty_json()
    g2 = load_genesis_json(text)
    assert g2.canonical_agent_id() == aid
    g2.verify()


def test_canonical_json_is_sorted_and_compact() -> None:
    g, _ = _make_genesis()
    canonical = g.to_canonical_json()
    # Compact = no whitespace *between* JSON tokens. (String values
    # may legitimately contain spaces — e.g., PEM keys carry
    # 'BEGIN PUBLIC KEY' literally.) The portable check: the rendered
    # form must equal a re-render with the same separators.
    parsed = json.loads(canonical)
    assert canonical == json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    # Sorted keys at the top level.
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_optional_fields_omitted_when_none() -> None:
    g, _ = _make_genesis(archetype=None)
    d = g.to_dict()
    # archetype was None, must not appear.
    assert "archetype" not in d
    # governance_zone was never set; same.
    assert "governance_zone" not in d


# ---------------------------------------------------------------------------
# Parsing & validation.
# ---------------------------------------------------------------------------


def test_parse_rejects_missing_required_fields() -> None:
    with pytest.raises(GenesisFormatError):
        load_genesis_json(json.dumps({"name": "x"}))


def test_parse_rejects_bad_trust_tier() -> None:
    g, key = _make_genesis()
    data = g.to_dict()
    data["trust_tier"] = 9
    with pytest.raises(GenesisFormatError):
        load_genesis_json(json.dumps(data))


def test_parse_rejects_unknown_archetype() -> None:
    g, key = _make_genesis()
    data = g.to_dict()
    data["archetype"] = "supreme-overlord"
    with pytest.raises(GenesisFormatError):
        load_genesis_json(json.dumps(data))


def test_parse_rejects_unsupported_version() -> None:
    g, _ = _make_genesis()
    data = g.to_dict()
    data["agtp_genesis_version"] = "agtp-genesis/99"
    with pytest.raises(GenesisFormatError):
        load_genesis_json(json.dumps(data))


# ---------------------------------------------------------------------------
# Cert-binding helper.
# ---------------------------------------------------------------------------


def test_cert_binding_passes_when_subject_matches_hash() -> None:
    g, _ = _make_genesis()
    aid = g.canonical_agent_id()
    verify_cert_genesis_binding(genesis=g, subject_agent_id=aid)


def test_cert_binding_rejects_mismatched_hash() -> None:
    g, _ = _make_genesis()
    with pytest.raises(GenesisFormatError):
        verify_cert_genesis_binding(genesis=g, subject_agent_id="f" * 64)


def test_cert_binding_rejects_invalid_signature() -> None:
    g, _ = _make_genesis()
    g.signature = ""  # unsigned
    with pytest.raises(GenesisSignatureError):
        verify_cert_genesis_binding(
            genesis=g, subject_agent_id=g.canonical_agent_id(),
        )


# ---------------------------------------------------------------------------
# Registrar-signed (separate issuer key).
# ---------------------------------------------------------------------------


def test_registrar_signed_genesis() -> None:
    """A Genesis signed by a registrar key separate from the agent's
    key verifies against the registrar key, not the agent's key."""
    registrar_key = Ed25519PrivateKey.generate()
    g, agent_key = _make_genesis(
        issuer="registrar.example.com",
        issuer_key=registrar_key,
    )
    # The agent's public key in the Genesis is independent of the
    # signing key.
    assert g.agent_public_key != g.issuer_public_key
    # Signature verifies against issuer_public_key (the registrar).
    g.verify()
