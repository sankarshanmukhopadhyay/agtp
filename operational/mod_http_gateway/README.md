# mod_http_gateway

REST → AGTP translation sidecar that runs inside the daemon. Loaded
by `agtpd` via `--load-module mod_http_gateway`, it starts a parallel
HTTP listener that accepts ordinary HTTP requests, translates them
into AGTP requests, and serves them through the daemon's regular
dispatch path. The daemon's AGTP wire is unaffected — this is purely
a side-listener that lets REST clients talk to an AGTP server while
the wire transition is in progress.

## When to use this

You have:

- Existing REST clients (browsers, curl scripts, legacy SDKs) that
  can't speak AGTP yet, and
- An AGTP-hosted agent that you want those clients to reach without
  rewriting them.

You don't want to:

- Run RCNS on REST traffic (REST callers cannot trigger negotiations
  — by design, RCNS is AGTP-native).
- Build a full reverse proxy (use NGINX / Envoy for that).

## Quick start

```bash
agtpd \
    --load-module mod_http_gateway \
    --port 4480
```

Defaults:

- HTTP listener binds `127.0.0.1:8080`
- Pinned Agent-ID is empty; REST clients MUST send `X-Agent-Id`
  themselves or get 401

To pin a single Agent-ID for all REST clients (useful for monolithic
deployments where the REST app speaks as one canonical agent):

```bash
AGTP_HTTP_GATEWAY_AGENT_ID=abc123... \
    agtpd --load-module mod_http_gateway
```

## Configuration

All via environment variables.

| Variable | Default | Effect |
|---|---|---|
| `AGTP_HTTP_GATEWAY_ENABLED` | `1` | Set to `0` to load the module without starting the listener |
| `AGTP_HTTP_GATEWAY_HOST` | `127.0.0.1` | Bind interface; **leave on loopback** unless you front the gateway with a reverse proxy that handles TLS |
| `AGTP_HTTP_GATEWAY_PORT` | `8080` | TCP port |
| `AGTP_HTTP_GATEWAY_AGENT_ID` | `""` | Pinned Agent-ID; when set, REST clients without an explicit `X-Agent-Id` header are served as this agent |

The gateway honors the daemon's `[policies.methods.aliases]` table
for verb translation — the same mechanism AGTP-native callers use.
You don't configure the gateway's verb mapping separately.

## Verb translation

Default seed (configurable via `[policies.methods.aliases]` in
`agtp-server.toml`):

| HTTP method | AGTP verb |
|---|---|
| `GET` | `FETCH` |
| `POST` | `CREATE` |
| `PUT` | `REPLACE` |
| `DELETE` | `REMOVE` |
| `PATCH` | `MODIFY` |
| `HEAD` | (forwarded as `HEAD`; daemon returns 459 unless aliased) |

Operators override or extend this by declaring aliases:

```toml
[policies.methods.aliases]
GET     = "DISCOVER"   # treat HTTP GET as DISCOVER instead of FETCH
WHOAMI  = "DESCRIBE"
LIST    = "DISCOVER"
```

An empty `[policies.methods.aliases]` block disables the default
seed — REST clients then get 459 unless the operator declares
explicit mappings.

The Attribution-Record carries the original HTTP verb as
`requested_method` whenever an alias fires, so chain inspectors can
tell that a record came in as `GET` and was served as `FETCH`.

## Header translation

The gateway maps a handful of common HTTP headers to their AGTP
equivalents:

| HTTP header | AGTP header |
|---|---|
| `X-Agent-Id` (or `Agent-ID`) | `Agent-ID` |
| `X-Request-Id` | `Request-ID` |
| `X-Task-Id` | `Task-ID` |
| `X-Session-Id` | `Session-ID` |
| `X-Synthesis-Id` | `Synthesis-Id` |
| `RCNS-Idempotency-Key` | `RCNS-Idempotency-Key` |
| `Content-Type` | `Content-Type` |

`Allow-RCNS` is **dropped** before the request enters dispatch. REST
callers cannot trigger RCNS negotiations — by design. A REST
request to an unregistered `(method, path)` returns 404
`endpoint-not-found`.

Any HTTP header not in the table above is **discarded**. If your
deployment needs additional headers forwarded (custom
authentication tokens, observability IDs), fork this module — the
mapping is intentionally narrow.

## What the gateway does NOT do

- **TLS termination.** Bind to loopback and front it with a real
  reverse proxy (NGINX, Envoy, Caddy) if you need HTTPS.
- **Content negotiation.** Bodies pass through verbatim. Set
  `Content-Type` correctly on the HTTP request and the daemon's
  schema validator handles the rest.
- **Path templating.** REST routes map 1:1 to AGTP paths. An HTTP
  `GET /accounts/123` becomes AGTP `FETCH /accounts/123`; the
  daemon's endpoint registry has to declare that path explicitly
  (or the route won't resolve).
- **Authentication beyond Agent-ID.** mTLS-required deployments
  should refuse REST entirely (don't load the module) or front the
  gateway with a proxy that re-presents the cert chain to AGTP.
- **RCNS.** A REST call to an unknown path returns 404 instead of
  attempting negotiation. AGTP-native callers (using `Allow-RCNS`
  on the AGTP wire) still get RCNS as usual.

## How a request flows

```text
HTTP client                 mod_http_gateway              daemon
-----------                 ----------------              ------
GET /products  --->  read body, translate verb
               --->  GET → FETCH (via aliases)
               --->  build AGTPRequest
               --->  dispatch(req, state, agent_doc) -->
                                                          [Method gate
                                                           Path gate
                                                           Policy gate
                                                           Registry lookup
                                                           ...
                                                           Handler runs]
               <---                                       AGTPResponse
serialize as HTTP
       <--- HTTP/1.1 200 OK
            <body bytes>
```

The "translate verb" step is the only meaningful work outside the
dispatch path. Everything else flows through the daemon's
existing gates — including authority checks, audit recording, and
the synthesis runtime for pre-bound contracts (a REST client
sending `X-Synthesis-Id` still executes against a synthesis it
previously negotiated via the AGTP wire).

## Limitations to be aware of

- **Single-port HTTP only.** No HTTP/2, no Unix sockets, no QUIC.
  Use the AGTP wire for those if you need them.
- **No streaming.** Bodies are read in full before dispatch and
  written in full after. Long-polling and chunked responses work
  for the AGTP wire only.
- **No CORS.** The gateway returns no CORS headers; browser
  callers should be fronted by a proxy that handles preflight.

For richer translation (route-specific verb maps, response
transformation, cross-protocol auth) operators should fork this
module or replace it with a dedicated translation layer.
