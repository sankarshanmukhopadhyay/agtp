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

### Added

- `agtp.registry` module: `HandlerRegistry` class, process-wide
  `registry` instance, and the `@endpoint` decorator for declaring
  Python AGTP handlers without touching server-side internals.
- `agtp.testing` module: `make_context`, `assert_ok`, `assert_error`
  for unit-testing handlers without `agtpd` or the gateway socket.
- Package-root re-exports of `EndpointContext`, `EndpointResponse`,
  `EndpointError`, `endpoint`, and `registry` so most handler code
  needs only `from agtp import ...`.

### Notes

This is the M3 step (a) skeleton: package shape and public API only.
The `@endpoint` decorator registers handlers into `agtp.registry`,
but the daemon's dispatch path is unchanged. Existing handlers
registered through TOML endpoint declarations + dotted-path
`handler_reference` continue to work exactly as before. The
`agtp.registry` surface is the forward-compatible scaffolding that
`mod_python` will consume in M3 step (b) when the gateway socket
goes live.

## [0.1.0] — pre-history

Three dataclasses lifted from the monolithic Python implementation,
present since before this changelog began:

- `EndpointContext` — per-request envelope handed to handler code.
- `EndpointResponse` — handler success shape.
- `EndpointError` — handler declared-failure shape.

Frozen as `EndpointContext` v1.0.0, `EndpointResponse` v1.0.0, and
`EndpointError` v1.0.0 in `core/schemas/` (see
[`core/schemas/CHANGELOG.md`](../core/schemas/CHANGELOG.md)).
