# External-service handlers

Phase 4 wires the third handler-binding kind: **external_service**. An external_service-bound endpoint declares an HTTPS URL plus a few translation maps; the dispatcher proxies incoming AGTP calls to that URL, translates the response back to AGTP shape, and returns. This is what the position paper calls *wrap-and-expose* — the migration path that makes AGTP usable for organizations with existing HTTPS APIs.

## Authoring an external-service endpoint

```toml
[endpoint]
method = "BOOK"
path = "/room"
description = "Books a room via the StayBeta reservations API."

[endpoint.semantic]
intent = "Reserve a room for the named guest at the named property."
actor = "agent"
outcome = "A confirmed reservation_id is returned."
capability = "transaction"
confidence = 0.85
impact = "irreversible"
is_idempotent = false

[[endpoint.input.required]]
name = "guest_id"
type = "string"
description = "Guest profile identifier."
format = "uuid"

[[endpoint.input.required]]
name = "check_in"
type = "string"
description = "Arrival date."
format = "date"

[[endpoint.input.required]]
name = "room_type"
type = "string"
description = "Room category."
enum = ["single", "double", "suite"]

[[endpoint.output]]
name = "reservation_id"
type = "string"
description = "Server-assigned reservation handle."

[endpoint.errors]
list = [
  "room_unavailable",
  "guest_not_found",
  "invalid_dates",
  "upstream_timeout",
  "upstream_connection_error",
  "upstream_malformed_response",
  "upstream_authentication_failed",
  "upstream_error",
]

# ---- the external_service binding ----
[endpoint.handler]
type = "external_service"
url = "https://api.staybeta.com/v1/reservations"
method = "POST"
timeout_seconds = 30

[endpoint.handler.headers]
"X-API-Key" = "${STAYBETA_API_KEY}"            # env-var substitution
"Content-Type" = "application/json"
"User-Agent" = "agtp-server/0.4"

# AGTP field -> HTTP field. Fields not in the map pass through with
# their original names.
[endpoint.handler.input_transform]
guest_id = "guestId"
check_in = "checkInDate"
check_out = "checkOutDate"
room_type = "roomType"

# AGTP field -> HTTP field (same direction). The handler inverts
# this map at response time so HTTP keys come back as AGTP keys.
[endpoint.handler.output_transform]
reservation_id = "id"
status = "bookingStatus"

# HTTP status -> AGTP error code. Each target must be declared in
# endpoint.errors above; the registry refuses undeclared mappings
# at startup.
[endpoint.handler.error_map]
"404" = "guest_not_found"
"409" = "room_unavailable"
"422" = "invalid_dates"
```

## What happens at invocation

1. The dispatcher resolves `(BOOK, /room)` against the endpoint registry; the external_service handler runs.
2. The handler validates the request body against `endpoint.input` (already done by the dispatcher) and renames the AGTP fields to their HTTP names per `input_transform`.
3. The handler renders headers — `${VAR}` references in values get substituted against the process environment. (Substitution happens at startup; this step at invocation only assembles the dict.)
4. The handler issues the configured HTTP method against the upstream URL with the renamed body. JSON is the default content type for non-empty bodies.
5. On success (HTTP 2xx), the response body is parsed as JSON and renamed via the inverted `output_transform`. The handler returns `EndpointResponse(body=…, status=upstream_status)`.
6. On HTTP error (4xx / 5xx), the handler consults `error_map` for the upstream status code. Mapped codes ride the structured `EndpointError`. Unmapped 401 / 403 use `upstream_authentication_failed`; everything else uses `upstream_error`.
7. On transport failure (timeout, DNS, connection refusal, TLS handshake failure), the handler returns the matching transport-level error code.

## Error-code contract

Every external_service endpoint should declare the public error codes the handler may produce, plus any codes the operator wants `error_map` entries to surface. The handler-side codes:

| Code | Triggered when |
|---|---|
| `upstream_timeout` | Connection or read timeout (default 30s; configurable via `timeout_seconds`). |
| `upstream_connection_error` | DNS failure, connection refused, TLS handshake failure. |
| `upstream_malformed_response` | Upstream returned a 2xx with a body that isn't valid JSON. |
| `upstream_authentication_failed` | HTTP 401 or 403 from the upstream, when not mapped via `error_map`. |
| `upstream_error` | Any other 4xx / 5xx not matched by `error_map`. |
| (mapped from `error_map`) | The configured AGTP code for the upstream HTTP status. |

The dispatcher refuses handler responses whose `code` isn't in the endpoint's `errors` list. To use any of the codes above, list them in `[endpoint.errors]`.

## Registration-time validation

`resolve_external_service` (in [`server/handler_resolution.py`](../server/handler_resolution.py)) catches misconfigurations at startup. Stable detail tags:

| Detail | Cause |
|---|---|
| `external-service-missing-method` | `[endpoint.handler]` has no `method`. |
| `external-service-bad-method` | `method` isn't a recognized HTTP verb (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`). |
| `external-service-bad-scheme` | The `url` field doesn't begin with `https://`. Plaintext upstream calls are refused at registration. |
| `external-service-bad-timeout` | `timeout_seconds` is missing, zero, or negative. |
| `external-service-error-map-undeclared:<code>` | An `error_map` entry maps an HTTP status to an AGTP code not listed in `[endpoint.errors]`. |

The boot sequence ([`server.main.AgentRegistry.configure_endpoints`](../server/main.py)) catches all `InvalidHandlerError` raises, logs them prominently, and skips the offending endpoint. The rest of the directory continues to load.

## Environment-variable substitution

Header values support shell-style `${VAR}` references. Substitution happens **once at startup** against the process environment so a missing variable can't surface as a per-request mystery 4xx from the upstream. Missing variables are replaced with the empty string AND printed as a warning to stderr at boot:

```
[server] external_service binding 'https://api.staybeta.com/v1/reservations':
  missing environment variables: STAYBETA_API_KEY — corresponding header
  values are empty
```

The default policy is permissive (operators sometimes inject API keys late in the deploy cycle). A future strict-mode flag would refuse registration if any variable is missing.

## Security guidance

- **HTTPS only.** The registry refuses `http://` URLs at registration. Plaintext upstream calls leak request bodies, response bodies, and any tokens authoring `${VAR}` headers.
- **Agent identity is not proxied.** The handler intentionally does NOT include the calling agent's identity in the upstream request. If a future authentication-passthrough mode lands, it'll be opt-in per binding — Phase 4's threat model is "wrap an existing API"; passing the agent through expands the upstream's auth surface and warrants its own design pass.
- **Default 30-second timeout.** Bindings can override per-endpoint via `timeout_seconds`. The minimum sane value depends on upstream behavior; tighter timeouts reduce blast radius from unresponsive upstreams.
- **Headers carry secrets.** Use `${VAR}` substitution for API keys; do not commit real keys to TOML. The boot sequence surfaces missing variables loudly.
- **Idempotency.** AGTP `is_idempotent` declarations advertise the endpoint's contract to agents, but the wrap-and-expose handler doesn't enforce it. If the upstream is non-idempotent, declare `is_idempotent = false` so retrying agents understand the contract.
- **No request signing or mTLS.** Phase 4 doesn't sign upstream requests or do mutual TLS. Operators that need either should run a sidecar / forward proxy in front of the binding.

## Translation helpers

The translation logic lives in three pure functions exported from [`server/handler_resolution.py`](../server/handler_resolution.py):

```python
from server.handler_resolution import (
    translate_input,
    translate_output,
    resolve_headers,
)

# AGTP -> HTTP
http_body = translate_input(agtp_body, input_transform)

# HTTP -> AGTP (output_map is "AGTP -> HTTP" by convention; the
# helper inverts it at runtime)
agtp_body = translate_output(http_body, output_transform)

# Render headers with ${VAR} substitution
headers, missing = resolve_headers({"X-API-Key": "${API_KEY}"})
```

These are unit-tested in isolation in [`tests/test_external_service_handler.py`](../tests/test_external_service_handler.py).

## Manifest exposure

External-service endpoints surface in the manifest's `endpoints` array with `handler_type: "external_service"`. The upstream URL, the input/output maps, the error map, the headers (and their secret values) — none of these ride on the wire. Agents see the binding kind and can reason about expected latency profiles.

## Sample

[`endpoints/fetch_article.toml`](../endpoints/fetch_article.toml) wraps `https://jsonplaceholder.typicode.com/posts/1` as `FETCH /article/{article_id}`. The output_map renames `userId` → `author_id`; the error_map maps HTTP 404 → `article_not_found`. Drop the file in, restart the server, invoke `FETCH /article/1`, see the proxied result.
