"""
Tests for the audit-follow-up spec conformance pass.

Covers the changes from the post-audit fix series:

  Batch A
    * Attribution-Record MUST-field completeness (audit_record_version,
      previous_audit_id zero sentinel, present-but-unknown sentinels).
    * verification_path vocab includes both ``org-asserted`` (Tier 2
      production) and ``self-signed`` (Tier 2 dev/local).

  Batch B
    * AgentDocument.trust_score range [0.0, 1.0]; out-of-range MUST
      be rejected; trust_score_computed_at MUST accompany populated
      trust_score; both elide when unpopulated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.identity import (
    AgentDocument, RequiresDeclaration,
    VALID_VERIFICATION_PATHS,
)


def _base_doc(**overrides):
    base = dict(
        agtp_version="1.0",
        agent_id="a" * 64,
        name="lauren",
        principal="Chris",
        principal_id="chris",
        description="",
        status="active",
        skills=["coffee"],
        requires=RequiresDeclaration(methods=["DISCOVER"]),
        scopes_accepted=[],
        issued_at="2026-05-25T00:00:00Z",
        issuer="self",
    )
    base.update(overrides)
    return AgentDocument(**base)


# ---------------------------------------------------------------------------
# Attribution-Record MUST-field completeness (Batch A).
# ---------------------------------------------------------------------------


def test_attribution_record_audit_record_version_always_present(
    tmp_path: Path,
) -> None:
    """Every record carries audit_record_version: "1" — both signed
    and unsigned builders, regardless of which optional fields the
    caller populated."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from server.signing import SigningService, parse_attribution_record

    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "signing.key"
    p.write_bytes(pem)
    svc = SigningService.from_key_path(str(p))

    signed = svc.build_attribution_record(
        server_id="t", issued_at="2026-05-25T00:00:00Z", status=200,
    )
    unsigned = svc.build_unsigned_attribution_record(
        server_id="t", issued_at="2026-05-25T00:00:00Z", status=200,
    )
    _, signed_payload, _ = parse_attribution_record(signed.jws)
    _, unsigned_payload, _ = parse_attribution_record(unsigned.jws)
    assert signed_payload["audit_record_version"] == "1"
    assert unsigned_payload["audit_record_version"] == "1"


def test_attribution_record_chain_head_uses_zero_sentinel(
    tmp_path: Path,
) -> None:
    """The chain-head sentinel is 64 zeros — not absent. Walkers
    terminate on the sentinel; tampering with the sentinel breaks
    the chain at its base."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from server.signing import SigningService, parse_attribution_record

    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "signing.key"
    p.write_bytes(pem)
    svc = SigningService.from_key_path(str(p))

    rec = svc.build_attribution_record(
        server_id="t", issued_at="2026-05-25T00:00:00Z", status=200,
        # previous_audit_id deliberately omitted
    )
    _, payload, _ = parse_attribution_record(rec.jws)
    assert payload["previous_audit_id"] == "0" * 64
    # Spec requires the sentinel to be exactly 64 chars of '0'.
    assert len(payload["previous_audit_id"]) == 64
    assert set(payload["previous_audit_id"]) == {"0"}


def test_attribution_record_must_fields_present_as_empty_sentinel(
    tmp_path: Path,
) -> None:
    """MUST fields (agent_id, owner_id, request_id, response_id)
    ride as empty strings when the daemon doesn't know them —
    present-but-unknown is distinct from absent. The five
    optional fields (principal_id, session_id, task_id, plus the
    optional 'extra' block) still drop when empty so verifiers
    can branch on 'field present'."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from server.signing import SigningService, parse_attribution_record

    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "signing.key"
    p.write_bytes(pem)
    svc = SigningService.from_key_path(str(p))

    rec = svc.build_attribution_record(
        server_id="t", issued_at="2026-05-25T00:00:00Z", status=200,
    )
    _, payload, _ = parse_attribution_record(rec.jws)
    # MUSTs present as empty.
    for key in ("agent_id", "owner_id", "request_id", "response_id"):
        assert key in payload
        assert payload[key] == ""
    # Optional fields drop when empty.
    for key in ("principal_id", "session_id", "task_id"):
        assert key not in payload


# ---------------------------------------------------------------------------
# verification_path vocab — both org-asserted and self-signed (Batch A).
# ---------------------------------------------------------------------------


def test_verification_path_admits_org_asserted() -> None:
    """org-asserted is the AGTP-TRUST canonical Tier 2 production
    value. AgentDocuments declaring it MUST load cleanly."""
    assert "org-asserted" in VALID_VERIFICATION_PATHS
    doc = _base_doc(verification_path="org-asserted")
    assert doc.verification_path == "org-asserted"


def test_verification_path_admits_self_signed_for_dev() -> None:
    """self-signed is the code-only marker for dev/local
    deployments where no organizational attestation exists.
    Treated as Tier 2 for trust posture purposes."""
    assert "self-signed" in VALID_VERIFICATION_PATHS
    doc = _base_doc(verification_path="self-signed")
    assert doc.verification_path == "self-signed"


def test_verification_path_admits_all_four_tier1_values() -> None:
    """The three Tier 1 production paths plus org-asserted comprise
    the AGTP-TRUST §verification-path canonical enum."""
    for path in ("dns-anchored", "log-anchored", "hybrid", "org-asserted"):
        assert path in VALID_VERIFICATION_PATHS


def test_verification_path_rejects_unknown() -> None:
    """Unknown values still raise on load — graceful demotion is
    a separate concern (item 30 in audit; not yet shipped)."""
    with pytest.raises(ValueError, match="verification_path"):
        _base_doc(verification_path="invented-path")


# ---------------------------------------------------------------------------
# trust_score (Batch B).
# ---------------------------------------------------------------------------


def test_trust_score_defaults_to_none() -> None:
    """The v00 daemon ships the slot but does not compute scores —
    default is None (omitted on serialization)."""
    doc = _base_doc()
    assert doc.trust_score is None
    assert doc.trust_score_computed_at == ""
    # Not in serialized output.
    out = doc.to_dict()
    assert "trust_score" not in out
    assert "trust_score_computed_at" not in out


def test_trust_score_accepts_valid_range() -> None:
    for score in (0.0, 0.5, 1.0, 0.99, 0.001):
        doc = _base_doc(
            trust_score=score,
            trust_score_computed_at="2026-05-25T00:00:00Z",
        )
        assert doc.trust_score == score


def test_trust_score_rejects_out_of_range() -> None:
    """AGTP-TRUST mandates [0.0, 1.0]; out-of-range MUST be
    rejected at construction."""
    for score in (-0.01, 1.01, 2.0, -1.0):
        with pytest.raises(ValueError, match="trust_score"):
            _base_doc(
                trust_score=score,
                trust_score_computed_at="2026-05-25T00:00:00Z",
            )


def test_trust_score_requires_computed_at() -> None:
    """Populated trust_score without trust_score_computed_at is
    meaningless to a relying party — refuse it."""
    with pytest.raises(ValueError, match="trust_score_computed_at"):
        _base_doc(trust_score=0.85)


def test_trust_score_round_trips_through_serialization() -> None:
    from core.identity import from_dict
    doc = _base_doc(
        trust_score=0.85,
        trust_score_computed_at="2026-05-25T00:00:00Z",
    )
    out = doc.to_dict()
    assert out["trust_score"] == 0.85
    assert out["trust_score_computed_at"] == "2026-05-25T00:00:00Z"
    # Round-trip back through from_dict.
    re_loaded = from_dict(out)
    assert re_loaded.trust_score == 0.85
    assert re_loaded.trust_score_computed_at == "2026-05-25T00:00:00Z"


# ---------------------------------------------------------------------------
# Header aliasing — Contract-Synthesized + Idempotency-Key (Batch C).
# ---------------------------------------------------------------------------


def _make_request(headers: dict):
    from core import wire
    return wire.AGTPRequest(
        method="QUERY", path="/",
        headers=headers, body_bytes=b"{}",
    )


def test_read_synthesis_id_prefers_contract_synthesized() -> None:
    """Spec-canonical Contract-Synthesized header takes precedence
    over the legacy Synthesis-Id name."""
    from core import wire
    req = _make_request({
        "Contract-Synthesized": "syn-spec",
        "Synthesis-Id": "syn-legacy",
    })
    assert wire.read_synthesis_id(req) == "syn-spec"


def test_read_synthesis_id_falls_back_to_legacy_with_warning() -> None:
    """Synthesis-Id alone still works but emits a deprecation
    warning. Mirrors the Target-Agent → Agent-ID precedent."""
    import warnings as _warnings
    from core import wire
    # Reset the one-shot guard so the warning fires for this test.
    wire._SYNTHESIS_HEADER_WARNED.clear()
    req = _make_request({"Synthesis-Id": "syn-legacy-only"})
    with _warnings.catch_warnings(record=True) as captured:
        _warnings.simplefilter("always")
        value = wire.read_synthesis_id(req)
    assert value == "syn-legacy-only"
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "Contract-Synthesized" in str(w.message)
        for w in captured
    )


def test_read_synthesis_id_warning_is_one_shot_per_value() -> None:
    """A retry of the same legacy id doesn't spam the log."""
    import warnings as _warnings
    from core import wire
    wire._SYNTHESIS_HEADER_WARNED.clear()
    req = _make_request({"Synthesis-Id": "syn-x"})
    with _warnings.catch_warnings(record=True) as captured:
        _warnings.simplefilter("always")
        wire.read_synthesis_id(req)
        wire.read_synthesis_id(req)
    deprecations = [
        w for w in captured
        if issubclass(w.category, DeprecationWarning)
    ]
    assert len(deprecations) == 1


def test_read_synthesis_id_returns_default_when_neither_set() -> None:
    from core import wire
    req = _make_request({})
    assert wire.read_synthesis_id(req) == ""
    assert wire.read_synthesis_id(req, default="none") == "none"


def test_read_idempotency_key_prefers_canonical_name() -> None:
    """Spec-canonical Idempotency-Key takes precedence over the
    RCNS-scoped legacy name."""
    from core import wire
    req = _make_request({
        "Idempotency-Key": "key-spec",
        "RCNS-Idempotency-Key": "key-legacy",
    })
    assert wire.read_idempotency_key(req) == "key-spec"


def test_read_idempotency_key_falls_back_to_legacy_with_warning() -> None:
    import warnings as _warnings
    from core import wire
    wire._IDEMPOTENCY_HEADER_WARNED.clear()
    req = _make_request({"RCNS-Idempotency-Key": "key-legacy-only"})
    with _warnings.catch_warnings(record=True) as captured:
        _warnings.simplefilter("always")
        value = wire.read_idempotency_key(req)
    assert value == "key-legacy-only"
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "Idempotency-Key" in str(w.message)
        for w in captured
    )


def test_rcns_gate_reads_canonical_idempotency_key_header() -> None:
    """End-to-end: the RCNS gate's idempotency cache works when the
    caller sends the spec-canonical Idempotency-Key header."""
    from unittest.mock import MagicMock
    from core import wire
    from core.identity import AgentDocument, RequiresDeclaration
    from server.config import (
        AuditConfig, RcnsConfig, ServerConfig, ServerInfo,
        ServerPolicy, SigningConfig, SynthesisConfig,
    )
    from server.rcns_gate import reset_state_for_tests, try_rcns
    from server.synthesis.runtime import SynthesisRuntime

    reset_state_for_tests()
    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        policy=ServerPolicy(synthesis_enabled=True),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(
            enabled=True, min_trust_tier=3,
            max_negotiations_per_minute=100,
        ),
        audit=AuditConfig(),
        signing=SigningConfig(),
    )
    runtime = SynthesisRuntime()
    state = MagicMock()
    state.config = cfg
    state.synthesis_runtime = runtime
    state.endpoint_registry = None
    doc = AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="x",
        principal="p", principal_id="pid", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["QUERY"], scopes=["rcns:negotiate"],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
        trust_tier=1,
    )
    body = b"{}"
    base_headers = {
        "Agent-ID": "a" * 64,
        "Content-Length": str(len(body)),
        "Allow-RCNS": "true",
    }

    # First request with spec-canonical Idempotency-Key.
    req1 = wire.AGTPRequest(
        method="QUERY", path="/things",
        headers={**base_headers, "Idempotency-Key": "key-canonical"},
        body_bytes=body,
    )
    r1 = try_rcns(req1, state, doc, method="QUERY", path="/things")

    # Second request with the LEGACY name, same key value.
    req2 = wire.AGTPRequest(
        method="QUERY", path="/things",
        headers={**base_headers, "RCNS-Idempotency-Key": "key-canonical"},
        body_bytes=body,
    )
    r2 = try_rcns(req2, state, doc, method="QUERY", path="/things")

    # Both should resolve to the same synthesis_id — proving both
    # header names hit the same cache entry.
    import json as _json
    sid1 = _json.loads(r1.body_bytes)["proposed_synthesis_id"]
    sid2 = _json.loads(r2.body_bytes)["proposed_synthesis_id"]
    assert sid1 == sid2


# ---------------------------------------------------------------------------
# AGTP-CERT operational MUSTs (Batch D).
# ---------------------------------------------------------------------------


def _verified_cert_with_extensions(
    agent_id: str = "a" * 64,
    principal_id: str = "chris@nomotic.inc",
):
    """Mock VerifiedCert with an AgentCertExtensions block populated."""
    from datetime import datetime, timezone
    from server.agent_cert_ext import AgentCertExtensions
    from server.mtls import VerifiedCert
    ext = AgentCertExtensions(
        subject_agent_id=agent_id,
        principal_id=principal_id,
    )
    now = datetime.now(tz=timezone.utc)
    return VerifiedCert(
        agent_id=agent_id,
        fingerprint="f" * 64,
        not_before=now, not_after=now,
        subject_common_name="lauren",
        extensions=ext,
    )


def test_cross_check_principal_id_passes_when_matching() -> None:
    from server.mtls import CertVerifier
    v = _verified_cert_with_extensions(principal_id="chris@nomotic.inc")
    # No raise = pass.
    CertVerifier.cross_check_principal_id_header(v, "chris@nomotic.inc")


def test_cross_check_principal_id_refuses_when_mismatched() -> None:
    from server.mtls import CertVerificationError, CertVerifier
    v = _verified_cert_with_extensions(principal_id="chris@nomotic.inc")
    with pytest.raises(CertVerificationError) as exc_info:
        CertVerifier.cross_check_principal_id_header(v, "imposter@evil.example")
    assert exc_info.value.detail == "principal-id-mismatch"


def test_cross_check_principal_id_is_noop_when_header_absent() -> None:
    """Caller didn't claim a Principal-ID; the daemon takes the
    cert-supplied value as authoritative without raising."""
    from server.mtls import CertVerifier
    v = _verified_cert_with_extensions(principal_id="chris@nomotic.inc")
    CertVerifier.cross_check_principal_id_header(v, "")


def test_cross_check_principal_id_is_noop_when_extension_absent() -> None:
    """Cert without a principal-id extension can't be cross-checked;
    the header takes precedence (with no contradicting source)."""
    from datetime import datetime, timezone
    from server.agent_cert_ext import AgentCertExtensions
    from server.mtls import CertVerifier, VerifiedCert
    now = datetime.now(tz=timezone.utc)
    v = VerifiedCert(
        agent_id="a" * 64, fingerprint="f" * 64,
        not_before=now, not_after=now,
        subject_common_name="lauren",
        extensions=AgentCertExtensions(),
    )
    CertVerifier.cross_check_principal_id_header(v, "chris@nomotic.inc")


def test_cert_session_registry_basic_register_and_lookup() -> None:
    from server.cert_sessions import CertSessionRegistry
    reg = CertSessionRegistry()
    reg.register(
        cert_serial="12345", session_id="sess-1",
        subject_agent_id="a" * 64,
    )
    reg.register(
        cert_serial="12345", session_id="sess-2",
        subject_agent_id="a" * 64,
    )
    reg.register(
        cert_serial="67890", session_id="sess-3",
        subject_agent_id="b" * 64,
    )
    assert reg.sessions_for_serial("12345") == ["sess-1", "sess-2"]
    assert reg.sessions_for_serial("67890") == ["sess-3"]
    assert reg.sessions_for_agent("a" * 64) == ["sess-1", "sess-2"]


def test_cert_session_registry_revoke_serial() -> None:
    from server.cert_sessions import CertSessionRegistry
    reg = CertSessionRegistry()
    reg.register(
        cert_serial="12345", session_id="sess-1",
        subject_agent_id="a" * 64,
    )
    terminated = reg.revoke_serial("12345")
    assert terminated == ["sess-1"]
    assert reg.sessions_for_serial("12345") == []


def test_cert_session_registry_revoke_agent_sweeps_rotated_certs() -> None:
    """Cert rotation produces multiple serials authorizing the same
    agent. Agent-wide revocation sweeps them all."""
    from server.cert_sessions import CertSessionRegistry
    reg = CertSessionRegistry()
    reg.register(
        cert_serial="111", session_id="sess-a",
        subject_agent_id="a" * 64,
    )
    reg.register(
        cert_serial="222", session_id="sess-b",
        subject_agent_id="a" * 64,
    )
    reg.register(
        cert_serial="333", session_id="sess-c",
        subject_agent_id="b" * 64,
    )
    terminated = reg.revoke_agent("a" * 64)
    assert set(terminated) == {"sess-a", "sess-b"}
    # b * 64's session is untouched.
    assert reg.sessions_for_serial("333") == ["sess-c"]


def test_revocation_notify_envelope_shape() -> None:
    """The envelope matches AGTP-CERT §6.2 byte-for-byte so any
    emitter produces the same wire shape."""
    from server.cert_sessions import build_revocation_notify_envelope
    env = build_revocation_notify_envelope(
        subject_agent_id="a" * 64,
        cert_serial="12345",
        reason="key-compromise",
        revoked_at="2026-05-25T00:00:00Z",
        issuer="ca.example",
    )
    assert env["event_type"] == "certificate_revoked"
    assert env["recipient"] == "infrastructure:broadcast"
    assert env["urgency"] == "critical"
    assert env["payload"]["subject_agent_id"] == "a" * 64
    assert env["payload"]["cert_serial"] == "12345"
    assert env["payload"]["reason"] == "key-compromise"


def test_apply_revocation_notify_terminates_sessions() -> None:
    """End-to-end: receive a revocation envelope, sweep the
    registry, return a structured summary."""
    from server.cert_sessions import (
        CertSessionRegistry,
        apply_revocation_notify,
        build_revocation_notify_envelope,
    )
    reg = CertSessionRegistry()
    reg.register(
        cert_serial="12345", session_id="sess-x",
        subject_agent_id="a" * 64,
    )
    reg.register(
        cert_serial="12345", session_id="sess-y",
        subject_agent_id="a" * 64,
    )
    envelope = build_revocation_notify_envelope(
        subject_agent_id="a" * 64, cert_serial="12345",
    )
    summary = apply_revocation_notify(envelope, reg)
    assert set(summary["terminated_sessions"]) == {"sess-x", "sess-y"}
    assert summary["serials_swept"] == ["12345"]
    # Registry is empty afterwards.
    assert len(reg) == 0


def test_apply_revocation_notify_ignores_non_certificate_revoked() -> None:
    """Wrong event_type → no-op, returns empty summary."""
    from server.cert_sessions import (
        CertSessionRegistry, apply_revocation_notify,
    )
    reg = CertSessionRegistry()
    reg.register(
        cert_serial="12345", session_id="sess-x",
        subject_agent_id="a" * 64,
    )
    summary = apply_revocation_notify(
        {"event_type": "agent_lifecycle_revoked", "payload": {}},
        reg,
    )
    assert summary["terminated_sessions"] == []
    assert len(reg) == 1  # untouched


def test_trust_score_field_order_in_serialization() -> None:
    """trust_score sits between trust_warning and owner_id in the
    canonical key order so agent.json files stay byte-stable."""
    doc = _base_doc(
        trust_score=0.85,
        trust_score_computed_at="2026-05-25T00:00:00Z",
    )
    keys = list(doc.to_dict().keys())
    # Find the relative position of trust_score; it must come
    # after trust_warning (if present) and before owner_id (if
    # present). For our doc, trust_warning is auto-populated for
    # Tier 2 and owner_id is empty (omitted). So just check
    # trust_score is between trust_warning and issued_at.
    assert keys.index("trust_score") > keys.index("trust_warning")
    assert keys.index("trust_score") < keys.index("issued_at")
    assert keys.index("trust_score_computed_at") == keys.index("trust_score") + 1
