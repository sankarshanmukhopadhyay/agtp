"""
MerchantHook — before_dispatch gate for PURCHASE.

The hook runs once per request before the handler. For PURCHASE
requests, it verifies the inbound request is correctly addressed to
this merchant (Merchant-ID match), references an unchanged manifest
(Merchant-Manifest-Fingerprint match), and the merchant is active.
All other methods pass through.

Cost per request:
  * One ``ctx.method == "PURCHASE"`` check.
  * For PURCHASE: O(1) header reads + one sha256 over the merchant's
    AgentDocument JSON. The fingerprint could be cached if the
    overhead becomes measurable; today it's a fresh hash per request
    (a few microseconds even for sizeable documents).

This is Phase 7's operational answer to the spec's note that PURCHASE
counterparty verification "must happen at the merchant edge, before
the merchant-side application layer parses the request body."
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from agtp.handlers import EndpointContext
from core import status as _status
from core import wire


_PURCHASE = "PURCHASE"


class MerchantHook:
    """Dispatch hook gating PURCHASE on merchant identity verification.

    Construction takes a ``strict`` flag. In strict mode, PURCHASE
    requests missing the ``Merchant-ID`` header are refused with 458.
    In default (non-strict) mode, missing headers pass through with a
    one-line stderr warning — useful during the rollout window when
    older clients haven't been upgraded.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict

    def before_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        server_state: Any,
    ) -> Optional[wire.AGTPResponse]:
        if ctx.method != _PURCHASE:
            return None

        # Resolve the targeted AgentDocument from the registry.
        lookup = getattr(server_state, "lookup", None)
        target = lookup(ctx.agent_id) if lookup is not None else None
        if target is None:
            # No AgentDocument loaded for this agent_id. The daemon's
            # regular routing should have rejected this before we got
            # here; fall through (the handler will produce its own
            # 404 if it gets that far).
            return None

        if getattr(target, "role", "agent") != "merchant":
            # PURCHASE against a non-merchant: not this hook's job.
            # The daemon's soft-deny gate refuses PURCHASE on agents
            # that don't declare it; if the agent declares PURCHASE
            # but isn't a merchant, that's a configuration bug we let
            # the handler surface naturally.
            return None

        if getattr(target, "status", "active") != "active":
            return _refuse(
                ctx, target,
                reason=f"merchant-not-active (status={target.status!r})",
            )

        merchant_id_header = _read_header(ctx, "merchant-id")
        if not merchant_id_header:
            if self.strict:
                return _refuse(
                    ctx, target,
                    reason="missing-merchant-id-header",
                )
            sys.stderr.write(
                f"[mod_merchant] PURCHASE without Merchant-ID header from "
                f"{ctx.agent_id[:12]}... — accepting in non-strict mode\n"
            )
        else:
            if merchant_id_header.lower() != target.agent_id.lower():
                return _refuse(
                    ctx, target,
                    reason="merchant-id-mismatch",
                    request_value=merchant_id_header,
                )

        fp_header = _read_header(ctx, "merchant-manifest-fingerprint")
        if fp_header:
            actual = target.manifest_fingerprint()
            if fp_header.lower() != actual.lower():
                return _refuse(
                    ctx, target,
                    reason="merchant-manifest-fingerprint-mismatch",
                    request_value=fp_header,
                    actual_value=actual,
                )
        elif self.strict:
            return _refuse(
                ctx, target,
                reason="missing-merchant-manifest-fingerprint-header",
            )

        return None


def _read_header(ctx: EndpointContext, name_lower: str) -> str:
    """EndpointContext.headers is already lowercased by the
    dispatcher; check the canonical name plus a graceful capitalized
    fallback."""
    if not ctx.headers:
        return ""
    return (
        ctx.headers.get(name_lower)
        or ctx.headers.get(name_lower.title())
        or ""
    )


def _refuse(
    ctx: EndpointContext,
    target: Any,
    *,
    reason: str,
    request_value: Optional[str] = None,
    actual_value: Optional[str] = None,
) -> wire.AGTPResponse:
    """Build a 458 Counterparty Unverified response. The body
    carries enough structured detail for a payment-network verifier
    to log and branch."""
    body: dict = {
        "error": {
            "code": "counterparty-unverified",
            "reason": reason,
            "explanation": (
                f"PURCHASE counterparty verification failed: {reason}"
            ),
            "merchant_id": target.agent_id,
        }
    }
    if request_value is not None:
        body["error"]["request_value"] = request_value
    if actual_value is not None:
        body["error"]["actual_value"] = actual_value
    return _status._build(_status.COUNTERPARTY_UNVERIFIED, body=body)
