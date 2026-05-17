# mod_proxy

Forward AGTP requests to an upstream `agtpd`.

`mod_proxy` is an operational module — loaded into `agtpd`'s own
process via `--load-module`. Once installed, it registers a
resolver for the `proxy` handler-binding type, letting endpoint
TOMLs declare upstream AGTP servers.

Differs from the built-in `external_service` binding (which proxies
to **HTTP** upstreams) in that the upstream speaks **AGTP**.

## When to use it

- **Federation.** A central `agtpd` routes a subset of methods to
  partner servers without putting the partners' addresses into the
  client's view.
- **Edge termination.** Terminate TLS at an edge node; proxy
  plaintext to internal `agtpd` instances on a private network.
- **Sharding / load balancing.** Distribute load across worker
  pools behind one public address.

## Install

```bash
python -m server 4480 \
    --agents-dir agents/ \
    --endpoints-dir endpoints/ \
    --load-module mod_proxy
```

## Declare a proxied endpoint

Drop a TOML file into `endpoints/` whose handler is a proxy
binding:

```toml
[endpoint]
method = "BOOK"
path   = "/room"
description = "Forwards room bookings to the partner reservation service."

[endpoint.handler]
type = "proxy"
url  = "agtp://reservations.partner.com"

# input / output / errors can mirror the upstream's contract OR be
# omitted to let the upstream validate; the daemon's input schema
# still applies before the proxy fires.
```

The proxy preserves:

| Inbound header  | Forwarded as     | Notes |
|-----------------|------------------|-------|
| `Agent-ID`      | `Agent-ID`       | Upstream authenticates the same agent |
| `Principal-ID`  | `Principal-ID`   | Forwarded when present |
| `Session-ID`    | `Session-ID`     | Forwarded when present |
| `Task-ID`       | `Task-ID`        | Forwarded when present |
| `Authority-Scope` | `Authority-Scope` | Comma-joined |

The request body is forwarded as JSON. The upstream's response body
becomes the proxy's response body; the upstream's status code is
preserved.

## Error mapping

When the upstream returns an error status (≥400), `mod_proxy` wraps
the failure in an `EndpointError`:

| Code                          | When it fires                                       |
|-------------------------------|-----------------------------------------------------|
| `proxy_unavailable`           | The daemon's AGTP client is not available           |
| `proxy_upstream_unreachable`  | Connection failed (DNS, TLS, network)               |
| `proxy_upstream_malformed`    | Upstream returned non-JSON                          |
| `proxy_upstream_error`        | Upstream returned status ≥400                       |

Endpoint TOMLs that declare proxy bindings should include these
codes in their `errors` list so the dispatcher's
declared-error-only gate doesn't refuse them.

## What this module does not do (v1)

- **No connection pooling.** One outbound connection per inbound
  request. Adequate for federation and sharding at modest scale;
  high-traffic deployments will want pooling, which is queued for
  the gateway protocol's `outbound_call` capability (gateway v2).
- **No retry / circuit breaker.** Upstream failure surfaces as
  `proxy_upstream_unreachable` immediately.
- **No identity transformation.** The upstream sees the original
  agent's `Agent-ID`. For deployments where the proxy should
  present its own identity (acting on behalf of the agent rather
  than as the agent), write a `registered_function` handler that
  does the outbound call with the desired identity.
- **No request rewriting.** Method, path, and input flow through
  unchanged. To rewrite, again use a `registered_function`.
