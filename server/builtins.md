# Tier A registered built-ins

This document is the operator-facing catalogue for the Tier A
endpoints that the daemon registers into the endpoint registry at
startup. Companion to [`server/builtins.py`](builtins.py) (the
implementation) and [`docs/endpoint-tiers.md`](../docs/endpoint-tiers.md)
(the full Tier A/B/C taxonomy).

Two implementation paths make an endpoint Tier A:

1. **Dispatcher short-circuit** — the daemon handles the
   `(method, path)` before registry lookup. Examples: the reserved
   DISCOVER roots (`/agents`, `/tools`, `/apis`, `/genesis`, the
   bare `/` index). These never enter the registry.
2. **Registered built-in** — the daemon registers a handler into
   the endpoint registry like any other endpoint, but the handler
   closure carries the `__agtp_builtin__` marker so
   [`core.endpoint_tiers.classify_tier`](../core/endpoint_tiers.py)
   knows it's Tier A. This module covers that second path.

Both produce the same protocol guarantee. The difference is purely
mechanical.

## Built-ins shipped today

### `DISCOVER /methods`

| | |
|---|---|
| Method | `DISCOVER` |
| Path | `/methods` |
| Marker | `__agtp_builtin__ = "discover_methods"` |
| Required scopes | none |
| Output | `methods: array` of `{method, path, description}` entries — one per registered endpoint |
| Operator override | TOML may declare an endpoint at the same `(method, path)` pair; the built-in registration silently skips |

Lightweight inventory of every endpoint the server registered. The
full manifest (via target-less DISCOVER) carries the complete
endpoint shape — semantic block, parameters, handler bindings; this
built-in is for callers that only want the inventory.

### `QUERY /proposals`

| | |
|---|---|
| Method | `QUERY` |
| Path | `/proposals` |
| Marker | `__agtp_builtin__ = "query_proposal"` |
| Required scopes | none |
| Required params | `proposal_id: string` |
| Output | `proposal_id`, `state ∈ {pending, accepted, rejected}`, plus the resolved 263 or 463 body when finished |
| Operator override | yes (same shape as `DISCOVER /methods`) |

§7 async PROPOSE poll. Only registered when the server passes a
`proposal_store` to `register_builtins()` — operators without
`async_evaluation_enabled = true` don't need it. The spec text
refers to this as `QUERY /proposals/{proposal_id}`; v00 implements
it as a literal-path endpoint with `proposal_id` in the body
because the registry's path-template routing is deferred.

## Adding a Tier A built-in

1. Write the handler closure. Attach:
   ```python
   handler.__agtp_handler_kind__ = "registered_function"
   handler.__agtp_builtin__ = "<short_label>"
   ```
   The `__agtp_builtin__` marker is what
   [`core.endpoint_tiers.classify_tier`](../core/endpoint_tiers.py)
   reads to distinguish Tier A registrations from Tier B at lookup
   time. Without it the classifier returns `"B"` even if the
   registration came from this module.
2. Build an `EndpointSpec` declaring the contract.
3. Add a `_register(...)` call inside `register_builtins`. The
   helper swallows `DuplicateEndpointError` so operator overrides
   are silently respected and prints to stderr on
   `InvalidEndpointError`.
4. Update [`core.endpoint_tiers.TIER_A_RESERVED_ENDPOINTS`](../core/endpoint_tiers.py)
   if the new endpoint is also a protocol guarantee. (Most are;
   exceptions are conditional registrations like `QUERY /proposals`
   which only ships when the operator opts into async evaluation.)
5. Cross-reference the new built-in in this catalogue and in
   [`docs/endpoint-tiers.md`](../docs/endpoint-tiers.md).

## See also

- [`core/endpoint_tiers.py`](../core/endpoint_tiers.py) — Tier
  classification function and reserved inventory.
- [`docs/endpoint-tiers.md`](../docs/endpoint-tiers.md) — full
  Tier A/B/C taxonomy.
- [`docs/path-grammar.md`](../docs/path-grammar.md) — reserved
  DISCOVER roots and prefix-shadowing rules.
- [`server/endpoint_registry.py`](endpoint_registry.py) — the
  registry that holds Tier A registered built-ins alongside Tier B
  operator endpoints.
