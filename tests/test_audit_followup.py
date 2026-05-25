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
