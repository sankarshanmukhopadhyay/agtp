# Canonical schema changelog

This changelog tracks every change to the **public-contract schemas**
in this directory. The rule is simple: if a schema's content changes,
it lands here. If a shape is not listed here, it is not part of the
public contract.

The changelog is the load-bearing artifact of the public/internal
boundary documented in [README.md](README.md). When a contributor
promotes an internal type to a public schema, the promotion shows up
as an entry below. When a public schema gains an optional field, the
minor bump is recorded. When a breaking change is required, it cannot
land inside the current major version — it waits for the next gateway
protocol major.

## Versioning

Each schema carries its own `$id` and is versioned independently.
Entries in this file are grouped by **release date**; within an
entry, each schema lists the version transition.

- **Major bump** (e.g. v1 → v2): breaking change. Only allowed when
  the gateway protocol itself cuts a new major (see
  [`gateway-protocol-v1.md` §12.1](../../docs/architecture/gateway-protocol-v1.md#121-when-v2-cuts)).
- **Minor bump** (e.g. v1.0 → v1.1): additive, backward-compatible.
  New optional field, new enum value, looser constraint.
- **Patch bump** (e.g. v1.0.0 → v1.0.1): documentation-only or
  description-only edit; no shape change.

A change that does not appear here did not happen as far as the
ecosystem is concerned. Drift detection (`tests/schemas/`) is the
enforcement.

## [1.0.0] — 2026-05-15

Initial schema freeze. Six public-contract schemas:

- **EndpointContext** v1.0.0 — per-request envelope handed across the
  gateway. Lifted from `agtp.handlers.EndpointContext`.
- **EndpointResponse** v1.0.0 — handler success shape. Lifted from
  `agtp.handlers.EndpointResponse`.
- **EndpointError** v1.0.0 — handler declared-failure shape. Lifted
  from `agtp.handlers.EndpointError`.
- **AgentDocument** v1.0.0 — v2 Agent Document. Lifted from
  `core.identity.AgentDocument`.
- **ServerManifest** v1.0.0 — server-level DISCOVER response. Lifted
  from `core.manifest.ServerManifest`, with `ServerInfoBlock`,
  `PolicyBlock`, `APIEndpoint`, and `HostedProtocol` frozen
  transitively.
- **GatewayHandshake** v1.0.0 — all v1 gateway frames (`hello`,
  `welcome`, `register`, `register_resume`, `register_ack`,
  `request`, `response`, `error`, `ping`, `pong`, `goodbye`).
  No source dataclass; defined directly from
  [`gateway-protocol-v1.md`](../../docs/architecture/gateway-protocol-v1.md).

Frozen for the lifetime of gateway protocol v1.
