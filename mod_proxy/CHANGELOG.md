# mod_proxy changelog

Operational module: forward AGTP requests to an upstream agtpd.

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major
version. Minor bumps add features. Patch bumps fix bugs.

## [Unreleased]

### Added — M9 initial release

Initial operational module. Adds the `proxy` handler-binding type
to the daemon.

- `mod_proxy.resolve_proxy` — handler factory that returns a
  closure forwarding inbound requests to the upstream agtpd's
  `(method, path)`. Preserves `Agent-ID`, `Principal-ID`,
  `Session-ID`, `Task-ID`, and `Authority-Scope` headers.
- `mod_proxy.install(server_state)` — boot hook called by the
  daemon. Patches `server.handler_resolution` so the `proxy`
  binding type resolves correctly.
- `proxy` added to `core.endpoint.ALL_HANDLER_TYPES`. Binding
  validation accepts `type = "proxy"` with a `url = "agtp://..."`.

### Error mapping

`proxy_upstream_unreachable`, `proxy_upstream_malformed`,
`proxy_upstream_error`, `proxy_unavailable` — the four declared
error codes the proxy may surface. Endpoint TOMLs that bind to a
proxy upstream should include these in their `errors` list.

### Known limits

- No connection pooling. One outbound socket per inbound request.
- No retry / circuit breaker.
- No identity transformation; the upstream sees the original agent.
- No request rewriting.

Higher-quality versions of these are deferred to a future revision
or to operator-authored `registered_function` handlers.
