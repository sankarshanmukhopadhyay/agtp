"""
Tests for T4.2 — SCITT / COSE_Sign1 lifecycle receipts.

  * server.cose: CBOR codec round-trip and COSE_Sign1 sign/verify.
  * Lifecycle handlers in scitt mode emit COSE_Sign1 lines.
  * INSPECT target=lifecycle parses both JWS and COSE entries (mixed
    streams stay readable).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import wire
from core.genesis import AgentGenesis, public_key_pem, utc_now_iso
from server.config import (
    AuditConfig, ServerConfig, ServerInfo, SigningConfig,
)
from server.cose import (
    CoseError,
    build_cose_sign1,
    cbor_decode,
    cbor_encode,
    cose_audit_id,
    parse_cose_payload,
    verify_cose_sign1,
)
from server.main import AgentRegistry
from server.methods import handle_inspect, handle_revoke
from server.signing import SigningService


# ---------------------------------------------------------------------------
# CBOR codec round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    0, 1, 23, 24, 100, 255, 256, 65535, 65536,
    -1, -24, -100, -256, -65536,
    "hello", "",
    b"\x00\x01\x02", b"",
    [], [1, 2, 3], ["a", "b"],
    {}, {1: "one", 2: "two"}, {"a": 1, "b": [2, 3]},
    None, True, False,
])
def test_cbor_round_trip(value) -> None:
    encoded = cbor_encode(value)
    decoded = cbor_decode(encoded)
    assert decoded == value


def test_cbor_map_canonical_ordering() -> None:
    """Canonical encoding: keys sorted by encoded bytes. So {1: ...,
    2: ..., 10: ...} encodes in that order regardless of dict
    insertion order."""
    a = cbor_encode({10: "a", 1: "b", 2: "c"})
    b = cbor_encode({1: "b", 2: "c", 10: "a"})
    assert a == b


# ---------------------------------------------------------------------------
# COSE_Sign1 sign + verify.
# ---------------------------------------------------------------------------


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def test_cose_sign_verify_round_trip() -> None:
    k = _key()
    payload = b'{"event":"test"}'
    cose = build_cose_sign1(
        private_key=k, payload_bytes=payload, kid="ed25519-test",
    )
    header = verify_cose_sign1(cose_bytes=cose, public_key=k.public_key())
    assert header[1] == -8  # alg = EdDSA
    assert header[4] == "ed25519-test"  # kid
    assert header[16] == "application/agtp-lifecycle+json"  # typ


def test_cose_tamper_detected() -> None:
    k = _key()
    cose = build_cose_sign1(
        private_key=k, payload_bytes=b"x", kid="k",
    )
    tampered = cose[:-1] + bytes([cose[-1] ^ 1])  # flip last sig byte
    with pytest.raises(CoseError):
        verify_cose_sign1(cose_bytes=tampered, public_key=k.public_key())


def test_cose_wrong_key_refused() -> None:
    k1 = _key()
    k2 = _key()
    cose = build_cose_sign1(
        private_key=k1, payload_bytes=b"x", kid="k",
    )
    with pytest.raises(CoseError):
        verify_cose_sign1(cose_bytes=cose, public_key=k2.public_key())


def test_cose_audit_id_is_sha256_of_bytes() -> None:
    import hashlib
    k = _key()
    cose = build_cose_sign1(
        private_key=k, payload_bytes=b"hello", kid="k",
    )
    assert cose_audit_id(cose) == hashlib.sha256(cose).hexdigest()


def test_parse_cose_payload_recovers_json() -> None:
    k = _key()
    payload = json.dumps(
        {"event_type": "revoke", "agent_id": "a" * 64},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    cose = build_cose_sign1(private_key=k, payload_bytes=payload, kid="k")
    parsed = parse_cose_payload(cose)
    assert parsed["payload"]["event_type"] == "revoke"
    assert parsed["payload"]["agent_id"] == "a" * 64


def test_parse_cose_payload_refuses_bad_tag() -> None:
    """A CBOR-encoded array NOT tagged 18 isn't a COSE_Sign1."""
    not_cose = cbor_encode([b"x", {}, b"y", b"z"])
    with pytest.raises(CoseError):
        parse_cose_payload(not_cose)


# ---------------------------------------------------------------------------
# End-to-end: lifecycle handler in scitt mode.
# ---------------------------------------------------------------------------


def _make_signing_service(tmp_path: Path) -> SigningService:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "signing.key"
    path.write_bytes(pem)
    return SigningService.from_key_path(str(path))


def _stage_scitt(tmp_path: Path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    audit_root = tmp_path / "audit"

    agent_key = Ed25519PrivateKey.generate()
    pub_pem = public_key_pem(agent_key.public_key())
    g = AgentGenesis(
        name="lauren", owner_id="nomotic.inc", principal_id="chris",
        agent_public_key=pub_pem, issued_at=utc_now_iso(),
        issuer="self", issuer_public_key=pub_pem,
    )
    g.sign(agent_key)
    aid = g.canonical_agent_id()
    (agent_dir / "lauren.agent.json").write_text(json.dumps({
        "agtp_version": "v0.0.6", "agent_id": aid, "name": "lauren",
        "principal": "c", "principal_id": "c", "description": "",
        "status": "active", "skills": [],
        "requires": {
            "methods": ["ACTIVATE", "DEACTIVATE", "REVOKE", "INSPECT"],
            "scopes": [], "wildcards": False,
        },
        "scopes_accepted": [], "issued_at": "now", "issuer": "self",
    }))
    (agent_dir / "lauren.genesis.json").write_text(g.to_pretty_json())

    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        audit=AuditConfig(
            attribution_records_enabled=True,
            chain_head_root=str(audit_root / "chain_heads"),
            records_root=str(audit_root / "records"),
            lifecycle_root=str(audit_root / "lifecycle"),
            mode="scitt",
        ),
        signing=SigningConfig(enabled=True),
    )
    cfg.signing_service = _make_signing_service(tmp_path)
    reg = AgentRegistry(agent_dir)
    reg.config = cfg
    return reg, aid, cfg, reg.lookup(aid)


def _req(method: str, aid: str, body: dict | None = None) -> wire.AGTPRequest:
    raw = json.dumps(body or {}).encode("utf-8")
    return wire.AGTPRequest(
        method=method,
        headers={"Agent-ID": aid, "Content-Length": str(len(raw))},
        body_bytes=raw,
    )


def test_scitt_mode_writes_cose_line(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage_scitt(tmp_path)
    handle_revoke(_req("REVOKE", aid, {"reason": "compromised"}), reg, doc)

    lifecycle_path = Path(cfg.audit.lifecycle_root) / f"{aid}.jsonl"
    assert lifecycle_path.exists()
    lines = lifecycle_path.read_text(encoding="ascii").strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("cose:")

    # Round-trip: decode the line, verify against the daemon's key.
    encoded = lines[0][len("cose:"):]
    padded = encoded + "=" * (-len(encoded) % 4)
    cose_bytes = base64.urlsafe_b64decode(padded)
    header = verify_cose_sign1(
        cose_bytes=cose_bytes,
        public_key=cfg.signing_service.public_key,
    )
    assert header[4] == cfg.signing_service.key_id

    parsed = parse_cose_payload(cose_bytes)
    assert parsed["payload"]["extra"]["event_type"] == "revoke"
    assert parsed["payload"]["extra"]["new_status"] == "retired"
    assert parsed["payload"]["extra"]["reason"] == "compromised"


def test_scitt_audit_id_is_sha256_of_cose(tmp_path: Path) -> None:
    """The audit_id stamped on the response equals sha256 of the COSE
    bytes — same role the JWS audit_id plays in mode=jws."""
    reg, aid, cfg, doc = _stage_scitt(tmp_path)
    resp = handle_revoke(_req("REVOKE", aid), reg, doc)
    body = json.loads(resp.body_bytes)
    response_audit_id = body["audit_id"]

    # Read the stored line and compute sha256 of the COSE bytes.
    lifecycle_path = Path(cfg.audit.lifecycle_root) / f"{aid}.jsonl"
    line = lifecycle_path.read_text(encoding="ascii").strip()
    encoded = line[len("cose:"):]
    padded = encoded + "=" * (-len(encoded) % 4)
    cose_bytes = base64.urlsafe_b64decode(padded)
    assert cose_audit_id(cose_bytes) == response_audit_id


def test_inspect_lifecycle_parses_cose_entries(tmp_path: Path) -> None:
    reg, aid, cfg, doc = _stage_scitt(tmp_path)
    handle_revoke(_req("REVOKE", aid, {"reason": "rotation"}), reg, doc)

    iresp = handle_inspect(
        _req("INSPECT", aid, {"target": "lifecycle", "agent_id": aid}),
        reg, doc,
    )
    assert iresp.status_code == 200
    body = json.loads(iresp.body_bytes)
    assert body["event_count"] == 1
    ev = body["events"][0]
    assert ev["format"] == "cose"
    assert ev["payload"]["extra"]["event_type"] == "revoke"
    assert ev["payload"]["extra"]["reason"] == "rotation"


def test_inspect_handles_mixed_jws_and_cose(tmp_path: Path) -> None:
    """A stream with both forms (e.g. mode flipped mid-stream) stays
    readable line-by-line."""
    reg, aid, cfg, doc = _stage_scitt(tmp_path)
    handle_revoke(_req("REVOKE", aid, {"reason": "first"}), reg, doc)

    # Hand-append a JWS-format line to simulate a pre-flip record.
    lifecycle_path = Path(cfg.audit.lifecycle_root) / f"{aid}.jsonl"
    record = cfg.signing_service.build_attribution_record(
        agent_id=aid, server_id="t.local", issued_at=utc_now_iso(),
        status=200, extra={
            "event_type": "activate",
            "previous_status": "retired", "new_status": "active",
        },
    )
    with lifecycle_path.open("a", encoding="ascii") as f:
        f.write(record.jws + "\n")

    iresp = handle_inspect(
        _req("INSPECT", aid, {"target": "lifecycle", "agent_id": aid}),
        reg, doc,
    )
    body = json.loads(iresp.body_bytes)
    assert body["event_count"] == 2
    formats = {ev["format"] for ev in body["events"]}
    assert formats == {"cose", "jws"}
