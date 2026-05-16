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
