"""Merchant counterparty and Intent Assertion enforcement module."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from mod_merchant.hook import MerchantHook
from mod_merchant.replay_store import InMemorySeenJtiStore, SeenJtiStore

__all__ = ["InMemorySeenJtiStore", "MerchantHook", "SeenJtiStore", "install"]

def _load_public_key(path: str):
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Intent Assertion verification key must be Ed25519")
    return key

def install(server_state: Any) -> None:
    """Register a fail-closed merchant hook.

    Production/default behavior requires an Ed25519 verification key via
    ``AGTP_MOD_MERCHANT_INTENT_VERIFY_KEY``. Legacy deployments must
    explicitly set ``AGTP_MOD_MERCHANT_ALLOW_UNVERIFIED_INTENT=1``.
    """
    strict = os.environ.get("AGTP_MOD_MERCHANT_STRICT", "1") == "1"
    allow_unverified = os.environ.get(
        "AGTP_MOD_MERCHANT_ALLOW_UNVERIFIED_INTENT", "0"
    ) == "1"
    key_path = os.environ.get("AGTP_MOD_MERCHANT_INTENT_VERIFY_KEY", "")
    if not key_path and not allow_unverified:
        raise ValueError(
            "mod_merchant requires AGTP_MOD_MERCHANT_INTENT_VERIFY_KEY; "
            "set AGTP_MOD_MERCHANT_ALLOW_UNVERIFIED_INTENT=1 only for "
            "explicit legacy/development operation"
        )
    public_key = _load_public_key(key_path) if key_path else None
    hook = MerchantHook(
        strict=strict,
        jti_store=InMemorySeenJtiStore(),
        intent_public_key=public_key,
        require_intent_assertion=not allow_unverified,
    )
    server_state.hook_registry.register(hook)
