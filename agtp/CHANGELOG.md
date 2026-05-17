# agtp-python changelog

The handler-author-facing Python library for AGTP. Pairs with
`mod_python` (the runtime module that connects to `agtpd` over the
gateway socket).

The rule mirrors `core/schemas/CHANGELOG.md`: if a change affects the
public API surface re-exported from `agtp/__init__.py` or
`agtp.testing`, it lands here. Internal-only refactors do not.

## Versioning

The package versions independently of the AGTP wire format, the
method catalog, and the gateway protocol. Major bumps coordinate with
gateway-protocol majors (see
[`docs/architecture/gateway-protocol-v1.md` §12.1](../docs/architecture/gateway-protocol-v1.md#121-when-v2-cuts)).
Minor bumps add features; patch bumps fix bugs or clarify
documentation.

## [Unreleased]

### Added — Phase C: DaemonClient (sign_request + outbound_call)

- `agtp.DaemonClient` protocol — handler-side view of the daemon's
  gateway capabilities. Implementations expose `sign(bytes) -> bytes`
  and `fetch(uri, method, ...) -> OutboundResponse`. Runtime modules
  set `EndpointContext.daemon` to an implementation; in-daemon
  dispatch leaves it `None`.
- `agtp.OutboundResponse` dataclass — `status`, `headers`, `body`.
- `agtp.DaemonError` exception with `code` attribute — raised when
  the daemon refuses the request (`capability_not_claimed`,
  `signing_unavailable`, `upstream_unreachable`, etc.). Handlers
  catch and translate to declared `EndpointError`s.
- `EndpointContext.daemon: Optional[Any]` field — the
  DaemonClient instance (typed as `Any` to avoid a circular import;
  `runtime_checkable` protocol in `agtp.handlers`).

Handlers running under `mod_python` can now call
`ctx.daemon.sign(b"data")` to request an Ed25519 signature from
the daemon (key stays in the daemon) or `ctx.daemon.fetch("agtp://...")`
to make an outbound AGTP call via the daemon's connection pool.
Other runtime modules pick up the same surface when they're updated.

### Added — Phase B: Agent-Cert / mTLS trust signals

- `EndpointContext.agent_verified: bool` — true when the daemon
  verified an Agent Certificate during the TLS handshake and the
  cert-derived Agent-ID matches the request's `agent_id`.
- `EndpointContext.agent_cert_fingerprint: Optional[str]` — SHA-256
  of the verified cert DER, hex-encoded.

Handlers can now branch on `ctx.agent_verified` to apply different
trust levels (e.g., refuse high-impact methods for header-only
authenticated agents). Existing handlers that don't read these
fields continue to work unchanged.

### Added — M3 step (a): handler-author API skeleton

- `agtp.registry` module: `HandlerRegistry` class, process-wide
  `registry` instance, and the `@endpoint` decorator for declaring
  Python AGTP handlers without touching server-side internals.
- `agtp.testing` module: `make_context`, `assert_ok`, `assert_error`
  for unit-testing handlers without `agtpd` or the gateway socket.
- Package-root re-exports of `EndpointContext`, `EndpointResponse`,
  `EndpointError`, `endpoint`, and `registry` so most handler code
  needs only `from agtp import ...`.
- `EndpointContext.principal_id` — identifier of the human or entity
  the calling agent acts on behalf of. Optional, defaults to `""`.
  Mirrors the field in the gateway request envelope.

### Added — M3 step (b): gateway socket goes live

- `mod_python` sibling package (`mod_python.GatewayClient`,
  `python -m mod_python --gateway-socket ...`). Connects to `agtpd`
  over the gateway socket and serves `@endpoint`-decorated handlers
  out-of-process. End-to-end coverage in `tests/test_gateway_e2e.py`.
- `agtpd --gateway-socket` flag routes `registered_function`
  endpoints through a connected runtime module instead of importing
  them in-daemon. Composition / external_service / embedded methods
  continue to resolve in-daemon. When no module is connected,
  gateway-bound endpoints return 503 `gateway_unavailable`.

### Added — M3 step (c): gateway as the recommended path

- README's Quick Start now features gateway-mode invocation as the
  recommended shape for production-flavor deployments.
- `resolve_registered_function`'s docstring marks it as the legacy
  in-daemon path. The function is retained for unit tests, legacy
  deployments, and the embedded methods path; full removal is a
  future major-version event.
- `register_resume` (gateway spec §6.4) implemented on both daemon
  and module sides — fast reconnect for PHP-FPM-style worker
  recycling without retransmitting schemas. Covered by
  `tests/test_gateway_resume.py`.

## [0.1.0] — pre-history

Three dataclasses lifted from the monolithic Python implementation,
present since before this changelog began:

- `EndpointContext` — per-request envelope handed to handler code.
- `EndpointResponse` — handler success shape.
- `EndpointError` — handler declared-failure shape.

Frozen as `EndpointContext` v1.0.0, `EndpointResponse` v1.0.0, and
`EndpointError` v1.0.0 in `core/schemas/` (see
[`core/schemas/CHANGELOG.md`](../core/schemas/CHANGELOG.md)).
