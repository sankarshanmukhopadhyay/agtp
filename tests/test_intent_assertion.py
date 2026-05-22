"""
Tests for agtp.intent — the Intent Assertion JWT helper handlers
use to mint signed purchase intents.

The helper consumes a DaemonClient (the gateway-protocol Phase C
abstraction) for signatures. Tests provide a stub DaemonClient
backed by a real Ed25519 key so the resulting JWTs verify against a
known public key.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agtp.handlers import DaemonError
from agtp.intent import (
    DEFAULT_TTL_SECONDS,
    IntentAssertionError,
    build_intent_assertion,
    fresh_jti,
    parse_intent_assertion,
)


class _LocalDaemon:
    """Stand-in for the gateway DaemonClient. Holds a real Ed25519
    key and signs on demand. The test verifies against the matching
    public key."""

    def __init__(self, key: Ed25519PrivateKey | None = None) -> None:
        self.key = key or Ed25519PrivateKey.generate()
        self.calls = 0

    def sign(self, data: bytes) -> bytes:
        self.calls += 1
        return self.key.sign(data)

    def fetch(self, *a, **kw):
        raise NotImplementedError


class _FailingDaemon:
    def sign(self, data: bytes) -> bytes:
        raise DaemonError("signing service unavailable", code="signing_unavailable")

    def fetch(self, *a, **kw):
        raise NotImplementedError


def _required_args() -> dict:
    return dict(
        issuer="agent-buyer",
        subject="chris@example.com",
        audience="agent-merchant",
        amount="9.99",
        currency="USD",
        merchant_id="agent-merchant",
        product_ref="sku:coffee-monthly",
    )


# ---------------------------------------------------------------------------
# fresh_jti.
# ---------------------------------------------------------------------------


def test_fresh_jti_is_unique() -> None:
    a = fresh_jti()
    b = fresh_jti()
    assert a != b
    assert len(a) == 32  # 128 bits as hex


# ---------------------------------------------------------------------------
# build_intent_assertion.
# ---------------------------------------------------------------------------


def test_build_returns_jwt_jti_exp() -> None:
    daemon = _LocalDaemon()
    out = build_intent_assertion(daemon=daemon, **_required_args())
    assert "jwt" in out and "jti" in out and "exp" in out
    assert out["jwt"].count(".") == 2
    assert daemon.calls == 1


def test_round_trip_jwt_parses_and_verifies() -> None:
    daemon = _LocalDaemon()
    out = build_intent_assertion(daemon=daemon, **_required_args())
    header, payload, signature = parse_intent_assertion(out["jwt"])
    assert header == {"alg": "EdDSA", "typ": "JWT"}
    assert payload["jti"] == out["jti"]
    assert payload["amount"] == "9.99"
    assert payload["currency"] == "USD"
    assert payload["merchant_id"] == "agent-merchant"
    assert payload["product_ref"] == "sku:coffee-monthly"
    # The signature verifies against the daemon's public key.
    parts = out["jwt"].split(".")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    daemon.key.public_key().verify(signature, signing_input)


def test_exp_defaults_to_300_seconds() -> None:
    daemon = _LocalDaemon()
    before = int(time.time())
    out = build_intent_assertion(daemon=daemon, **_required_args())
    _, payload, _ = parse_intent_assertion(out["jwt"])
    assert payload["exp"] - payload["iat"] == DEFAULT_TTL_SECONDS
    assert payload["iat"] >= before


def test_ttl_override() -> None:
    daemon = _LocalDaemon()
    out = build_intent_assertion(daemon=daemon, ttl_seconds=60, **_required_args())
    _, payload, _ = parse_intent_assertion(out["jwt"])
    assert payload["exp"] - payload["iat"] == 60


def test_key_id_lands_in_header() -> None:
    daemon = _LocalDaemon()
    out = build_intent_assertion(
        daemon=daemon, key_id="ed25519-abc123", **_required_args(),
    )
    header, _, _ = parse_intent_assertion(out["jwt"])
    assert header["kid"] == "ed25519-abc123"


def test_extra_claims_overlay() -> None:
    daemon = _LocalDaemon()
    out = build_intent_assertion(
        daemon=daemon,
        extra_claims={"policy_id": "pol-42", "approval_token": "tok-x"},
        **_required_args(),
    )
    _, payload, _ = parse_intent_assertion(out["jwt"])
    assert payload["policy_id"] == "pol-42"
    assert payload["approval_token"] == "tok-x"


def test_extra_claims_cannot_override_structural() -> None:
    daemon = _LocalDaemon()
    with pytest.raises(IntentAssertionError):
        build_intent_assertion(
            daemon=daemon,
            extra_claims={"amount": "0.01"},  # try to underprice
            **_required_args(),
        )


def test_missing_required_field_rejected() -> None:
    daemon = _LocalDaemon()
    args = _required_args()
    args["amount"] = ""
    with pytest.raises(IntentAssertionError):
        build_intent_assertion(daemon=daemon, **args)


def test_negative_ttl_rejected() -> None:
    daemon = _LocalDaemon()
    with pytest.raises(IntentAssertionError):
        build_intent_assertion(
            daemon=daemon, ttl_seconds=0, **_required_args(),
        )


def test_daemon_signing_failure_surfaces() -> None:
    daemon = _FailingDaemon()
    with pytest.raises(IntentAssertionError) as exc_info:
        build_intent_assertion(daemon=daemon, **_required_args())
    assert "daemon refused" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Independent verification — the whole point of Intent Assertions.
# ---------------------------------------------------------------------------


def test_jwt_verifies_with_just_the_public_key() -> None:
    """A payment network that doesn't speak AGTP holds the buyer's
    public key and verifies the JWT independently. This is the bridge
    the Intent Assertion provides."""
    daemon = _LocalDaemon()
    pubkey = daemon.key.public_key()
    out = build_intent_assertion(daemon=daemon, **_required_args())

    parts = out["jwt"].split(".")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    sig_padded = parts[2] + "=" * (-len(parts[2]) % 4)
    signature = base64.urlsafe_b64decode(sig_padded)

    # Verifies — payment network is happy.
    pubkey.verify(signature, signing_input)


def test_tampered_amount_fails_verification() -> None:
    daemon = _LocalDaemon()
    pubkey = daemon.key.public_key()
    out = build_intent_assertion(daemon=daemon, **_required_args())

    parts = out["jwt"].split(".")
    # Rewrite the payload to claim $0.01.
    tampered_payload = base64.urlsafe_b64encode(
        json.dumps({"amount": "0.01"}, separators=(",", ":")).encode("utf-8"),
    ).rstrip(b"=").decode("ascii")
    signing_input = f"{parts[0]}.{tampered_payload}".encode("ascii")
    sig_padded = parts[2] + "=" * (-len(parts[2]) % 4)
    signature = base64.urlsafe_b64decode(sig_padded)

    with pytest.raises(InvalidSignature):
        pubkey.verify(signature, signing_input)
