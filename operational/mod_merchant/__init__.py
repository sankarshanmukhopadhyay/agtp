"""
mod_merchant — PURCHASE counterparty verification for AGTP merchants.

Loaded by ``agtpd`` via ``--load-module mod_merchant``. The module's
``install(server_state)`` function registers a :class:`MerchantHook`
that runs before every PURCHASE dispatch and verifies the inbound
request is correctly addressed to *this* merchant. Per
``draft-hood-agtp-merchant-identity-02 §4.2`` the hook enforces:

  * The agent the request targets has ``role == "merchant"``.
  * The inbound ``Merchant-ID`` header (if present) equals that
    agent's Canonical Agent-ID.
  * The inbound ``Merchant-Manifest-Fingerprint`` header (if
    present) equals ``sha256`` of the merchant's canonical
    AgentDocument JSON.
  * The agent's lifecycle ``status == "active"``.

Any mismatch returns **458 Counterparty Unverified** before the
handler runs, so a mis-targeted PURCHASE never reaches the
application layer.

When the request targets an agent whose role is not "merchant", the
hook is a no-op for PURCHASE — PURCHASE is only valid against
merchants by design, so a non-merchant target falls through to the
daemon's regular soft-deny gate (which refuses PURCHASE on agents
that don't declare it).

When the inbound request omits ``Merchant-ID`` entirely (legacy
client, or pre-Phase-7 caller), the hook accepts the request but
emits a structured warning via the daemon's log. Operators who want
to refuse legacy traffic outright set
``AGTP_MOD_MERCHANT_STRICT=1``.

Pairs with the registrar's merchant-issuance flow: a merchant
operator runs ``python -m tools.registrar issue --role merchant
...``, drops the resulting ``.genesis.json`` + ``.agent.json`` into
the daemon's ``agents/`` directory, and loads ``mod_merchant``. From
that point every inbound PURCHASE is counterparty-verified at the
wire layer.
"""

from __future__ import annotations

import os
from typing import Any

from mod_merchant.hook import MerchantHook


__all__ = ["MerchantHook", "install"]


def install(server_state: Any) -> None:
    """Boot hook: register MerchantHook. Called by agtpd after
    ``--load-module mod_merchant``."""
    strict = os.environ.get("AGTP_MOD_MERCHANT_STRICT", "0") == "1"
    hook = MerchantHook(strict=strict)
    server_state.hook_registry.register(hook)
