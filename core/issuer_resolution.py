"""
Genesis-issuer trust resolution — Pattern 3 of the AGTP identity
composition story.

The Tier 1 trust path needs to answer "is this Genesis-issuer key
trusted?" without baking a single hardcoded list into every
deployment. AGTP supports two answer mechanisms:

  * **Local trust anchor** — operator-pinned ``(name, key)`` pairs
    in a config file. The key is the registrar's published Ed25519
    public key (base64url-of-raw-bytes per AGTP-IDENTIFIERS).
    Simplest path; works for fixed sets of trusted registrars.
  * **OIDC-federated trust anchor** — the registrar publishes its
    signing keys in a JWKS that an OIDC issuer hosts. The local
    config carries only the OIDC discovery URL and the expected
    ``iss``; runtime resolution fetches the discovery document and
    JWKS, then checks whether the Genesis-issuer key matches any
    JWK the IdP published.

:func:`resolve_issuer_trust` is the single entry point. Three
outcomes:

  * ``("local", entry)`` — the key matches a locally-listed anchor.
  * ``("oidc", metadata)`` — the key matches a JWK published by the
    OIDC issuer named in the matching anchor.
  * ``("unknown", None)`` — no anchor matched (or all OIDC fetches
    failed / didn't publish the key).

Network failures during OIDC resolution return
``("unknown", None)`` and never throw. This keeps the verifier
side robust to transient connectivity issues — falling back
cleanly is better than crashing every request that referenced an
unreachable IdP.

Caching: JWKS responses cache per discovery URL with a default
1-hour TTL. The cache is process-global; operators with strict
key-rotation requirements should configure the TTL down or
restart the daemon at rotation time.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_JWKS_TTL_SECONDS = 3600       # 1 hour
DEFAULT_FETCH_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Module-level JWKS cache.
# ---------------------------------------------------------------------------


_CACHE_LOCK = threading.Lock()

#: ``discovery_url -> (expiry_unix_ts, parsed_response_dict)``
_DISCOVERY_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

#: ``jwks_uri -> (expiry_unix_ts, jwks_dict)``
_JWKS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def reset_cache_for_tests() -> None:
    """Clear both caches. Tests reach for this between cases;
    production never calls it."""
    with _CACHE_LOCK:
        _DISCOVERY_CACHE.clear()
        _JWKS_CACHE.clear()


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def resolve_issuer_trust(
    issuer_pubkey_b64url: str,
    *,
    trust_anchors: List[Dict[str, Any]],
    ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
    fetch_timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve the trust posture of a Genesis-issuer public key.

    ``trust_anchors`` is a list of entries, each shaped per its
    ``type`` field:

      * Local anchor:
        ``{"type": "key", "value": "<b64url-raw-32-byte-key>",
        "name": "...optional..."}``
      * OIDC anchor:
        ``{"type": "oidc", "discovery_url": "https://idp.example/.well-known/openid-configuration",
        "trusted_issuer": "https://idp.example", "name": "..."}``

    Local anchors check first (they're cheap; no network). OIDC
    anchors check next, in declaration order. The first match
    wins. Anchors with unknown ``type`` are skipped silently —
    forward-compat for future anchor kinds.

    Returns one of:

      * ``("local", entry)`` — ``entry`` is the matching anchor dict.
      * ``("oidc", metadata)`` — ``metadata`` carries ``anchor``
        (the matching anchor dict), ``jwk`` (the specific JWK that
        matched), and ``discovery`` (the cached discovery document).
      * ``("unknown", None)`` — no anchor matched, or OIDC fetches
        all failed.

    Network failures and malformed responses are swallowed (logged
    via stderr is operator territory; this resolver never raises).
    """
    if not issuer_pubkey_b64url:
        return ("unknown", None)
    target = issuer_pubkey_b64url.strip()

    # Pass 1: local anchors.
    for anchor in trust_anchors or []:
        if not isinstance(anchor, dict):
            continue
        if anchor.get("type") != "key":
            continue
        value = str(anchor.get("value") or "").strip()
        if value == target:
            return ("local", dict(anchor))

    # Pass 2: OIDC anchors.
    for anchor in trust_anchors or []:
        if not isinstance(anchor, dict):
            continue
        if anchor.get("type") != "oidc":
            continue
        discovery_url = str(anchor.get("discovery_url") or "").strip()
        trusted_issuer = str(anchor.get("trusted_issuer") or "").strip()
        if not discovery_url:
            continue
        match = _check_oidc_anchor(
            target_pubkey_b64url=target,
            discovery_url=discovery_url,
            trusted_issuer=trusted_issuer,
            ttl_seconds=ttl_seconds,
            fetch_timeout_seconds=fetch_timeout_seconds,
        )
        if match is not None:
            jwk, discovery = match
            return ("oidc", {
                "anchor": dict(anchor),
                "jwk": jwk,
                "discovery": discovery,
            })

    return ("unknown", None)


# ---------------------------------------------------------------------------
# OIDC-specific helpers.
# ---------------------------------------------------------------------------


def _check_oidc_anchor(
    *,
    target_pubkey_b64url: str,
    discovery_url: str,
    trusted_issuer: str,
    ttl_seconds: int,
    fetch_timeout_seconds: int,
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Resolve one OIDC anchor against the target key.

    Returns ``(matching_jwk, discovery_dict)`` on match, or
    ``None`` (network failure, malformed response, key not in
    JWKS, issuer mismatch).
    """
    discovery = _fetch_discovery(
        discovery_url,
        ttl_seconds=ttl_seconds,
        fetch_timeout_seconds=fetch_timeout_seconds,
    )
    if not discovery:
        return None
    # When the anchor names a trusted_issuer, the discovery doc's
    # ``issuer`` field MUST match. This protects against an
    # IdP-substitution attack where the discovery URL is correct
    # but the served metadata has been swapped.
    if trusted_issuer:
        served_issuer = str(discovery.get("issuer") or "").strip()
        if served_issuer != trusted_issuer:
            return None
    jwks_uri = str(discovery.get("jwks_uri") or "").strip()
    if not jwks_uri:
        return None
    jwks = _fetch_jwks(
        jwks_uri,
        ttl_seconds=ttl_seconds,
        fetch_timeout_seconds=fetch_timeout_seconds,
    )
    if not jwks:
        return None
    keys = jwks.get("keys") or []
    if not isinstance(keys, list):
        return None
    for jwk in keys:
        if not isinstance(jwk, dict):
            continue
        jwk_pubkey_b64url = _jwk_to_ed25519_b64url(jwk)
        if jwk_pubkey_b64url is None:
            continue
        if jwk_pubkey_b64url == target_pubkey_b64url:
            return (dict(jwk), dict(discovery))
    return None


def _jwk_to_ed25519_b64url(jwk: Dict[str, Any]) -> Optional[str]:
    """Convert a JWK to the AGTP-canonical b64url-of-raw-bytes
    form, returning ``None`` when the JWK isn't an Ed25519 key.

    RFC 8037 §2 defines the OKP key type (kty="OKP") with
    crv="Ed25519" and ``x`` as the b64url-encoded 32 raw bytes.
    AGTP's canonical form is exactly that — so for Ed25519 the
    answer is just the ``x`` field, normalized (no padding).
    """
    if jwk.get("kty") != "OKP":
        return None
    if jwk.get("crv") != "Ed25519":
        return None
    x = jwk.get("x")
    if not isinstance(x, str) or not x:
        return None
    # Normalize: AGTP canonical form is unpadded. Strip any
    # padding the JWK happens to carry so byte-equality works
    # against the daemon's representation.
    return x.rstrip("=")


def _fetch_discovery(
    discovery_url: str, *, ttl_seconds: int, fetch_timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Cached fetch of an OIDC discovery document. Returns the
    parsed JSON dict or ``None`` on any failure."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _DISCOVERY_CACHE.get(discovery_url)
        if entry is not None:
            expires_at, cached = entry
            if expires_at > now:
                return cached
    fetched = _fetch_json(discovery_url, timeout=fetch_timeout_seconds)
    if fetched is None:
        return None
    with _CACHE_LOCK:
        _DISCOVERY_CACHE[discovery_url] = (now + ttl_seconds, fetched)
    return fetched


def _fetch_jwks(
    jwks_uri: str, *, ttl_seconds: int, fetch_timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Cached fetch of a JWKS document. Returns the parsed JSON
    dict or ``None`` on any failure."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _JWKS_CACHE.get(jwks_uri)
        if entry is not None:
            expires_at, cached = entry
            if expires_at > now:
                return cached
    fetched = _fetch_json(jwks_uri, timeout=fetch_timeout_seconds)
    if fetched is None:
        return None
    with _CACHE_LOCK:
        _JWKS_CACHE[jwks_uri] = (now + ttl_seconds, fetched)
    return fetched


def _fetch_json(url: str, *, timeout: int) -> Optional[Dict[str, Any]]:
    """One-shot JSON fetch. Returns ``None`` on any failure —
    network error, non-200, non-JSON, anything. Never raises."""
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                return None
            return data
    except urllib.error.URLError:
        return None
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    except Exception:  # noqa: BLE001 — defensive; never raise upward
        return None


# ---------------------------------------------------------------------------
# Convenience: load anchors from a JSON file.
# ---------------------------------------------------------------------------


def load_trust_anchors(path: str) -> List[Dict[str, Any]]:
    """Read a JSON file containing a list of anchor dicts.

    File format (top-level is either a list of anchors or an
    object with an ``anchors`` key whose value is the list)::

        [
          {"type": "key", "name": "primary-registrar",
           "value": "FXJ-X2hL3...32-byte-b64url..."},
          {"type": "oidc", "name": "enterprise-idp",
           "discovery_url": "https://idp.example/.well-known/openid-configuration",
           "trusted_issuer": "https://idp.example"}
        ]

    Returns an empty list on missing file or malformed JSON —
    operators see the empty trust anchor list and decide whether
    that's acceptable for their deployment. Surfacing as a hard
    error here would block boot for misconfigurations that the
    operator may explicitly want to roll out gradually.
    """
    from pathlib import Path
    p = Path(path).expanduser()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        return [a for a in data if isinstance(a, dict)]
    if isinstance(data, dict):
        anchors = data.get("anchors") or []
        if isinstance(anchors, list):
            return [a for a in anchors if isinstance(a, dict)]
    return []


__all__ = [
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "DEFAULT_JWKS_TTL_SECONDS",
    "load_trust_anchors",
    "reset_cache_for_tests",
    "resolve_issuer_trust",
]
