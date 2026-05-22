# RCNS — Runtime Contract Negotiation Substrate

RCNS is AGTP's mechanism for serving endpoints that don't statically
exist. When a request arrives at a `(method, path)` pair that no
registry resolves, RCNS-enabled servers can attempt to *synthesize*
a binding on the fly via the existing PROPOSE machinery. The caller
either previews the proposed contract (confirm-first) or accepts
inline execution (optimistic) — in both cases the resulting
`synthesis_id` is bound to the originating Agent-ID for the
contract's lifetime.

RCNS is opt-in by design at multiple layers. The default daemon
posture is "RCNS off"; an operator enables it deliberately; an
agent's request opts in via a header; the agent must hold the
`rcns:negotiate` scope; and the agent must meet a configured trust
tier. Four locks, each held independently by a different actor.

> **See also.** [Endpoint tiers](endpoint-tiers.md) — the Tier
> A/B/C taxonomy. [PROPOSE](propose.md) — the explicit-PROPOSE
> surface RCNS shares its synthesis machinery with.

## The four locks

The dispatcher gate evaluates each lock cheapest-first. The order
matters for failure modes — a low-cost lock failing keeps the
expensive ones from running.

| # | Lock | Source | Closed → response |
|---|---|---|---|
| 1 | `[policies.rcns].enabled = true` | Server config | Falls through to 404 (silent) |
| 2 | `Allow-RCNS: true` or `optimistic` | Request header | Falls through to 404 (silent) |
| 3 | `rcns:negotiate` in agent scopes | AgentDocument | **262** `type = "scope-required"` |
| 4 | `trust_tier ≤ min_trust_tier` | AgentDocument | **464** `reason = "trust-tier-insufficient"` |

Locks 1 and 2 fall through silently because they represent the
absence of any RCNS intent — the server doesn't advertise the
mechanism (1) or the caller didn't ask for it (2). Locks 3 and 4
fail loudly with structured refusals because the caller did opt in
but doesn't satisfy the policy.

The trust-tier comparison follows the existing AGTP convention:
**lower numbers are stronger trust.** `min_trust_tier = 1` admits
only Tier 1 agents; `2` admits Tier 1 and Tier 2; `3` admits any
declared posture.

## Two delivery modes

Both modes run the synthesis runtime against the unregistered
`(method, path)`. They differ in what the gate returns once a
plan is in hand.

### Confirm-first — `Allow-RCNS: true`

The default. Two round-trips, safest for low-trust callers.

```
Client → Server   QUERY /patterns
                  Allow-RCNS: true
                  Agent-ID: <agent>

Server → Client   461 RCNS Contract Available
                  Body: {
                    "contract": {
                      "method": "QUERY",
                      "path": "/patterns",
                      "plan_summary": "passthrough to QUERY",
                      "recipe_name": null,
                      "recipe_version": null,
                      "policy_name": "passthrough"
                    },
                    "proposed_synthesis_id": "syn-AbCdEf..."
                  }

Client → Server   QUERY /patterns
                  Synthesis-Id: syn-AbCdEf...
                  Agent-ID: <agent>

Server → Client   200 OK  (handler result)
```

The caller inspects the contract before any execution. Re-issuing
with the `Synthesis-Id` header runs the plan through the same
dispatcher path that explicit-PROPOSE syntheses use — authority
checks, audit recording, the lot.

### Optimistic — `Allow-RCNS: optimistic`

One round-trip. The gate synthesizes, instantiates, and executes
inline.

```
Client → Server   QUERY /patterns
                  Allow-RCNS: optimistic
                  Agent-ID: <agent>

Server → Client   200 OK  (handler result)
                  Contract-Synthesized: syn-AbCdEf...
```

The response carries the same body the handler produces directly
(200/4xx/5xx as appropriate); the `Contract-Synthesized` response
header surfaces the bound `synthesis_id` so the caller can reuse
it for subsequent invocations without re-negotiating.

Optimistic mode trades preview safety for latency. Use it for
high-volume callers running against trusted servers; use
confirm-first elsewhere.

## Refusal vocabulary (464)

When the gate runs but cannot deliver, it returns **464 RCNS No
Contract** with a structured `reason`. Distinct from 463 (which is
reserved for *explicit* PROPOSE refusals) so chain inspectors can
separate negotiation outcomes from PROPOSE outcomes.

| Reason | Meaning | When |
|---|---|---|
| `composition-impossible` | No policy returned a plan | Honest negative — synthesis runtime tried every policy and none could fulfill |
| `synthesis-error` | Runtime raised | Operator should consult the high-fidelity audit record; the response body's `details.exception` names the class |
| `trust-tier-insufficient` | Lock 4 closed | Agent's trust tier didn't meet `min_trust_tier` |
| `rcns-disabled` | Lock 1 closed (reserved) | The dispatcher gate currently falls through silently; this code is reserved for explicit "RCNS not on" responses (e.g., a DISCOVER /patterns endpoint surfacing server posture) |
| `contract-not-yours` | Cross-agent contract presentation | Synthesis_id was negotiated for a different Agent-ID; not transferable |
| `contract-revoked` | Operator revoked the binding (RCNS-4) | Caller should re-negotiate from scratch |

## Contract scoping

Every RCNS-spawned `synthesis_id` is bound to the **originating
Agent-ID**. The dispatcher checks this on every Synthesis-Id
request:

```
if originating_agent_id != request.agent_id:
    return 464 contract-not-yours
```

This applies to RCNS-spawned syntheses and any explicit PROPOSE
that opts into the same scoping. Pre-RCNS-3 explicit PROPOSE
callers that don't set the field run unscoped (any caller can
present the id).

Cross-agent delegation — Agent A negotiates a contract and shares
it with Agent B — is out of scope for v00. The data model reserves
a `delegatable_to: [agent_id]` field for a future phase; current
implementation ignores it.

## Abuse mitigations

### Per-agent rate limit

`[policies.rcns].max_negotiations_per_minute` (default `10`)
enforces a per-agent rolling window. Exceeded calls return
**429** with `error.scope = "rcns"` so callers can distinguish
negotiation throttling from ordinary request throttling. Setting
the limit to `0` disables this gate.

The limit applies independently of the request rate limit. A
chatty agent that's well-behaved on ordinary requests can still
hit the RCNS limit by spamming unregistered paths.

### Idempotency key

Repeated negotiations with the same `RCNS-Idempotency-Key` header
from the same agent return the cached `synthesis_id` without
re-running composition. The cache key is `(agent_id, idempotency_key)`;
the window is `[policies.rcns].idempotency_window_seconds`
(default `60`).

Use this for retry loops: if the caller times out and retries with
the same key, the second negotiation returns the original
synthesis_id rather than spawning a duplicate. Setting the window
to `0` disables the cache.

### No recursive RCNS

Requests carrying `Synthesis-Id` skip the RCNS gate entirely —
they're already inside a contract. Steps dispatched from inside a
synthesis go through the dispatcher with the same scoping; they
either resolve in the registry or 404 normally. The runtime never
spawns nested negotiations.

## Audit integration

When a request dispatches through a synthesis (RCNS-spawned or
PROPOSE-spawned), the Attribution-Record carries three additional
fields under the existing extension block:

| Field | Value |
|---|---|
| `synthesis_id` | The contract under which the action ran |
| `contract_hash` | sha256 of the canonical-JSON contract shape |
| `negotiation_origin` | `propose-explicit` / `rcns-confirmed` / `rcns-optimistic` |

`contract_hash` is the key field for chain analysis: two
syntheses with identical contract shape share a hash, so the
inspector can group invocations by contract identity even across
re-negotiations.

These fields surface automatically — handlers don't populate them.
The dispatcher reads the synthesis runtime's side-tables at
response-stamping time.

### RCNS lifecycle events

In addition to per-invocation Attribution-Records, RCNS-4 emits
three lifecycle events onto the **originating agent's** existing
per-agent lifecycle stream (the same stream `ACTIVATE` / `REVOKE`
write to). Same JWS / SCITT dual-mode as ordinary lifecycle
events; no new store, no new file format. Events have
``event_type`` values prefixed ``rcns_``:

| Event type | When | Actor |
|---|---|---|
| `rcns_propose_accepted` | RCNS gate instantiated a contract | The negotiating agent |
| `rcns_revoke` | Operator called `REVOKE target=contract` | Caller (originator or `inspect:all`) |
| `rcns_release` | Agent called `SUSPEND` with a `synthesis_id` | Caller |

Each event's `extra` payload carries the contract lineage
(`synthesis_id`, `contract_hash`, `method`, `path`, `recipe_name`,
`recipe_version`, `negotiation_origin`) plus `actor_agent_id` —
the Agent-ID of the caller who triggered the event (relevant for
operator revocations where actor ≠ originator).

Emission is best-effort. Deployments without
`[audit].attribution_records_enabled = true` skip the audit write
entirely — RCNS still works in-memory, the durable trail just
isn't there. The 200/461/263 response carries an `audit_id` only
when the event was actually written.

## Observability surfaces

### `DISCOVER /patterns`

Tier A endpoint. Returns the RCNS posture plus the patterns the
server will negotiate on:

```json
{
  "target": "patterns",
  "rcns": {
    "enabled": true,
    "min_trust_tier": 1,
    "max_negotiations_per_minute": 10,
    "idempotency_window_seconds": 60,
    "on_policy_change": "grandfather",
    "modes": ["confirm-first", "optimistic"]
  },
  "patterns": [
    {
      "kind": "recipe",
      "policy_name": "recipes",
      "name": "recon-on-accounts",
      "version": "2",
      "match": {
        "name_exact": "RECONCILE",
        "path_regex": "^/accounts/.*$",
        ...
      },
      "step_count": 1,
      "underlying_methods": ["QUERY"]
    },
    {"kind": "policy", "policy_name": "passthrough"}
  ]
}
```

Always reachable, even when RCNS is disabled (so callers can
discover the server's posture without speculating).

### `DISCOVER /contracts`

Tier A endpoint. Returns currently-bound syntheses owned by the
caller. With the `inspect:all` scope (operator visibility), returns
every contract on the server. Each entry: `synthesis_id`,
`method`, `path`, `originating_agent_id`, `contract_hash`,
`negotiation_origin`, `recipe_name`, `recipe_version`,
`policy_name`, `persistent`, `expires_at`.

### `INSPECT target=contract`

Detail view. Body: `{target: "contract", synthesis_id: "syn-..."}`.
Returns the full plan, recipe lineage, expiration, contract hash.
ACL: caller's own contracts; `inspect:all` reaches across.

### `INSPECT target=rcns-attempt`

Diagnostic for a failed 464. Body:
`{target: "rcns-attempt", attempt_id: "rcns-..."}`. Returns the
recorded diagnostic — which policies were tried, what reason
fired, when the attempt happened.

Every 464 response carries an `RCNS-Attempt-Id` response header
that operators can copy/paste into the INSPECT call. Diagnostics
live in an in-memory ring buffer (last 200 attempts); evicted
entries return 404 `rcns-attempt-not-found`.

## Contract lifecycle

### `REVOKE target=contract`

Operator surface for revoking a bound contract. Body:
```json
{"target": "contract", "synthesis_id": "syn-...", "reason": "..."}
```

Authorization: the contract's originator OR a caller with
`inspect:all` scope. On success: the runtime evicts the
synthesis_id immediately and an `rcns_revoke` audit event is
written to the originating agent's lifecycle stream. Subsequent
presentations of the revoked synthesis_id return 404
`synthesis-not-found` — the runtime no longer recognizes the id.

The 464 vocabulary's `contract-revoked` reason is reserved for
future scenarios where revocation needs to survive runtime
eviction (e.g., persistent contracts surviving a restart). v00 is
"revoked = gone."

### `SUSPEND` with `synthesis_id`

Agent-side release. Body: `{synthesis_id: "syn-...", reason: "..."}`.
Clears the contract from the runtime (same effect as REVOKE) and
writes an `rcns_release` audit event onto the originating agent's
stream. The response body's `rcns_release_audit_id` field surfaces
the resulting audit_id when an event was written.

Use SUSPEND for clean-up: an agent done with a contract releases
it politely rather than waiting for TTL expiry.

## Configuration reference

```toml
[policies.rcns]
enabled                       = true
min_trust_tier                = 1            # 1 strictest, 3 most permissive
max_negotiations_per_minute   = 10           # per agent; 0 = unlimited
idempotency_window_seconds    = 60           # 0 disables the cache
on_policy_change              = "grandfather"  # or "invalidate" — RCNS-4
```

All keys are optional; defaults give the safest posture (`enabled
= false`).

## Out of scope (v00)

- **Cross-server RCNS** — one daemon's negotiation reaching into
  another's primitives. Needs cross-daemon trust + audit-chain
  extensions; future phase.
- **LLM-driven composition policies** — recipe + passthrough are
  the v00 policy surface. New policies plug into the existing
  `CompositionPolicy` protocol; no RCNS-3 wire changes required.
- **Promoting RCNS contracts into the Tier B registry** — useful
  governance question; future phase.
- **RCNS via the HTTP gateway** — RCNS is AGTP-native only. REST
  clients calling `mod_http_gateway` against an unknown path get a
  404; the gateway does not negotiate.
- **Delegatable contracts** — `delegatable_to` is reserved in the
  schema, not honored. Cross-agent contract reuse requires
  explicit delegation; current implementation refuses with
  `contract-not-yours`.

## See also

- [Endpoint tiers](endpoint-tiers.md) — Tier A/B/C taxonomy.
- [PROPOSE](propose.md) — explicit-PROPOSE surface (RCNS-2's
  endpoint-keyed form is what RCNS internally calls).
- [`server/rcns_gate.py`](../server/rcns_gate.py) — the gate
  implementation.
- [`server/synthesis/`](../server/synthesis/) — the composition
  runtime RCNS shares with explicit PROPOSE.
