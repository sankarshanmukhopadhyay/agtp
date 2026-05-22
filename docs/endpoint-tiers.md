# Endpoint tiers

Every `(method, path)` endpoint on an AGTP server falls into one of
three tiers. The classification is mechanical — there is a single
source of truth in [`core/endpoint_tiers.py`](../core/endpoint_tiers.py)
— and it drives a real protocol distinction. The tier of an endpoint
governs:

- whether operator policy can disable it,
- which dispatcher gates apply,
- how it shows up in DISCOVER output,
- whether the daemon may *synthesize* it on demand (Tier C only).

This document is the canonical reference. The two terms that look
similar but shouldn't be confused:

- **Method** — an entry in the open AGTP verb catalog
  ([`core/methods.json`](../core/methods.json)). A *vocabulary*.
  Always available regardless of any server's configuration.
- **Endpoint** — a `(method, path)` binding on a particular server.
  A *capability*. Available only because *this* server registered
  it (or, under RCNS, negotiated it on demand).

> "Methods are an open vocabulary on the protocol. Endpoints are a
> closed registration on a particular server. RCNS is the runtime
> contract that bridges the two when no static registration exists."

## The three tiers

### Tier A — Native (protocol layer)

Endpoints the daemon implements directly. Every conformant AGTP
server answers to every Tier A endpoint identically. Operator
policy cannot disable them, redirect them, or change their schema.
The dispatcher's `[policies.methods]` allow / disallow / redirects
chain does not apply.

Tier A is the protocol's self-describing surface. An agent that
walks a Tier A endpoint always learns the same thing on any
server.

The Tier A inventory today:

| Method | Path | Notes |
|---|---|---|
| `DISCOVER` | `/` | Endpoint directory (the bare-root index) |
| `DISCOVER` | `/methods` | Endpoint inventory (lightweight) |
| `DISCOVER` | `/agents` | Agents hosted on this server |
| `DISCOVER` | `/tools` | Tools the server exposes |
| `DISCOVER` | `/apis` | External APIs the server fronts |
| `DISCOVER` | `/genesis` | The agent's Genesis (when one is loaded) |
| `DISCOVER` | `/patterns` | RCNS-negotiable surface (RCNS-4) |
| `DISCOVER` | `/contracts` | Currently-bound contracts (RCNS-4) |
| `QUERY` | `/proposals` | §7 async PROPOSE poll surface |

Lifecycle methods (`ACTIVATE`, `DEACTIVATE`, `REVOKE`, `REINSTATE`,
`DEPRECATE`) dispatched against an Agent-ID, `PROPOSE` itself, and
`INSPECT` with `target=` families are also Tier A but are
method-keyed rather than `(method, path)`-keyed — they apply on the
bare URI plus their target body parameter.

Tier A endpoints are implemented in one of two ways:

- **Dispatched directly by the daemon** (e.g. the reserved DISCOVER
  roots). These never enter the endpoint registry; the dispatcher
  short-circuits them before registry lookup.
- **Registered as built-ins by [`server/builtins.py`](../server/builtins.py)**
  (e.g. `DISCOVER /methods`, `QUERY /proposals`). These appear in
  the registry like any other endpoint but their handler closures
  carry the `__agtp_builtin__` marker so the classifier knows
  they're Tier A.

Both flavors share the same protocol guarantee. The implementation
mechanism is hidden from callers.

#### Adding to Tier A

Adding to the Tier A inventory is a **protocol-level decision**, not
an operator one. The canonical inventory lives in
[`core/endpoint_tiers.py`](../core/endpoint_tiers.py)'s
`TIER_A_RESERVED_ENDPOINTS` frozenset. Any new entry needs:

1. A spec entry in
   [`docs/path-grammar.md`](path-grammar.md) (for reserved
   paths) or this document.
2. A handler in the daemon (either a dispatcher short-circuit or a
   built-in registration through [`server/builtins.py`](../server/builtins.py)).
3. Conformance tests covering the new endpoint across every
   reference deployment.

Operator TOML cannot register a `(method, path)` in
`TIER_A_RESERVED_ENDPOINTS` — the path grammar's
`validate_discover_path` reserves the prefix for protocol use, and
the registry's duplicate-registration check rejects collisions on
the path-keyed reserved roots. The exception is `DISCOVER /methods`
and `QUERY /proposals`: operator TOML *can* declare endpoints at
those paths and the built-in registration silently skips
(`DuplicateEndpointError` is swallowed in
[`register_builtins`](../server/builtins.py)). The operator's
choice wins. This is a deliberate escape hatch for operators who
need different semantics; in practice it is rarely used.

### Tier B — Application (registry layer)

Endpoints declared by operator-authored TOML or contributed by
operational / runtime modules. Tier B endpoints live in the
[`EndpointRegistry`](../server/endpoint_registry.py) and are subject
to the full operator policy chain.

Examples shipping today:

- `DISCOVER /products`, `DISCOVER /customers/active` from operator
  endpoint TOML.
- `PURCHASE /checkout` from `mod_merchant`.
- `RECONCILE /accounts/{id}` from a runtime module.
- Anything bound through
  [`server.endpoint_registry.EndpointRegistry.register`](../server/endpoint_registry.py)
  at startup.

Tier B endpoints can be:

- allowed / disallowed via `[policies.methods]`,
- redirected via `[policies.methods.redirects]`,
- removed by deleting their TOML file or unloading their module,
- and shadowed by Tier A registrations (the duplicate-registration
  rule from §9 of the API spec).

Operator policy gates fire on Tier B and not on Tier A. A
mis-authored `disallow` rule cannot take a server off-protocol.

### Tier C — RCNS (negotiated layer, future)

Endpoints that don't exist at request time but that the daemon can
*synthesize* on demand via the Runtime Contract Negotiation
Substrate. Tier C is the third origin for an endpoint binding —
operator-static (Tier B) and protocol-native (Tier A) are the other
two.

Mechanically, a Tier C endpoint is identical to a Tier B endpoint
*at execution time*: the dispatcher runs the same authority gates,
the same audit chain. The difference is where the binding came
from. A Tier C binding is created by the dispatcher in response to
a request whose `(method, path)` no other tier resolved, when:

1. The server has `[policies.rcns].enabled = true`.
2. The request carries `Allow-RCNS: true` (or `optimistic`).
3. The agent's scopes include `rcns:negotiate`.
4. The agent's resolved `trust_tier ≤ [policies.rcns].min_trust_tier`.

Under those four conditions the dispatcher runs the synthesis
runtime against the unregistered `(method, path)`. A successful
plan yields either a 461 RCNS Contract Available (confirm-first
preview) or a 263 Proposal Approved with `Contract-Synthesized:
<synthesis_id>` (optimistic execution). A failed negotiation
returns 464 RCNS No Contract with a structured reason.

RCNS-1 reserved the status codes and tier classification. RCNS-2
generalized the synthesis runtime to `(method, path)` keying.
RCNS-3 wired the dispatcher gate. RCNS-4 added the observability
surface: `DISCOVER /patterns` (negotiable surface), `DISCOVER /contracts`
(active syntheses), `INSPECT target=contract`,
`INSPECT target=rcns-attempt`, `REVOKE target=contract`, and
`rcns_*` lifecycle audit events. See [`docs/rcns.md`](rcns.md) for
the full wire-level spec.

A Tier C contract is bound for the lifetime of its `synthesis_id`.
When the contract expires (TTL elapses, operator revokes, or agent
releases via `SUSPEND`), subsequent invocations of that
`synthesis_id` return 464 with `reason = contract-revoked` (operator
revocation) or fall back to RCNS re-negotiation (TTL expiry). The
unregistered state — neither Tier A nor B nor C — returns the
ordinary 404 `endpoint-not-found`.

## Classification surface

The canonical lookup is
[`core.endpoint_tiers.classify_tier`](../core/endpoint_tiers.py):

```python
from core.endpoint_tiers import classify_tier

classify_tier("DISCOVER", "/agents")              # "A" — reserved
classify_tier("DISCOVER", "/products", registry=reg)  # "B" — operator-registered
classify_tier("DISCOVER", "/unknown",  registry=reg)  # "unregistered"

# RCNS-3+ — synthesis_lookup parameter active:
classify_tier("DISCOVER", "/patterns", registry=reg,
              synthesis_lookup=syn)                 # "C" — RCNS-spawned
```

Returns one of `"A"`, `"B"`, `"C"`, `"unregistered"`. Callers that
want the full Tier A inventory (reserved + registered builtins)
use `tier_a_inventory(registry)`.

## DISCOVER surface

The bare `DISCOVER /` index annotates each entry with its tier so
discovery output stays self-describing:

```json
{
  "target": "index",
  "endpoints": [
    {"path": "/", "target": "index", "tier": "A", "reserved": true},
    {"path": "/agents", "target": "agents", "tier": "A", "reserved": true},
    ...
  ]
}
```

`DISCOVER /methods` (the registry inventory) carries a `tier` field
per entry once the registry walks the classifier (this lands in
RCNS-3 alongside the dispatcher gate; pre-RCNS-3 the inventory
remains tier-less).

## Status codes used in tier transitions

| Code | Name | Tier interaction |
|---|---|---|
| 200 | OK | Tier A, B, or C succeeded |
| 263 | Proposal Approved | Explicit PROPOSE (Tier C creation) succeeded |
| 404 | Not Found | `(method, path)` is unregistered and RCNS gate did not fire |
| **461** | **RCNS Contract Available** | RCNS confirm-first preview (RCNS-3) |
| 463 | Proposal Rejected | Explicit PROPOSE refused |
| **464** | **RCNS No Contract** | RCNS attempted on caller's behalf; could not deliver (RCNS-3) |

461 and 464 are reserved in
[`core/status.py`](../core/status.py) by RCNS-1 so downstream code
can target stable codes; the dispatcher gate that returns them
ships in RCNS-3.

## See also

- [`docs/path-grammar.md`](path-grammar.md) — Reserved DISCOVER
  roots and the path-grammar rule preventing prefix collision with
  protocol-owned paths.
- [`docs/methods.md`](methods.md) — Method vocabulary versus
  endpoint binding.
- [`docs/propose.md`](propose.md) — Explicit PROPOSE (Tier C
  creation by direct agent invocation).
- [`server/builtins.md`](../server/builtins.md) — Tier A registered
  built-ins shipped by the daemon.
