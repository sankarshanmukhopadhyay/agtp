"""
Tests for ``agtp.intent.verify_intent_assertion`` — the signature +
claims verifier added alongside the F4 governance finding (the
reference implementation shipped a builder and a structural parser
for Intent Assertions but no verifier at all).

Reuses the ``_LocalDaemon`` fixture pattern from
tests/test_intent_assertion.py to mint real signed assertions, then
exercises the verifier against a matching key, a wrong key, an
expired assertion, an audience mismatch, and a merchant_id mismatch.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agtp.intent import (
    IntentAssertionError,
    build_intent_assertion,
    verify_intent_assertion,
)


class _LocalDaemon:
    def __init__(self, key=None) -> None:
        self.key = key or Ed25519PrivateKey.generate()

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data)

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


def _mint(daemon: _LocalDaemon, **overrides) -> str:
    args = _required_args()
    args.update(overrides)
    asn = build_intent_assertion(daemon=daemon, **args)
    return asn["jwt"]


def test_verify_succeeds_with_matching_key() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon)
    payload = verify_intent_assertion(
        jwt, issuer_public_key=daemon.key.public_key(),
    )
    assert payload["merchant_id"] == "agent-merchant"


def test_verify_rejects_wrong_key() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon)
    wrong_key = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(IntentAssertionError) as exc_info:
        verify_intent_assertion(jwt, issuer_public_key=wrong_key)
    assert exc_info.value.reason == "invalid-signature"


def test_verify_rejects_expired_assertion() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon, ttl_seconds=1)
    # int(time.time()) truncation on both the exp claim and the
    # "now" comparison can eat up to ~1s of the nominal margin
    # depending on where the mint and the check land relative to
    # the second boundary; sleep comfortably past worst case rather
    # than the bare ttl_seconds + a sliver.
    time.sleep(2.5)
    with pytest.raises(IntentAssertionError) as exc_info:
        verify_intent_assertion(
            jwt, issuer_public_key=daemon.key.public_key(),
            leeway_seconds=0,
        )
    assert exc_info.value.reason == "expired"


def test_verify_accepts_expired_within_leeway() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon, ttl_seconds=1)
    time.sleep(2.5)
    payload = verify_intent_assertion(
        jwt, issuer_public_key=daemon.key.public_key(),
        leeway_seconds=60,
    )
    assert payload["jti"]


def test_verify_rejects_audience_mismatch() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon)
    with pytest.raises(IntentAssertionError) as exc_info:
        verify_intent_assertion(
            jwt, issuer_public_key=daemon.key.public_key(),
            expected_audience="someone-else",
        )
    assert exc_info.value.reason == "audience-mismatch"


def test_verify_accepts_matching_audience() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon)
    payload = verify_intent_assertion(
        jwt, issuer_public_key=daemon.key.public_key(),
        expected_audience="agent-merchant",
    )
    assert payload["aud"] == "agent-merchant"


def test_verify_rejects_merchant_id_mismatch() -> None:
    daemon = _LocalDaemon()
    jwt = _mint(daemon)
    with pytest.raises(IntentAssertionError) as exc_info:
        verify_intent_assertion(
            jwt, issuer_public_key=daemon.key.public_key(),
            expected_merchant_id="a-different-merchant",
        )
    assert exc_info.value.reason == "merchant-mismatch"


def test_verify_rejects_malformed_jwt() -> None:
    daemon = _LocalDaemon()
    with pytest.raises(IntentAssertionError) as exc_info:
        verify_intent_assertion(
            "not.a.validjwt", issuer_public_key=daemon.key.public_key(),
        )
    assert exc_info.value.reason == "malformed"


def test_verify_does_not_check_jti_uniqueness() -> None:
    """Documents the boundary: this verifier is stateless and
    replaying the exact same (still-unexpired) assertion twice
    verifies twice. jti replay detection is a deployment concern —
    see mod_merchant.replay_store.SeenJtiStore — not something a
    stateless verifier can do."""
    daemon = _LocalDaemon()
    jwt = _mint(daemon)
    first = verify_intent_assertion(jwt, issuer_public_key=daemon.key.public_key())
    second = verify_intent_assertion(jwt, issuer_public_key=daemon.key.public_key())
    assert first["jti"] == second["jti"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__]))
