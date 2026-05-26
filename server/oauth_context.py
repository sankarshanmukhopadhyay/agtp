"""
OAuth/OIDC composition — token extraction and validation hooks.

Pattern 2 of the AGTP-identity-composition story (see
``docs/oauth-composition.md``): AGTP identifies *which agent* is
making the call (the wire layer's Agent-ID / cert); an OAuth bearer
token carried in the standard HTTP ``Authorization: Bearer <token>``
header identifies *which principal* the agent is acting on behalf
of (the application layer). The two are orthogonal — Agent-ID
answers "who is asking?", OAuth principal answers "for whom?".

This module is intentionally narrow:

  * :func:`extract_token` parses the Authorization header and
    returns the bearer token (or ``None``). Other auth schemes
    (Basic, Digest) pass through untouched — they're not OAuth and
    AGTP doesn't interpret them.
  * :class:`OAuthValidator` is the ABC operators plug their token
    validation into. Two reference implementations ship:
    :class:`NoOpValidator` accepts everything (sanity-test + early-
    integration use), :class:`JWTValidator` checks an Ed25519 / RSA
    signature against a configured public key, plus the standard
    ``exp`` / ``nbf`` / ``iat`` time bounds.
  * :class:`OAuthValidationError` is the structured failure type.
    ``reason`` is a stable tag the dispatcher relays into the 401
    response body so clients can branch on "no token" vs "bad
    signature" vs "expired" without parsing text.

Token opacity is the load-bearing design property — AGTP forwards
or validates tokens, it does NOT interpret claims beyond the
operator-configured ``principal_id_claim``. The validator interface
is the extension point; full OIDC discovery, introspection (RFC
7662), and refresh-token flows are operator territory.
"""

from __future__ import annotations

import base64
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from core import wire


# ---------------------------------------------------------------------------
# Token extraction.
# ---------------------------------------------------------------------------


_BEARER_PREFIX = "bearer "


def extract_token(request: wire.AGTPRequest) -> Optional[str]:
    """Return the bearer token from the request's Authorization
    header, or ``None``.

    Recognizes the standard ``Authorization: Bearer <token>`` form.
    Other schemes (``Basic``, ``Digest``, anything else) return
    ``None`` — AGTP doesn't interpret them. Whitespace around the
    token is stripped. The scheme name is matched
    case-insensitively per RFC 7235 §2.1.
    """
    raw = wire.header(request, "Authorization")
    if not raw:
        return None
    text = raw.strip()
    if len(text) <= len(_BEARER_PREFIX):
        return None
    if text[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token = text[len(_BEARER_PREFIX) :].strip()
    return token or None


# ---------------------------------------------------------------------------
# Validator interface + structured failure.
# ---------------------------------------------------------------------------


class OAuthValidationError(Exception):
    """Structured token-validation failure.

    ``reason`` is the stable tag the dispatcher echoes into the 401
    response body (``error.reason``). Vocabulary:

      * ``oauth-invalid-signature`` — signature didn't verify.
      * ``oauth-expired`` — ``exp`` claim in the past.
      * ``oauth-not-yet-valid`` — ``nbf`` claim in the future.
      * ``oauth-malformed`` — token doesn't parse as the expected
        shape (e.g., not three dot-separated segments for JWT).
      * ``oauth-unknown-issuer`` — validator doesn't recognize the
        signing party.
      * ``oauth-invalid`` — catch-all when none of the above fits.
    """

    def __init__(self, message: str, *, reason: str = "oauth-invalid") -> None:
        super().__init__(message)
        self.reason = reason


class OAuthValidator(ABC):
    """Plug-in surface for OAuth token validation.

    Operators register custom validators (OIDC introspection, opaque-
    token introspection per RFC 7662, vendor-specific token formats)
    by subclassing this and registering the class with
    :func:`register_validator`.

    The contract is narrow on purpose: take a token, return the
    claims dict on success, raise :class:`OAuthValidationError` on
    failure. Anything richer (token caching, refresh, revocation
    checks) lives inside the validator implementation, not in this
    interface.
    """

    @abstractmethod
    def validate(self, token: str) -> Dict[str, Any]:
        """Return the validated token's claims dict.

        Raises :class:`OAuthValidationError` on any failure — the
        dispatcher catches and surfaces the structured reason on
        the 401 response.
        """


# ---------------------------------------------------------------------------
# NoOp validator — accepts every well-formed token.
# ---------------------------------------------------------------------------


class NoOpValidator(OAuthValidator):
    """Accepts any non-empty token and returns its claims dict if
    the token is a JWT, or an empty dict otherwise.

    Use this for early integration testing and as a sanity-test
    baseline. It MUST NOT be used in production — a no-op
    validator means anyone presenting any string gets through.
    The dispatcher logs a warning at boot when this validator is
    active alongside ``oauth.enabled = true``.
    """

    name = "noop"

    def validate(self, token: str) -> Dict[str, Any]:
        if not token:
            raise OAuthValidationError(
                "no token presented", reason="oauth-malformed",
            )
        # Best-effort claims extraction so test fixtures can put
        # claims into a no-op token and have them surface as the
        # acting_principal_id. Non-JWT tokens return an empty
        # claims dict — caller treats absent claims as "no
        # principal_id lifted."
        parts = token.split(".")
        if len(parts) >= 2:
            try:
                claims = _decode_jwt_payload(parts[1])
                if isinstance(claims, dict):
                    return claims
            except (ValueError, json.JSONDecodeError):
                pass
        return {}


# ---------------------------------------------------------------------------
# JWT validator — signature + standard time bounds.
# ---------------------------------------------------------------------------


class JWTValidator(OAuthValidator):
    """Validates a JWT (RFC 7519) against an operator-configured
    public key.

    Configuration (passed to :meth:`__init__` from the
    ``validator_config`` block):

      * ``public_key`` — PEM- or b64url-of-raw-bytes-encoded public
        key. The supported algorithms depend on the key type;
        Ed25519 → EdDSA, RSA → RS256/RS384/RS512.
      * ``allowed_algs`` — list of acceptable ``alg`` header values
        (defaults to ``["EdDSA"]``).
      * ``expected_issuer`` — optional ``iss`` claim the token MUST
        carry.
      * ``expected_audience`` — optional ``aud`` claim the token
        MUST carry (string or list of acceptable values).
      * ``leeway_seconds`` — clock-skew tolerance for time-bound
        checks (default 60).

    Time-bound checks honor the standard JWT claims: ``exp`` MUST
    be in the future (modulo leeway), ``nbf`` MUST be in the past
    (modulo leeway), ``iat`` is recorded but not enforced.
    """

    name = "jwt"

    def __init__(self, config: Dict[str, Any]) -> None:
        public_key_value = str(config.get("public_key") or "").strip()
        if not public_key_value:
            raise ValueError(
                "JWTValidator requires a 'public_key' in validator_config"
            )
        self._public_key = _load_public_key_any(public_key_value)
        self._allowed_algs = set(
            config.get("allowed_algs") or ["EdDSA"]
        )
        self._expected_issuer = config.get("expected_issuer") or ""
        aud = config.get("expected_audience")
        if isinstance(aud, str):
            aud = [aud] if aud else []
        self._expected_audience = list(aud or [])
        self._leeway_seconds = int(config.get("leeway_seconds") or 60)

    def validate(self, token: str) -> Dict[str, Any]:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.asymmetric.rsa import (
            RSAPublicKey,
        )

        parts = token.split(".")
        if len(parts) != 3:
            raise OAuthValidationError(
                "JWT must be three dot-separated segments",
                reason="oauth-malformed",
            )
        header_b64, payload_b64, signature_b64 = parts
        try:
            header = _decode_jwt_payload(header_b64)
            payload = _decode_jwt_payload(payload_b64)
        except (ValueError, json.JSONDecodeError) as exc:
            raise OAuthValidationError(
                f"JWT segments did not parse: {exc}",
                reason="oauth-malformed",
            ) from exc

        alg = str(header.get("alg") or "")
        if alg not in self._allowed_algs:
            raise OAuthValidationError(
                f"alg {alg!r} is not in the configured allowed_algs",
                reason="oauth-malformed",
            )

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        try:
            signature = _b64url_decode(signature_b64)
        except ValueError as exc:
            raise OAuthValidationError(
                f"signature segment is not valid base64url: {exc}",
                reason="oauth-malformed",
            ) from exc

        try:
            if isinstance(self._public_key, Ed25519PublicKey):
                if alg != "EdDSA":
                    raise OAuthValidationError(
                        "Ed25519 key requires alg=EdDSA",
                        reason="oauth-malformed",
                    )
                self._public_key.verify(signature, signing_input)
            elif isinstance(self._public_key, RSAPublicKey):
                algo_map = {
                    "RS256": hashes.SHA256(),
                    "RS384": hashes.SHA384(),
                    "RS512": hashes.SHA512(),
                }
                if alg not in algo_map:
                    raise OAuthValidationError(
                        f"RSA key does not accept alg {alg!r}",
                        reason="oauth-malformed",
                    )
                self._public_key.verify(
                    signature,
                    signing_input,
                    padding.PKCS1v15(),
                    algo_map[alg],
                )
            else:
                raise OAuthValidationError(
                    f"unsupported public-key type "
                    f"{type(self._public_key).__name__}",
                    reason="oauth-invalid",
                )
        except InvalidSignature as exc:
            raise OAuthValidationError(
                "JWT signature did not verify",
                reason="oauth-invalid-signature",
            ) from exc

        # Issuer / audience checks.
        if self._expected_issuer and payload.get("iss") != self._expected_issuer:
            raise OAuthValidationError(
                f"iss claim {payload.get('iss')!r} does not match expected "
                f"{self._expected_issuer!r}",
                reason="oauth-unknown-issuer",
            )
        if self._expected_audience:
            aud_claim = payload.get("aud")
            aud_values = (
                [aud_claim] if isinstance(aud_claim, str) else (aud_claim or [])
            )
            if not any(a in self._expected_audience for a in aud_values):
                raise OAuthValidationError(
                    f"aud claim {aud_claim!r} does not match expected "
                    f"audiences {self._expected_audience!r}",
                    reason="oauth-invalid",
                )

        # Time-bound checks.
        now = int(time.time())
        exp = payload.get("exp")
        if exp is not None:
            try:
                if int(exp) + self._leeway_seconds < now:
                    raise OAuthValidationError(
                        "JWT exp is in the past",
                        reason="oauth-expired",
                    )
            except (TypeError, ValueError) as exc:
                raise OAuthValidationError(
                    f"exp claim is not an integer: {exp!r}",
                    reason="oauth-malformed",
                ) from exc
        nbf = payload.get("nbf")
        if nbf is not None:
            try:
                if int(nbf) - self._leeway_seconds > now:
                    raise OAuthValidationError(
                        "JWT nbf is in the future",
                        reason="oauth-not-yet-valid",
                    )
            except (TypeError, ValueError) as exc:
                raise OAuthValidationError(
                    f"nbf claim is not an integer: {nbf!r}",
                    reason="oauth-malformed",
                ) from exc

        return payload


# ---------------------------------------------------------------------------
# Validator registry — operators register custom classes here.
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, Any] = {
    "noop": NoOpValidator,
    "jwt": JWTValidator,
}


def register_validator(name: str, cls: type) -> None:
    """Register a custom :class:`OAuthValidator` subclass under
    ``name``. The dispatcher's config maps ``validator: <name>`` to
    the class registered here.

    Re-registration is allowed (operators can override the shipped
    ``noop``/``jwt`` validators in their deployment harness)."""
    if not name:
        raise ValueError("validator name must be non-empty")
    _REGISTRY[name] = cls


def get_validator(
    name: str, config: Optional[Dict[str, Any]] = None,
) -> OAuthValidator:
    """Instantiate the validator registered under ``name`` with the
    supplied ``validator_config``.

    Validators with no required config (``noop``) ignore the dict.
    Validators with required config (``jwt`` needs a ``public_key``)
    raise during construction when the config is incomplete — the
    error surfaces at server boot, not on the first request.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"no OAuth validator registered under {name!r}; "
            f"known: {sorted(_REGISTRY)}"
        )
    # Validators come in two shapes: zero-arg (NoOpValidator and
    # similar config-less ones) and config-dict-taking (JWTValidator
    # and operator-custom ones). Try with the config first; fall
    # back to no-args on TypeError. ABCMeta's inherited __init__
    # signature isn't reliable enough to introspect, so we just
    # call and let Python tell us.
    try:
        return cls(config or {})
    except TypeError as exc:
        # Distinguish "constructor takes no args" (fall back) from
        # "constructor raised TypeError internally" (re-raise so
        # the operator sees the real error).
        msg = str(exc)
        if "takes no arguments" in msg or "takes 1 positional" in msg:
            return cls()
        raise


def known_validators() -> Dict[str, type]:
    """Return a copy of the registry — used by introspection and
    test fixtures."""
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _b64url_decode(text: str) -> bytes:
    """URL-safe base64 decode with padding tolerance."""
    padded = text + ("=" * (-len(text) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _decode_jwt_payload(b64url: str) -> Dict[str, Any]:
    raw = _b64url_decode(b64url)
    return json.loads(raw.decode("utf-8"))


def _load_public_key_any(value: str):
    """Accept PEM or base64url-of-raw-bytes Ed25519, or PEM RSA."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    if "-----BEGIN" in value:
        return serialization.load_pem_public_key(value.encode("utf-8"))
    # b64url-of-raw-bytes — must be a 32-byte Ed25519 key.
    raw = _b64url_decode(value)
    if len(raw) == 32:
        return Ed25519PublicKey.from_public_bytes(raw)
    raise ValueError(
        f"public_key is not a PEM block and not 32 raw Ed25519 bytes "
        f"(got {len(raw)} bytes)"
    )


__all__ = [
    "JWTValidator",
    "NoOpValidator",
    "OAuthValidationError",
    "OAuthValidator",
    "extract_token",
    "get_validator",
    "known_validators",
    "register_validator",
]
