"""
Intent Assertion helpers for handler authors.

An **Intent Assertion** is a short-lived signed JWT a buyer agent
emits alongside a PURCHASE to commit, in writing, to the financial
intent. It rides on the wire purely as application content — the
daemon doesn't issue Intent Assertions, the **handler** does. The
daemon's role is limited to providing the signing primitive via
``EndpointContext.daemon.sign(bytes)`` (the gateway protocol's
``sign_request`` capability landed in Phase C).

This module is a convenience layer for handlers that need to mint
Intent Assertions. The wire shape is JWS Compact Serialization
(RFC 7515) with EdDSA — the same family Attribution-Record uses, so
verifiers handle both with one library.

## Why the daemon doesn't own this

A previous design draft considered making the daemon the Intent
Assertion issuer. We pulled back: handlers know what they're
asserting (a specific purchase intent against a specific catalog
item at a specific price); the daemon doesn't. Making the daemon
the issuer would force it to grow a domain-aware payload builder,
which is application logic. The daemon stays signer-not-issuer.

The split is small but principled:

    [handler]   builds payload {iss, sub, aud, jti, iat, exp, ...}
    [handler]   asks daemon for an Ed25519 signature over the
                canonical signing input
    [daemon]    signs and returns the raw signature bytes (no
                knowledge of what was signed)
    [handler]   assembles the compact JWT, returns it to the buyer

A payment network downstream verifies the JWT independently — it
doesn't speak AGTP. The Intent Assertion is the bridge to
traditional commerce rails.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from agtp.handlers import DaemonClient, DaemonError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )


JWT_ALG_EDDSA = "EdDSA"
JWT_TYP = "JWT"
DEFAULT_TTL_SECONDS = 300  # 5 minutes — Intent Assertions are short-lived


class IntentAssertionError(Exception):
    """Raised when an Intent Assertion can't be built or signed."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def fresh_jti() -> str:
    """Mint a fresh JWT ID — 128 bits of entropy, hex-encoded.

    Used as the ``jti`` claim on every Intent Assertion. Also the
    value the buyer puts in ``attribution_extra["intent_assertion_jti"]``
    so the receipt chain references the assertion."""
    return secrets.token_hex(16)


def build_intent_assertion(
    *,
    daemon: DaemonClient,
    issuer: str,
    subject: str,
    audience: str,
    amount: str,
    currency: str,
    merchant_id: str,
    product_ref: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    extra_claims: Optional[Dict[str, Any]] = None,
    key_id: str = "",
) -> Dict[str, Any]:
    """Build, sign, and return an Intent Assertion JWT.

    The daemon signs via :meth:`DaemonClient.sign`; the handler never
    touches a private key. Returns a dict::

        {
          "jwt": "<header>.<payload>.<signature>",
          "jti": "<128-bit hex>",
          "exp": <unix-seconds>,
        }

    Handlers typically also stash ``jti`` in
    ``EndpointResponse.attribution_extra`` so the audit chain
    references it. Pseudo-code::

        from agtp.intent import build_intent_assertion, fresh_jti

        def buy_subscription(ctx):
            asn = build_intent_assertion(
                daemon=ctx.daemon,
                issuer=ctx.agent_id,
                subject=ctx.principal_id,
                audience=merchant_agent_id,
                amount="9.99", currency="USD",
                merchant_id=merchant_agent_id,
                product_ref="sku:coffee-subscription-monthly",
            )
            # Send AGTP PURCHASE with the JWT in the body, jti in
            # attribution_extra so it lands in the audit chain.
            return EndpointResponse(
                body={"intent_assertion": asn["jwt"], ...},
                attribution_extra={"intent_assertion_jti": asn["jti"]},
            )

    Required claims (mandatory per
    ``draft-hood-agtp-merchant-identity-02 §6``):

      * ``iss``   issuer — the buyer's Canonical Agent-ID
      * ``sub``   subject — the principal authorizing the spend
      * ``aud``   audience — the merchant's Canonical Agent-ID
      * ``jti``   unique JWT id (fresh per assertion)
      * ``iat``   issued at (unix seconds)
      * ``exp``   expiry (unix seconds; defaults to iat + 300)
      * ``amount`` / ``currency``  money amount as a string
                                   (decimal precision preserved)
      * ``merchant_id`` redundant with ``aud`` but kept explicit so
                       payment networks that don't parse ``aud``
                       still see the merchant
      * ``product_ref`` opaque reference to the product/service

    ``extra_claims`` overlays additional fields onto the payload —
    use for governance metadata (``policy_id``, ``approval_token``,
    etc.) that's deployment-specific.
    """
    if not all([issuer, subject, audience, amount, currency, merchant_id, product_ref]):
        raise IntentAssertionError(
            "issuer, subject, audience, amount, currency, merchant_id, "
            "and product_ref are all required"
        )
    if ttl_seconds <= 0:
        raise IntentAssertionError("ttl_seconds must be positive")

    now = int(time.time())
    jti = fresh_jti()
    payload: Dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "jti": jti,
        "iat": now,
        "exp": now + ttl_seconds,
        "amount": amount,
        "currency": currency,
        "merchant_id": merchant_id,
        "product_ref": product_ref,
    }
    if extra_claims:
        # Don't let extras stomp the structural claims — refuse and
        # let the handler decide rather than silently overriding.
        clobbered = set(extra_claims) & set(payload)
        if clobbered:
            raise IntentAssertionError(
                f"extra_claims must not override structural claims: "
                f"{sorted(clobbered)}"
            )
        payload.update(extra_claims)

    header: Dict[str, Any] = {"alg": JWT_ALG_EDDSA, "typ": JWT_TYP}
    if key_id:
        header["kid"] = key_id

    protected_b64 = _b64url(_canonical(header).encode("utf-8"))
    payload_b64 = _b64url(_canonical(payload).encode("utf-8"))
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    try:
        signature = daemon.sign(signing_input)
    except DaemonError as exc:
        raise IntentAssertionError(
            f"daemon refused to sign Intent Assertion: {exc}"
        ) from exc
    jwt_compact = f"{protected_b64}.{payload_b64}.{_b64url(signature)}"
    return {"jwt": jwt_compact, "jti": jti, "exp": payload["exp"]}


def parse_intent_assertion(jwt_compact: str) -> tuple[Dict[str, Any], Dict[str, Any], bytes]:
    """Parse a compact-form Intent Assertion without verifying it.

    Returns ``(header, payload, signature_bytes)``. Verifiers
    combine this with the issuer's public key to validate. Same
    structural shape as Attribution-Records, so a single JWS parser
    handles both.
    """
    parts = jwt_compact.split(".")
    if len(parts) != 3:
        raise IntentAssertionError(
            f"Intent Assertion must have three segments, got {len(parts)}"
        )
    pad = lambda s: s + "=" * (-len(s) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(pad(parts[0])).decode("utf-8"))
        payload = json.loads(base64.urlsafe_b64decode(pad(parts[1])).decode("utf-8"))
        signature = base64.urlsafe_b64decode(pad(parts[2])) if parts[2] else b""
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntentAssertionError(
            f"Intent Assertion segments are not valid JWS: {exc}"
        ) from exc
    return header, payload, signature


def verify_intent_assertion(
    jwt_compact: str,
    *,
    issuer_public_key: "Ed25519PublicKey",
    expected_audience: Optional[str] = None,
    expected_merchant_id: Optional[str] = None,
    leeway_seconds: int = 60,
) -> Dict[str, Any]:
    """Verify an Intent Assertion's signature and standard claims.

    Added alongside the governance/security hardening pass: the
    reference implementation shipped ``build_intent_assertion`` and
    ``parse_intent_assertion`` but no verifier at all — a handler
    (or downstream payment network) that wanted to confirm an
    Intent Assertion was genuine, current, and addressed to it had
    nothing to call. This closes that gap for the general case;
    ``mod_merchant`` additionally offers header-only ``jti`` replay
    detection (see ``mod_merchant.replay_store``) that doesn't need
    the issuer's public key and runs at the wire edge before the
    body is parsed.

    Checks, in order:

      1. Structural + signature: three dot-separated JWS segments,
         ``alg`` must be ``EdDSA``, signature must verify against
         ``issuer_public_key``.
      2. ``exp`` must be in the future (modulo ``leeway_seconds``).
      3. ``nbf``, when present, must be in the past (modulo
         ``leeway_seconds``). Not required by
         ``build_intent_assertion`` (which doesn't set it), but
         honored when a caller supplies one.
      4. ``aud``, when ``expected_audience`` is supplied, must match.
      5. ``merchant_id``, when ``expected_merchant_id`` is supplied,
         must match — redundant with ``aud`` in the common case but
         checked independently since the two fields are independently
         settable.

    Returns the decoded payload on success. Raises
    :class:`IntentAssertionError` on any failure; the exception's
    ``reason`` attribute is a stable tag (mirrors
    ``server.oauth_context.OAuthValidationError``'s vocabulary):
    ``invalid-signature``, ``expired``, ``not-yet-valid``,
    ``audience-mismatch``, ``merchant-mismatch``, ``malformed``.

    This function does NOT check ``jti`` uniqueness — replay
    detection needs a store of previously-seen values, which is a
    deployment concern (see ``mod_merchant.replay_store
    .SeenJtiStore``), not something a stateless verifier can do.
    """
    from cryptography.exceptions import InvalidSignature

    try:
        header, payload, signature = parse_intent_assertion(jwt_compact)
    except IntentAssertionError as exc:
        exc.reason = "malformed"  # type: ignore[attr-defined]
        raise

    alg = str(header.get("alg") or "")
    if alg != JWT_ALG_EDDSA:
        err = IntentAssertionError(
            f"Intent Assertion alg must be {JWT_ALG_EDDSA!r}, got {alg!r}"
        )
        err.reason = "malformed"  # type: ignore[attr-defined]
        raise err

    parts = jwt_compact.split(".")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    try:
        issuer_public_key.verify(signature, signing_input)
    except InvalidSignature as exc:
        err = IntentAssertionError("Intent Assertion signature did not verify")
        err.reason = "invalid-signature"  # type: ignore[attr-defined]
        raise err from exc

    now = int(time.time())
    exp = payload.get("exp")
    if exp is None or int(exp) + leeway_seconds < now:
        err = IntentAssertionError(
            f"Intent Assertion exp is missing or in the past (exp={exp!r})"
        )
        err.reason = "expired"  # type: ignore[attr-defined]
        raise err

    nbf = payload.get("nbf")
    if nbf is not None and int(nbf) - leeway_seconds > now:
        err = IntentAssertionError("Intent Assertion nbf is in the future")
        err.reason = "not-yet-valid"  # type: ignore[attr-defined]
        raise err

    if expected_audience is not None and payload.get("aud") != expected_audience:
        err = IntentAssertionError(
            f"aud {payload.get('aud')!r} does not match expected "
            f"{expected_audience!r}"
        )
        err.reason = "audience-mismatch"  # type: ignore[attr-defined]
        raise err

    if (
        expected_merchant_id is not None
        and payload.get("merchant_id") != expected_merchant_id
    ):
        err = IntentAssertionError(
            f"merchant_id {payload.get('merchant_id')!r} does not match "
            f"expected {expected_merchant_id!r}"
        )
        err.reason = "merchant-mismatch"  # type: ignore[attr-defined]
        raise err

    return payload


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "IntentAssertionError",
    "JWT_ALG_EDDSA",
    "build_intent_assertion",
    "fresh_jti",
    "parse_intent_assertion",
    "verify_intent_assertion",
]
