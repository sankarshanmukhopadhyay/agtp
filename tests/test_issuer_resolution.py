"""
Tests for Genesis-issuer trust resolution — Pattern 3 of the AGTP
identity composition story.

Three things to validate:

  1. Local trust anchors: ``("local", entry)`` when the key matches a
     pinned ``(name, key)`` pair; ``("unknown", None)`` otherwise.
  2. OIDC trust anchors: discovery + JWKS fetch and JWK->AGTP-canonical
     key matching. Tests mock the HTTP layer so they don't touch the
     network.
  3. Robustness: network failures during OIDC resolution return
     ``("unknown", None)`` and never throw.

The HTTP layer is mocked at ``core.issuer_resolution._fetch_json`` —
the single seam through which all network I/O flows. This keeps tests
deterministic without taking a hard dep on ``responses`` or similar.
"""

from __future__ import annotations

import base64
import json
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from core import issuer_resolution


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ed25519_pair() -> Tuple[Any, str]:
    """Return ``(private_key, public_key_b64url_raw)``.

    The b64url-of-raw-bytes form is AGTP-IDENTIFIERS canonical and
    is what the resolver compares against.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, _b64url(raw)


def _make_jwk(pubkey_b64url: str, *, kid: str = "k1") -> Dict[str, Any]:
    """RFC 8037 §2 OKP/Ed25519 JWK."""
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": kid,
        "x": pubkey_b64url,
        "use": "sig",
        "alg": "EdDSA",
    }


def _discovery_doc(
    issuer: str = "https://idp.example",
    jwks_uri: str = "https://idp.example/jwks.json",
) -> Dict[str, Any]:
    return {
        "issuer": issuer,
        "jwks_uri": jwks_uri,
        "id_token_signing_alg_values_supported": ["EdDSA", "RS256"],
    }


class _FakeFetcher:
    """Map of url -> JSON dict (or None to simulate failure).

    Patched in for ``_fetch_json`` to keep tests offline.
    """

    def __init__(self, routes: Dict[str, Optional[Dict[str, Any]]]):
        self.routes = routes
        self.calls: List[str] = []

    def __call__(self, url: str, *, timeout: int) -> Optional[Dict[str, Any]]:
        self.calls.append(url)
        return self.routes.get(url)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with empty discovery / JWKS caches."""
    issuer_resolution.reset_cache_for_tests()
    yield
    issuer_resolution.reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Local trust anchors.
# ---------------------------------------------------------------------------


def test_local_anchor_matches() -> None:
    _, pubkey = _ed25519_pair()
    anchors = [{"type": "key", "name": "primary", "value": pubkey}]
    verdict, payload = issuer_resolution.resolve_issuer_trust(
        pubkey, trust_anchors=anchors,
    )
    assert verdict == "local"
    assert payload is not None
    assert payload["name"] == "primary"
    assert payload["value"] == pubkey


def test_local_anchor_no_match_returns_unknown() -> None:
    _, pubkey_a = _ed25519_pair()
    _, pubkey_b = _ed25519_pair()
    anchors = [{"type": "key", "name": "other", "value": pubkey_a}]
    verdict, payload = issuer_resolution.resolve_issuer_trust(
        pubkey_b, trust_anchors=anchors,
    )
    assert verdict == "unknown"
    assert payload is None


def test_local_anchor_empty_input_is_unknown() -> None:
    """Empty / whitespace input keys never match; defensive against
    the verifier asking about a missing field."""
    _, pubkey = _ed25519_pair()
    anchors = [{"type": "key", "name": "p", "value": pubkey}]
    verdict, _ = issuer_resolution.resolve_issuer_trust(
        "", trust_anchors=anchors,
    )
    assert verdict == "unknown"


def test_local_anchor_first_match_wins_with_multiple_anchors() -> None:
    """Multiple local anchors: the matching one is returned even
    when it sits after a non-matching one."""
    _, pubkey_a = _ed25519_pair()
    _, pubkey_b = _ed25519_pair()
    anchors = [
        {"type": "key", "name": "a", "value": pubkey_a},
        {"type": "key", "name": "b", "value": pubkey_b},
    ]
    verdict, payload = issuer_resolution.resolve_issuer_trust(
        pubkey_b, trust_anchors=anchors,
    )
    assert verdict == "local"
    assert payload["name"] == "b"


def test_unknown_anchor_type_is_skipped() -> None:
    """Forward-compat: an anchor with an unrecognized ``type`` does
    not crash resolution; it's silently ignored."""
    _, pubkey = _ed25519_pair()
    anchors = [
        {"type": "future-anchor-kind", "value": "whatever"},
        {"type": "key", "name": "p", "value": pubkey},
    ]
    verdict, payload = issuer_resolution.resolve_issuer_trust(
        pubkey, trust_anchors=anchors,
    )
    assert verdict == "local"
    assert payload["name"] == "p"


def test_empty_anchor_list_is_unknown() -> None:
    _, pubkey = _ed25519_pair()
    verdict, payload = issuer_resolution.resolve_issuer_trust(
        pubkey, trust_anchors=[],
    )
    assert verdict == "unknown"
    assert payload is None


# ---------------------------------------------------------------------------
# OIDC trust anchors.
# ---------------------------------------------------------------------------


def test_oidc_anchor_matches_jwk_in_jwks() -> None:
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(jwks_uri=jwks_uri),
        jwks_uri: {"keys": [_make_jwk(pubkey)]},
    })
    anchors = [{
        "type": "oidc",
        "name": "enterprise-idp",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "oidc"
    assert payload is not None
    assert payload["anchor"]["name"] == "enterprise-idp"
    assert payload["jwk"]["x"] == pubkey
    assert payload["discovery"]["issuer"] == "https://idp.example"
    # Both endpoints fetched.
    assert discovery_url in fetcher.calls
    assert jwks_uri in fetcher.calls


def test_oidc_anchor_key_not_in_jwks_is_unknown() -> None:
    _, target_pubkey = _ed25519_pair()
    _, other_pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(jwks_uri=jwks_uri),
        # JWKS publishes a different key; our target should not match.
        jwks_uri: {"keys": [_make_jwk(other_pubkey)]},
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            target_pubkey, trust_anchors=anchors,
        )
    assert verdict == "unknown"
    assert payload is None


def test_oidc_network_failure_on_discovery_returns_unknown() -> None:
    """Network failure during discovery fetch surfaces as unknown
    — never throws."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    fetcher = _FakeFetcher({discovery_url: None})  # simulates URLError
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "unknown"
    assert payload is None


def test_oidc_network_failure_on_jwks_returns_unknown() -> None:
    """Discovery succeeds, JWKS fetch fails — still unknown, no
    throw."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(jwks_uri=jwks_uri),
        jwks_uri: None,
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "unknown"
    assert payload is None


def test_oidc_issuer_substitution_attack_is_rejected() -> None:
    """When ``trusted_issuer`` is set on the anchor, a discovery doc
    whose ``issuer`` field disagrees MUST be rejected even when the
    JWKS would otherwise match. This is the IdP-substitution
    defense documented in :func:`_check_oidc_anchor`."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(
            issuer="https://attacker.example",  # mismatch
            jwks_uri=jwks_uri,
        ),
        jwks_uri: {"keys": [_make_jwk(pubkey)]},
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "unknown"
    assert payload is None


def test_oidc_anchor_without_trusted_issuer_skips_issuer_check() -> None:
    """When the anchor omits ``trusted_issuer``, the discovery doc's
    issuer is not validated — useful for dev / local IdPs that
    don't have a stable issuer URL."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://dev-idp.local/.well-known/openid-configuration"
    jwks_uri = "https://dev-idp.local/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(
            issuer="https://something-else.example",
            jwks_uri=jwks_uri,
        ),
        jwks_uri: {"keys": [_make_jwk(pubkey)]},
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        # No trusted_issuer → issuer is not validated.
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "oidc"
    assert payload["jwk"]["x"] == pubkey


def test_oidc_anchor_jwks_with_non_okp_keys_filters_them_out() -> None:
    """A JWKS that mixes RSA / EC / OKP keys must still match the
    OKP/Ed25519 one. Non-OKP keys are silently filtered."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    rsa_jwk = {
        "kty": "RSA",
        "kid": "rsa-1",
        "n": "fake-modulus",
        "e": "AQAB",
        "use": "sig",
        "alg": "RS256",
    }
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(jwks_uri=jwks_uri),
        jwks_uri: {"keys": [rsa_jwk, _make_jwk(pubkey, kid="ed-1")]},
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "oidc"
    assert payload["jwk"]["kid"] == "ed-1"


def test_oidc_anchor_jwks_padded_x_field_still_matches() -> None:
    """Some IdPs publish JWK ``x`` values with base64 padding; AGTP
    canonical form is unpadded. The resolver must normalize."""
    _, pubkey = _ed25519_pair()
    padded = pubkey + "="  # legal-but-noncanonical padded form
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(jwks_uri=jwks_uri),
        jwks_uri: {"keys": [_make_jwk(padded)]},
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "oidc"


def test_oidc_caching_avoids_duplicate_fetches() -> None:
    """Two back-to-back resolutions hit the network once for
    discovery and once for JWKS; the second resolution uses the
    cache."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    jwks_uri = "https://idp.example/jwks.json"
    fetcher = _FakeFetcher({
        discovery_url: _discovery_doc(jwks_uri=jwks_uri),
        jwks_uri: {"keys": [_make_jwk(pubkey)]},
    })
    anchors = [{
        "type": "oidc",
        "discovery_url": discovery_url,
        "trusted_issuer": "https://idp.example",
    }]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        v1, _ = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
        v2, _ = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert v1 == "oidc"
    assert v2 == "oidc"
    # Exactly one discovery fetch + one JWKS fetch across both calls.
    assert fetcher.calls.count(discovery_url) == 1
    assert fetcher.calls.count(jwks_uri) == 1


def test_local_anchor_short_circuits_before_oidc() -> None:
    """Local anchors check first. When one matches, no network
    activity happens — operators relying on the cheap path can
    front a costly OIDC anchor with a pinned key."""
    _, pubkey = _ed25519_pair()
    discovery_url = "https://idp.example/.well-known/openid-configuration"
    fetcher = _FakeFetcher({})  # any HTTP touch returns None
    anchors = [
        {"type": "key", "name": "pinned", "value": pubkey},
        {"type": "oidc", "discovery_url": discovery_url,
         "trusted_issuer": "https://idp.example"},
    ]
    with patch.object(issuer_resolution, "_fetch_json", fetcher):
        verdict, payload = issuer_resolution.resolve_issuer_trust(
            pubkey, trust_anchors=anchors,
        )
    assert verdict == "local"
    assert payload["name"] == "pinned"
    assert fetcher.calls == []  # OIDC anchor never touched.


# ---------------------------------------------------------------------------
# Network-layer robustness — _fetch_json never raises.
# ---------------------------------------------------------------------------


def test_fetch_json_url_error_returns_none() -> None:
    """The lowest-level fetch helper swallows URLError."""
    def _boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")
    with patch.object(issuer_resolution.urllib.request, "urlopen", _boom):
        result = issuer_resolution._fetch_json(
            "https://idp.example/x", timeout=5,
        )
    assert result is None


def test_fetch_json_unexpected_exception_returns_none() -> None:
    """Even an unexpected exception type returns None rather than
    propagating — the resolver must never crash the verifier."""
    def _boom(*args, **kwargs):
        raise RuntimeError("something weird happened in urllib")
    with patch.object(issuer_resolution.urllib.request, "urlopen", _boom):
        result = issuer_resolution._fetch_json(
            "https://idp.example/x", timeout=5,
        )
    assert result is None


# ---------------------------------------------------------------------------
# JWK conversion.
# ---------------------------------------------------------------------------


def test_jwk_to_ed25519_b64url_strips_padding() -> None:
    """The canonical-form helper drops trailing '=' padding so
    byte-equality against AGTP keys works."""
    _, pubkey = _ed25519_pair()
    jwk = _make_jwk(pubkey + "==")
    assert issuer_resolution._jwk_to_ed25519_b64url(jwk) == pubkey


def test_jwk_to_ed25519_b64url_rejects_non_okp() -> None:
    """Only OKP-kty JWKs convert; RSA / EC return None."""
    rsa = {"kty": "RSA", "n": "x", "e": "AQAB"}
    assert issuer_resolution._jwk_to_ed25519_b64url(rsa) is None


def test_jwk_to_ed25519_b64url_rejects_non_ed25519_okp() -> None:
    """OKP with a curve other than Ed25519 (X25519, Ed448, etc.)
    returns None — AGTP currently lives on Ed25519 alone."""
    okp_x25519 = {"kty": "OKP", "crv": "X25519", "x": "ignored"}
    assert issuer_resolution._jwk_to_ed25519_b64url(okp_x25519) is None


def test_jwk_to_ed25519_b64url_rejects_missing_x() -> None:
    """OKP/Ed25519 without an ``x`` field is malformed; return None
    rather than throwing."""
    bad = {"kty": "OKP", "crv": "Ed25519"}
    assert issuer_resolution._jwk_to_ed25519_b64url(bad) is None


# ---------------------------------------------------------------------------
# load_trust_anchors — JSON file loader.
# ---------------------------------------------------------------------------


def test_load_trust_anchors_list_form(tmp_path) -> None:
    anchors_file = tmp_path / "anchors.json"
    anchors_file.write_text(json.dumps([
        {"type": "key", "name": "primary", "value": "abc"},
        {"type": "oidc", "name": "idp",
         "discovery_url": "https://idp.example/.well-known/openid-configuration",
         "trusted_issuer": "https://idp.example"},
    ]), encoding="utf-8")
    anchors = issuer_resolution.load_trust_anchors(str(anchors_file))
    assert len(anchors) == 2
    assert anchors[0]["name"] == "primary"
    assert anchors[1]["type"] == "oidc"


def test_load_trust_anchors_object_form(tmp_path) -> None:
    anchors_file = tmp_path / "anchors.json"
    anchors_file.write_text(json.dumps({
        "anchors": [
            {"type": "key", "name": "primary", "value": "abc"},
        ],
    }), encoding="utf-8")
    anchors = issuer_resolution.load_trust_anchors(str(anchors_file))
    assert len(anchors) == 1
    assert anchors[0]["name"] == "primary"


def test_load_trust_anchors_missing_file_returns_empty() -> None:
    """A missing file is not a fatal misconfiguration — operators
    may roll out anchors gradually."""
    anchors = issuer_resolution.load_trust_anchors(
        "/path/does/not/exist/anchors.json",
    )
    assert anchors == []


def test_load_trust_anchors_malformed_json_returns_empty(tmp_path) -> None:
    anchors_file = tmp_path / "anchors.json"
    anchors_file.write_text("not valid json {", encoding="utf-8")
    anchors = issuer_resolution.load_trust_anchors(str(anchors_file))
    assert anchors == []


def test_load_trust_anchors_non_dict_entries_filtered(tmp_path) -> None:
    """Entries that aren't dicts (e.g. a stray string in the list)
    are filtered out rather than blowing up the loader."""
    anchors_file = tmp_path / "anchors.json"
    anchors_file.write_text(json.dumps([
        {"type": "key", "name": "primary", "value": "abc"},
        "not a dict",
        42,
    ]), encoding="utf-8")
    anchors = issuer_resolution.load_trust_anchors(str(anchors_file))
    assert len(anchors) == 1
    assert anchors[0]["name"] == "primary"
