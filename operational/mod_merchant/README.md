# mod_merchant

PURCHASE counterparty verification for AGTP merchants.

`mod_merchant` registers a `before_dispatch` hook against the
daemon's `HookRegistry`. On every PURCHASE request, the hook
verifies the inbound request is correctly addressed to *this*
merchant. Failures return **458 Counterparty Unverified** before
the handler runs.

## How an agent becomes a merchant

The Agent Genesis is identity-only and immutable — it never carries
a role. To declare an existing agent as a merchant, the operator
adds `"role": "merchant"` to the agent's `*.agent.json` manifest
file. No new Genesis, no new Agent-ID, no broken audit chain. An
agent that later retires its merchant capabilities just removes
the field from the manifest.

Example: starting with a standard agent at
`agents/lauren.agent.json`, you upgrade to merchant simply by
editing:

```json
{
  "name": "lauren",
  ...existing fields...,
  "role": "merchant"
}
```

Reload the daemon (or HUP it in a future revision). The Agent-ID
is unchanged; existing certs and chains still resolve.

## What it enforces

When a request arrives whose dispatched agent has `role: merchant`:

1. **Merchant lifecycle is active.** `agent.status == "active"`,
   else 458 (`merchant-not-active`).
2. **Merchant-ID header matches.** When the buyer sends
   `Merchant-ID`, it must equal the merchant's Canonical Agent-ID.
   Mismatch returns 458 (`merchant-id-mismatch`).
3. **Merchant-Manifest-Fingerprint matches.** When the buyer sends
   `Merchant-Manifest-Fingerprint`, it must equal
   `sha256(canonical AgentDocument JSON)`. Mismatch returns 458
   (`merchant-manifest-fingerprint-mismatch`).

When the dispatched agent's `role` is `agent` (default), or when
the method is anything other than PURCHASE, the hook is a no-op.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `AGTP_MOD_MERCHANT_STRICT` | `0` | When `1`, PURCHASE without `Merchant-ID` is refused with 458 (`missing-merchant-id-header`). Default mode accepts pre-Phase-7 buyers with a stderr warning so existing clients keep working during rollout. |

## When to load it

Load `mod_merchant` when:

- The daemon hosts one or more `role: merchant` agents.
- You want counterparty-verification enforcement at the wire layer.

Servers that only host `role: agent` (the default) can load
`mod_merchant` safely — it's a no-op for them.

## Loading

```bash
agtpd --load-module mod_merchant
```

Combine with `mod_agent_cert` for full Scope-Enforcement-Point
behavior on PURCHASE.

## Status codes

| Code | Reason field | Meaning |
|---|---|---|
| 458 | `merchant-not-active` | Merchant's lifecycle status is not `active`. |
| 458 | `merchant-id-mismatch` | Inbound `Merchant-ID` doesn't match the dispatched agent. |
| 458 | `merchant-manifest-fingerprint-mismatch` | Inbound fingerprint doesn't match recomputed `sha256(AgentDocument JSON)`. |
| 458 | `missing-merchant-id-header` | Strict mode only — inbound PURCHASE lacks `Merchant-ID`. |
| 458 | `missing-merchant-manifest-fingerprint-header` | Strict mode only — inbound PURCHASE lacks `Merchant-Manifest-Fingerprint`. |

## How a buyer prepares a PURCHASE

```python
# 1. DESCRIBE the merchant to get its AgentDocument.
manifest = describe(merchant_uri)

# 2. Compute the fingerprint over the canonical JSON.
fingerprint = hashlib.sha256(manifest.to_canonical_json().encode()).hexdigest()

# 3. Build the Intent Assertion (agtp.intent helper) and send PURCHASE.
asn = build_intent_assertion(
    daemon=ctx.daemon,
    issuer=ctx.agent_id,
    subject=ctx.principal_id,
    audience=manifest.agent_id,
    merchant_id=manifest.agent_id,
    amount="9.99", currency="USD",
    product_ref="sku:coffee-monthly",
)
send_purchase(
    merchant_uri,
    headers={
        "Merchant-ID": manifest.agent_id,
        "Merchant-Manifest-Fingerprint": fingerprint,
    },
    body={"intent_assertion": asn["jwt"], ...},
    attribution_extra={"intent_assertion_jti": asn["jti"]},
)
```

The buyer's daemon stamps the Intent Assertion JTI into the
response's Attribution-Record `extra` block, anchoring the
purchase to the agent's audit chain (Phase 6 INSPECT walks it).

## Cost

O(1) per non-PURCHASE request (early-return on method check).
O(len(AgentDocument JSON)) per PURCHASE for the sha256 — typically
a few KB, microseconds. The fingerprint isn't cached today; if the
overhead becomes measurable, the hook can memoize against
AgentDocument identity.
