"""
Tests for OAuth/OIDC composition — Pattern 2 of the AGTP identity
composition story.

Three layers covered:

  1. Token extraction (server.oauth_context.extract_token).
  2. Validator framework (NoOpValidator, JWTValidator, registry).
  3. Dispatcher integration (401 surface, claim lift, Attribution-
     Record stamping, Pattern 1 backward compatibility).
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from server.config import (
    AgentsConfig, AuditConfig, OAuthConfig, RcnsConfig, ServerConfig,
    ServerInfo, ServerPolicy, SigningConfig, SynthesisConfig,
)
from server.oauth_context import (
    JWTValidator, NoOpValidator, OAuthValidationError,
    OAuthValidator, extract_token, get_validator, known_validators,
    register_validator,
)


# ---------------------------------------------------------------------------
# Helpers — JWT minting for the validator tests.
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint_jwt(
    payload: Dict[str, Any], priv_key, *, alg: str = "EdDSA",
) -> str:
    header = {"alg": alg, "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = priv_key.sign(signing_input)
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _ed25519_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return priv, pub_pem


def _req(headers: Dict[str, str] | None = None) -> wire.AGTPRequest:
    h = {"Agent-ID": "a" * 64, "Content-Length": "2"}
    if headers:
        h.update(headers)
    return wire.AGTPRequest(
        method="QUERY", path="/", headers=h, body_bytes=b"{}",
    )


# ---------------------------------------------------------------------------
# Token extraction.
# ---------------------------------------------------------------------------


def test_extract_token_recognizes_bearer() -> None:
    req = _req({"Authorization": "Bearer abc.def.ghi"})
    assert extract_token(req) == "abc.def.ghi"


def test_extract_token_is_case_insensitive_on_scheme() -> None:
    """RFC 7235 §2.1: scheme is case-insensitive."""
    assert extract_token(_req({"Authorization": "BEARER xyz"})) == "xyz"
    assert extract_token(_req({"Authorization": "bearer xyz"})) == "xyz"
    assert extract_token(_req({"Authorization": "BeArEr xyz"})) == "xyz"


def test_extract_token_strips_surrounding_whitespace() -> None:
    assert extract_token(
        _req({"Authorization": "   Bearer   abc.def.ghi   "}),
    ) == "abc.def.ghi"


def test_extract_token_returns_none_when_header_missing() -> None:
    assert extract_token(_req()) is None


def test_extract_token_ignores_other_schemes() -> None:
    """Basic / Digest / anything-else passes through as None —
    AGTP doesn't interpret them."""
    for scheme in ("Basic dXNlcjpwYXNz", "Digest username=foo", "Negotiate ..."):
        assert extract_token(_req({"Authorization": scheme})) is None


def test_extract_token_handles_empty_bearer_value() -> None:
    """`Authorization: Bearer` with nothing after must not be
    treated as a present token."""
    assert extract_token(_req({"Authorization": "Bearer"})) is None
    assert extract_token(_req({"Authorization": "Bearer    "})) is None


# ---------------------------------------------------------------------------
# NoOpValidator sanity.
# ---------------------------------------------------------------------------


def test_noop_validator_accepts_any_non_empty_token() -> None:
    v = NoOpValidator()
    assert v.validate("anything") == {}


def test_noop_validator_lifts_jwt_payload_when_present() -> None:
    """If the token *looks* like a JWT, the no-op validator returns
    its claims so test fixtures can put a `sub` claim into a
    no-op token and have it lift cleanly."""
    payload = {"sub": "chris@nomotic.ai", "name": "Chris Hood"}
    token = "fakehdr." + _b64url(
        json.dumps(payload).encode(),
    ) + ".fakesig"
    claims = NoOpValidator().validate(token)
    assert claims["sub"] == "chris@nomotic.ai"


def test_noop_validator_refuses_empty_token() -> None:
    with pytest.raises(OAuthValidationError) as exc:
        NoOpValidator().validate("")
    assert exc.value.reason == "oauth-malformed"


# ---------------------------------------------------------------------------
# JWTValidator.
# ---------------------------------------------------------------------------


def test_jwt_validator_accepts_signed_token() -> None:
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt(
        {"sub": "chris@nomotic.ai", "iat": int(time.time())},
        priv,
    )
    v = JWTValidator({"public_key": pub_pem})
    claims = v.validate(token)
    assert claims["sub"] == "chris@nomotic.ai"


def test_jwt_validator_rejects_wrong_key() -> None:
    priv_a, _ = _ed25519_pair()
    _, pub_b_pem = _ed25519_pair()
    token = _mint_jwt({"sub": "x"}, priv_a)
    v = JWTValidator({"public_key": pub_b_pem})
    with pytest.raises(OAuthValidationError) as exc:
        v.validate(token)
    assert exc.value.reason == "oauth-invalid-signature"


def test_jwt_validator_rejects_expired_token() -> None:
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt(
        {"sub": "x", "exp": int(time.time()) - 3600},
        priv,
    )
    v = JWTValidator({"public_key": pub_pem, "leeway_seconds": 0})
    with pytest.raises(OAuthValidationError) as exc:
        v.validate(token)
    assert exc.value.reason == "oauth-expired"


def test_jwt_validator_rejects_not_yet_valid_token() -> None:
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt(
        {"sub": "x", "nbf": int(time.time()) + 3600},
        priv,
    )
    v = JWTValidator({"public_key": pub_pem, "leeway_seconds": 0})
    with pytest.raises(OAuthValidationError) as exc:
        v.validate(token)
    assert exc.value.reason == "oauth-not-yet-valid"


def test_jwt_validator_honors_leeway_seconds() -> None:
    """A token expired 30s ago passes with a 60s leeway."""
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt(
        {"sub": "x", "exp": int(time.time()) - 30},
        priv,
    )
    v = JWTValidator({"public_key": pub_pem, "leeway_seconds": 60})
    claims = v.validate(token)
    assert claims["sub"] == "x"


def test_jwt_validator_refuses_malformed_jwt() -> None:
    priv, pub_pem = _ed25519_pair()
    v = JWTValidator({"public_key": pub_pem})
    with pytest.raises(OAuthValidationError) as exc:
        v.validate("not.a.jwt.shape")
    assert exc.value.reason == "oauth-malformed"


def test_jwt_validator_enforces_alg_allowlist() -> None:
    priv, pub_pem = _ed25519_pair()
    # Mint with a deliberately-wrong alg header.
    payload_b64 = _b64url(json.dumps({"sub": "x"}).encode())
    header_b64 = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    sig = _b64url(priv.sign(f"{header_b64}.{payload_b64}".encode()))
    token = f"{header_b64}.{payload_b64}.{sig}"
    v = JWTValidator({"public_key": pub_pem})  # default allowed = EdDSA
    with pytest.raises(OAuthValidationError) as exc:
        v.validate(token)
    assert exc.value.reason == "oauth-malformed"


def test_jwt_validator_enforces_expected_issuer() -> None:
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt({"sub": "x", "iss": "wrong.example"}, priv)
    v = JWTValidator({
        "public_key": pub_pem,
        "expected_issuer": "expected.example",
    })
    with pytest.raises(OAuthValidationError) as exc:
        v.validate(token)
    assert exc.value.reason == "oauth-unknown-issuer"


def test_jwt_validator_requires_public_key_in_config() -> None:
    with pytest.raises(ValueError, match="public_key"):
        JWTValidator({})


# ---------------------------------------------------------------------------
# Validator registry.
# ---------------------------------------------------------------------------


def test_get_validator_returns_noop_by_name() -> None:
    v = get_validator("noop")
    assert isinstance(v, NoOpValidator)


def test_get_validator_returns_jwt_with_config() -> None:
    _, pub_pem = _ed25519_pair()
    v = get_validator("jwt", {"public_key": pub_pem})
    assert isinstance(v, JWTValidator)


def test_get_validator_raises_on_unknown_name() -> None:
    with pytest.raises(KeyError, match="no OAuth validator"):
        get_validator("invented")


def test_register_validator_makes_custom_class_discoverable() -> None:
    class _AcceptHardcoded(OAuthValidator):
        name = "hardcoded"
        def validate(self, token: str) -> Dict[str, Any]:
            if token == "magic":
                return {"sub": "test"}
            raise OAuthValidationError(
                "wrong token", reason="oauth-invalid",
            )
    register_validator("test-hardcoded", _AcceptHardcoded)
    assert "test-hardcoded" in known_validators()
    v = get_validator("test-hardcoded")
    assert v.validate("magic")["sub"] == "test"


# ---------------------------------------------------------------------------
# OAuthConfig TOML loading.
# ---------------------------------------------------------------------------


def test_oauth_config_default_is_off(tmp_path: Path) -> None:
    """A server config with no [policies.oauth] block has OAuth off."""
    from server.config import load
    f = tmp_path / "s.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "op"
contact = "c"
""",
        encoding="utf-8",
    )
    cfg = load(f)
    assert cfg.oauth.enabled is False


def test_oauth_config_loads_from_toml(tmp_path: Path) -> None:
    from server.config import load
    f = tmp_path / "s.toml"
    f.write_text(
        """
[server]
server_id = "t.local"
operator = "op"
contact = "c"

[policies.oauth]
enabled = true
required_on_methods = ["PURCHASE", "execute"]
validator = "jwt"
principal_id_claim = "preferred_username"

[policies.oauth.validator_config]
expected_issuer = "https://idp.example"
""",
        encoding="utf-8",
    )
    cfg = load(f)
    assert cfg.oauth.enabled is True
    # Normalized to uppercase regardless of input case.
    assert cfg.oauth.required_on_methods == ["PURCHASE", "EXECUTE"]
    assert cfg.oauth.validator == "jwt"
    assert cfg.oauth.principal_id_claim == "preferred_username"
    assert cfg.oauth.validator_config["expected_issuer"] == "https://idp.example"


# ---------------------------------------------------------------------------
# Dispatcher integration.
# ---------------------------------------------------------------------------


def _doc(scopes: list | None = None) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["DISCOVER", "QUERY", "PURCHASE"],
            scopes=scopes or [],
            wildcards=True,
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


def _state(*, oauth_cfg: OAuthConfig | None = None) -> Any:
    """ServerState mock that the dispatcher will read."""
    config = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="op", contact="c"),
        policy=ServerPolicy(),
        agents=AgentsConfig(),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(),
        oauth=oauth_cfg or OAuthConfig(),
        audit=AuditConfig(),
        signing=SigningConfig(),
    )
    state = MagicMock()
    state.config = config
    state.synthesis_runtime = None
    state.endpoint_registry = None
    state.methods_policy = None
    return state


def _build_request(
    *,
    method: str = "QUERY", path: str = "/",
    auth_header: str | None = None,
) -> wire.AGTPRequest:
    headers = {"Agent-ID": "a" * 64, "Content-Length": "2"}
    if auth_header:
        headers["Authorization"] = auth_header
    return wire.AGTPRequest(
        method=method, path=path, headers=headers, body_bytes=b"{}",
    )


def test_pattern_1_oauth_disabled_passes_through_unchanged() -> None:
    """The most important regression check: OAuth disabled (default)
    means no extraction, no validation, no 401. Pattern 1
    deployments work exactly as they did before."""
    from server.methods import dispatch
    state = _state()  # OAuthConfig() defaults to enabled=False
    req = _build_request()  # no Authorization header
    resp = dispatch(req, state, _doc(), config=state.config)
    # QUERY is embedded; the no-handler path returns 405 (method
    # registered in catalog, no handler bound on this state). The
    # KEY assertion is that we do NOT see 401 oauth-anything.
    assert resp.status_code != 401


def test_oauth_required_method_returns_401_when_token_missing() -> None:
    from server.methods import dispatch
    state = _state(
        oauth_cfg=OAuthConfig(
            enabled=True,
            required_on_methods=["PURCHASE"],
            validator="noop",
        ),
    )
    req = _build_request(method="PURCHASE")  # no auth
    resp = dispatch(req, state, _doc(), config=state.config)
    assert resp.status_code == 401
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "oauth-required"
    assert body["error"]["method"] == "PURCHASE"


def test_oauth_optional_method_passes_without_token() -> None:
    """When required_on_methods doesn't list the method, an absent
    token does not produce 401."""
    from server.methods import dispatch
    state = _state(
        oauth_cfg=OAuthConfig(
            enabled=True,
            required_on_methods=["PURCHASE"],
            validator="noop",
        ),
    )
    # QUERY isn't in required_on_methods — token-less is fine.
    req = _build_request(method="QUERY")
    resp = dispatch(req, state, _doc(), config=state.config)
    assert resp.status_code != 401


def test_oauth_invalid_signature_returns_401_oauth_invalid_signature() -> None:
    from server.methods import dispatch
    priv_a, _ = _ed25519_pair()
    _, pub_b_pem = _ed25519_pair()
    token = _mint_jwt({"sub": "test"}, priv_a)
    state = _state(
        oauth_cfg=OAuthConfig(
            enabled=True,
            required_on_methods=["PURCHASE"],
            validator="jwt",
            validator_config={"public_key": pub_b_pem},
        ),
    )
    req = _build_request(
        method="PURCHASE",
        auth_header=f"Bearer {token}",
    )
    resp = dispatch(req, state, _doc(), config=state.config)
    assert resp.status_code == 401
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "oauth-invalid-signature"


def test_oauth_successful_validation_lifts_principal_claim() -> None:
    """After successful validation the dispatcher lifts the
    configured claim onto request.acting_principal_id so
    handlers and the Attribution-Record can read it."""
    from server.methods import dispatch
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt(
        {"sub": "chris@nomotic.ai", "iat": int(time.time())},
        priv,
    )
    state = _state(
        oauth_cfg=OAuthConfig(
            enabled=True,
            validator="jwt",
            validator_config={"public_key": pub_pem},
        ),
    )
    req = _build_request(
        method="QUERY",
        auth_header=f"Bearer {token}",
    )
    dispatch(req, state, _doc(), config=state.config)
    assert getattr(req, "acting_principal_id", "") == "chris@nomotic.ai"


def test_oauth_custom_principal_claim_lifted() -> None:
    """The principal_id_claim knob picks which JWT claim becomes
    acting_principal_id."""
    from server.methods import dispatch
    priv, pub_pem = _ed25519_pair()
    token = _mint_jwt(
        {"sub": "ignored", "preferred_username": "chris"},
        priv,
    )
    state = _state(
        oauth_cfg=OAuthConfig(
            enabled=True,
            validator="jwt",
            validator_config={"public_key": pub_pem},
            principal_id_claim="preferred_username",
        ),
    )
    req = _build_request(method="QUERY", auth_header=f"Bearer {token}")
    dispatch(req, state, _doc(), config=state.config)
    assert getattr(req, "acting_principal_id", "") == "chris"


def test_oauth_validator_misconfig_returns_500() -> None:
    """An unknown validator name surfaces as 500 (operator
    misconfiguration) rather than crashing the dispatcher."""
    from server.methods import dispatch
    state = _state(
        oauth_cfg=OAuthConfig(
            enabled=True,
            validator="invented-validator-name",
            required_on_methods=["PURCHASE"],
        ),
    )
    req = _build_request(
        method="PURCHASE",
        auth_header="Bearer some-token",
    )
    resp = dispatch(req, state, _doc(), config=state.config)
    assert resp.status_code == 500
    body = json.loads(resp.body_bytes)
    assert body["error"]["code"] == "oauth-validator-misconfigured"


def test_per_agent_oauth_policy_overrides_server() -> None:
    """A per-agent policies.oauth block takes precedence — one
    agent on a multi-tenant server can require tokens while
    another doesn't."""
    from server.methods import dispatch
    state = _state(
        # Server-wide: OAuth off entirely.
        oauth_cfg=OAuthConfig(enabled=False),
    )
    # Per-agent: OAuth enabled, PURCHASE required.
    doc = _doc()
    doc.policies = {
        "oauth": {
            "enabled": True,
            "required_on_methods": ["PURCHASE"],
            "validator": "noop",
        },
    }
    req = _build_request(method="PURCHASE")  # no auth
    resp = dispatch(req, state, doc, config=state.config)
    assert resp.status_code == 401
    body = json.loads(resp.body_bytes)
    assert body["error"]["reason"] == "oauth-required"


# ---------------------------------------------------------------------------
# Attribution-Record stamps acting_principal_id (not the token).
# ---------------------------------------------------------------------------


def test_attribution_record_carries_acting_principal_not_token() -> None:
    """The validated principal claim rides on the Attribution-Record
    extra block; the raw token MUST NOT appear anywhere."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from server.main import _finalize_response
    from server.signing import (
        SigningService, parse_attribution_record,
    )

    # Daemon signing key.
    daemon_key = Ed25519PrivateKey.generate()
    key_pem = daemon_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    import tempfile, os
    fd, key_path = tempfile.mkstemp(suffix=".key")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key_pem)
        sig_service = SigningService.from_key_path(key_path)
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass

    cfg = ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        policy=ServerPolicy(),
        agents=AgentsConfig(),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(),
        oauth=OAuthConfig(),
        audit=AuditConfig(
            attribution_records_enabled=True,
        ),
        signing=SigningConfig(),
    )
    cfg.signing_service = sig_service

    # Build a response, and a request whose acting_principal_id was
    # set by the dispatcher (we simulate the lift directly here).
    response = wire.AGTPResponse(
        status_code=200, status_text="OK",
        headers={}, body_bytes=b"{}",
    )
    request = wire.AGTPRequest(
        method="QUERY", path="/",
        headers={"Agent-ID": "a" * 64, "Content-Length": "2"},
        body_bytes=b"{}",
    )
    setattr(request, "acting_principal_id", "chris@nomotic.ai")

    _finalize_response(
        response,
        request,
        cfg,
        attribution_extra={"acting_principal_id": "chris@nomotic.ai"},
    )

    jws = response.headers.get("Attribution-Record")
    assert jws
    _, payload, _ = parse_attribution_record(jws)
    # Acting-principal lifted into the extra block.
    extra = payload.get("extra", {})
    assert extra.get("acting_principal_id") == "chris@nomotic.ai"
    # The raw token MUST NOT appear anywhere in the payload.
    payload_text = json.dumps(payload)
    assert "Bearer" not in payload_text
    assert "Authorization" not in payload_text
