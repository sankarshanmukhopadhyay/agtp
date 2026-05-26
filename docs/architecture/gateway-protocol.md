# AGTP Gateway Protocol v1

**Status:** Working draft
**Version:** gateway/1.0
**Audience:** Implementers of `agtpd`, runtime modules (`mod_python`,
`mod_php`, `mod_go`, ...), and operational modules.
**Companion document:** [`server-modules.md`](server-modules.md)

## 1. Scope

This document specifies the wire-level contract between `agtpd` and
any module that plugs into it. Two halves of the same connection
implement this protocol: the **daemon side** runs inside `agtpd`; the
**module side** runs inside a separate handler process.

This is **not** the AGTP protocol. AGTP is the agent-facing wire
format on TCP/4480 (see `draft-hood-independent-agtp-08`). The
gateway protocol is the daemon-to-handler IPC that exists strictly
inside one operator's deployment. It is not exposed to the public
network and it is not a substitute for AGTP.

The gateway protocol is deliberately small. Its job is to hand a
fully-validated request envelope from `agtpd` to the right module
and carry the response back. Anything `agtpd` can do without a
module's help (catalog validation, path grammar, JSON Schema
validation, scope gating, manifest serving) is done **before** the
request crosses the gateway. Modules receive trusted, parsed input.

### Goals

- **Language-neutral.** Implementable in Python, PHP, Go, Node, Rust,
  C with no language-specific assumptions.
- **Singleplex.** One in-flight request per connection at a time.
  Concurrency is achieved by opening multiple connections.
- **Out-of-process.** Module crashes never reach `agtpd`. Daemon
  restarts never corrupt module state.
- **Debuggable.** Frames are human-readable on the wire so operators
  can `socat` the socket during incidents.
- **Forward-compatible.** Capability negotiation at handshake lets v1
  modules continue to work after a v2 daemon adds streaming, signing,
  or outbound proxying.

### Non-goals (v1)

- Streaming responses (deferred; wire protocol is sync today).
- Module-initiated outbound calls (deferred; daemon will pool them).
- Module-to-module communication. Modules talk only to `agtpd`.

(Module-requested signing of agent responses landed as an optional
capability in v1; see §14.1 `sign_request`.)

## 2. Transport

### 2.1 Unix domain socket (primary)

The default transport is a Unix domain socket. The path is set in
`agtp-server.toml`:

```toml
[gateway]
socket = "/var/run/agtpd/gateway.sock"
```

Modules connect with `SOCK_STREAM`. The daemon creates the socket
with permissions `0660` and a configurable group. Modules running
under a different uid join the group rather than escalating
privileges.

### 2.2 TCP loopback (fallback)

For container deployments where `agtpd` and the module run in
sibling containers, TCP loopback is the alternative:

```toml
[gateway]
listen = "127.0.0.1:4485"
```

(Avoid 4482 in examples — that's the chain inspector default.
The registrar runs on HTTPS/443, not AGTP, so it doesn't collide
with the AGTP-port space at all. Operators are free to pick any
free port for the gateway socket; it's the daemon's choice, not
a protocol constant.)

Loopback is bound to `127.0.0.1` only (never a non-loopback
address). Cross-host gateway connections are out of scope; the
gateway carries trusted, post-authentication state and must not
traverse untrusted networks. Operators who need cross-host
deployment terminate AGTP on a remote `agtpd` and run the module
locally to that daemon.

### 2.3 Mutual TLS on the gateway

Not required in v1. The gateway socket lives entirely inside the
operator's trust boundary and is protected by filesystem permissions
(Unix socket) or loopback (TCP). Operators who need mTLS on the
gateway (e.g., cross-namespace Kubernetes deployments) layer it
externally with a sidecar; the gateway protocol itself stays
plaintext.

## 3. Framing

The gateway protocol uses **length-prefixed JSON** for all frames.

```
+----+----+----+----+----+...
| 4-byte big-endian | JSON payload (UTF-8) ...
| unsigned length   |
+----+----+----+----+----+...
```

- The length prefix is the byte length of the JSON payload that
  follows. It does **not** include itself.
- Maximum payload size is 16 MiB (`0x01000000`). Daemons MUST refuse
  larger frames with `frame_too_large` and close the connection.
- Payloads are UTF-8 JSON objects. Top-level value MUST be an object,
  never an array or scalar.
- No multiplexing in v1. The sender writes one frame, then waits for
  the peer's reply, then writes the next. (See §4 for the exact
  sequence per role.)

### 3.1 Why length-prefixed JSON

- **Half-close hazard avoided.** The wire format spec forbids
  TLS-level half-close; length-prefixed framing avoids the
  equivalent at the gateway layer.
- **Debuggable.** Operators can dump a frame with
  `hexdump -C /var/run/agtpd/gateway.sock` and read the JSON.
- **Language-portable.** Every target runtime has a JSON parser. No
  protobuf compiler, no schema versioning at the encoding layer.
- **Replaceable.** CBOR (RFC 8949) is the candidate for v2 once the
  protocol stabilizes and per-request encoding overhead matters.

### 3.2 Frame types

Every frame's JSON payload carries a `type` field:

| `type`           | Direction       | Purpose |
|------------------|-----------------|---------|
| `hello`          | module → daemon | Connection handshake |
| `welcome`        | daemon → module | Handshake response |
| `register`       | daemon → module | Push the endpoint set (full, with schemas) |
| `register_resume`| daemon → module | Resume registration on cached manifest hash |
| `register_ack`   | module → daemon | Module accepts the set |
| `request`        | daemon → module | One AGTP request |
| `response`       | module → daemon | The handler's reply |
| `error`          | either          | Protocol or handler error |
| `ping` / `pong`  | either          | Keepalive |
| `goodbye`        | either          | Orderly close |

## 4. Connection lifecycle

```
  Module                                          agtpd
    |                                               |
    | --- connect() ----------------------------->  |
    |                                               |
    | --- hello {gateway_versions, module_id} --->  |
    |                                               |
    | <-- welcome {gateway_version, capabilities}-- |
    |                                               |
    | <-- register {endpoints, manifest_hash} ----  |
    |                                               |
    | --- register_ack {ok: true} -------------->   |
    |                                               |
    |       ── connection is now READY ──           |
    |                                               |
    | <-- request {request_id, envelope} ---------- |
    | --- response {request_id, envelope} ----->    |
    |                                               |
    | <-- request {request_id, envelope} ---------- |
    | --- response {request_id, envelope} ----->    |
    |                ... etc ...                    |
    |                                               |
    | <-- goodbye OR --- goodbye --->               |
    |                                               |
    | --- close() ---                               |
```

A connection has four phases: **handshake**, **registration**,
**ready**, **closing**. Frames out of order for the current phase
are protocol errors and result in connection close.

### 4.1 Phase: handshake

Module sends `hello`. Daemon replies `welcome` or closes the
connection with an `error` frame.

### 4.2 Phase: registration

Daemon sends `register` carrying the endpoint set the module owns
for this connection. Module replies `register_ack` (or `error` if
it cannot honor the set — e.g., a handler reference doesn't resolve).

### 4.3 Phase: ready

Daemon sends `request` frames. Module replies one `response` per
`request`, matched by `request_id`. The next `request` is not sent
until the prior `response` is received. Either side MAY send
`ping`; the peer MUST reply with `pong` within the keepalive timeout
or the connection is considered dead.

### 4.4 Phase: closing

Either side MAY send `goodbye` to start an orderly close. The
sender stops issuing new frames. Any in-flight request must
complete or the daemon will re-dispatch it to another worker.

### 4.5 Connection death

Sudden close (TCP RST, EPIPE, process exit) is treated as
"handler unavailable." The daemon retries the in-flight request on
another module connection. If no other connection is available, the
inbound AGTP request returns 503 Unavailable to the agent.

## 5. Handshake

### 5.1 `hello` (module → daemon)

```json
{
  "type": "hello",
  "gateway_versions": ["1.0"],
  "module": {
    "id": "mod_python",
    "version": "0.1.0",
    "runtime": "CPython 3.13",
    "pid": 42891
  },
  "capabilities": ["registered_function"],
  "cached_manifest_hash": "sha256:b8e2..."
}
```

- `gateway_versions` — array of supported gateway protocol versions,
  in preference order. v1 modules send `["1.0"]`.
- `module.id` — short identifier (`mod_python`, `mod_php`, ...). Used
  in daemon logs.
- `module.version` — semver of the module implementation. Logged.
- `module.runtime` — free-form runtime description. Logged.
- `module.pid` — module's process id. Logged.
- `capabilities` — optional capability tags the module claims.
  v1 recognizes `registered_function`. Future values:
  `composition`, `external_service`, `streaming`, `outbound_call`,
  `signing_request`.
- `cached_manifest_hash` — optional. The `manifest_hash` from the
  module's most recent successful registration, persisted across
  worker restarts. Lets the daemon skip schema retransmission when
  the manifest hasn't changed. Absent on a module's first ever
  connection. See §6.4.

### 5.2 `welcome` (daemon → module)

```json
{
  "type": "welcome",
  "gateway_version": "1.0",
  "daemon": {
    "version": "agtpd 0.7.0",
    "server_id": "agents.agtp.io",
    "catalog_version": "1.0.0"
  },
  "capabilities": ["registered_function"]
}
```

- `gateway_version` — the version the daemon chose from the module's
  `gateway_versions` list. Both sides use this version for the
  remainder of the connection.
- `daemon.catalog_version` — the AGTP method catalog version the
  daemon is using. Modules MAY refuse to operate against a catalog
  version they were not built for; closing with `goodbye` is the
  correct response.
- `capabilities` — intersection of module-claimed and
  daemon-supported capabilities. Subsequent registration is bounded
  by this set.

### 5.3 Version mismatch

If no version in `gateway_versions` is supported by the daemon, the
daemon replies with `error` (code `gateway_version_unsupported`) and
closes. The module logs the failure and exits with non-zero status
so a process supervisor surfaces the misconfiguration.

## 6. Registration

The operator's manifest is the source of truth for what endpoints
this deployment serves. `agtpd` loads it at startup, then declares
the relevant subset to each module on connect.

### 6.1 `register` (daemon → module)

```json
{
  "type": "register",
  "manifest_hash": "sha256:b8e2...",
  "endpoints": [
    {
      "method": "BOOK",
      "path": "/room",
      "handler_reference": "drupal_agtp.handlers.book_room",
      "input_schema_ref": "#/schemas/book_room.input",
      "output_schema_ref": "#/schemas/book_room.output",
      "errors": ["room_unavailable", "invalid_dates"],
      "required_scopes": ["booking:write"]
    }
  ],
  "schemas": {
    "book_room.input": { "$schema": "...", "type": "object", ... },
    "book_room.output": { "$schema": "...", "type": "object", ... }
  }
}
```

- `manifest_hash` — SHA-256 of the canonically-serialized operator
  manifest, expressed as `"sha256:<hex>"`. The canonical form is
  RFC 8785 (JSON Canonicalization Scheme): keys sorted lexically,
  no insignificant whitespace, numbers in their shortest exact
  IEEE 754 form, strings UTF-8. Both sides MUST compute the hash
  the same way; otherwise resume (§6.4) silently misses. The hash
  covers the entire serialized manifest including schemas — any
  change to a schema produces a new hash.
- `endpoints` — the `(method, path)` set this module owns. Each
  entry carries everything the module needs to dispatch the request
  to user code. The `handler_reference` is interpreted by the
  module (`mod_python` treats it as a dotted import path; `mod_php`
  treats it as a class/method reference; etc.) — `agtpd` is opaque
  to its shape.
- `input_schema_ref` / `output_schema_ref` — JSON Pointer references
  into the `schemas` block. Modules MAY validate inputs again as
  defense-in-depth but are not required to (the daemon already did).
- `errors` — declared error codes for the endpoint. The module's
  response MUST use one of these codes when returning an
  `endpoint_error`.

### 6.2 `register_ack` (module → daemon)

```json
{
  "type": "register_ack",
  "ok": true,
  "resolved": ["BOOK /room", "CHECK_IN /room", "..."]
}
```

Or on partial failure:

```json
{
  "type": "register_ack",
  "ok": false,
  "errors": [
    {
      "endpoint": "BOOK /room",
      "reason": "handler_not_found",
      "detail": "module has no resolution for drupal_agtp.handlers.book_room"
    }
  ]
}
```

If `ok` is `false`, the daemon logs the failure and closes the
connection. The module exits non-zero. Operator intervention is
expected — `agtpd` will not silently route around an endpoint the
module promised to serve.

### 6.3 Re-registration

The operator may change the manifest at runtime (config reload,
deployment). `agtpd` sends a fresh `register` frame to each module.
Modules apply the new set atomically: requests already in flight
finish under the prior binding; new requests use the new binding.
The `manifest_hash` lets modules detect no-op reloads.

### 6.4 Fast resume (`register_resume`)

When a module reconnects (PHP-FPM worker recycle, deploy rollback,
crash recovery), retransmitting the schema block is wasteful — the
schemas didn't change. The module declares its last-known manifest
hash in `hello.cached_manifest_hash`; if the daemon's current hash
matches, the daemon sends `register_resume` in place of `register`:

```json
{
  "type": "register_resume",
  "manifest_hash": "sha256:b8e2..."
}
```

The module replies with the same `register_ack` shape (§6.2). On
hash match, no schemas cross the wire — the module reuses its cached
bindings. The daemon MUST send `register_resume` if and only if the
hashes match exactly; on any mismatch the daemon sends the full
`register` frame and the module discards its cache.

This is a pure optimization for the steady-state restart case. A
module that has never connected before, or that has lost its cache,
sends `hello` without `cached_manifest_hash` and gets the full
`register` frame.

Modules SHOULD persist `manifest_hash` and the resolved binding
table to local disk so the cache survives worker recycles. The
storage format is module-internal; the protocol does not specify it.

## 7. Request and response frames

### 7.1 `request` (daemon → module)

```json
{
  "type": "request",
  "request_id": "req-7f3a91b2",
  "envelope": {
    "method": "BOOK",
    "path": "/room",
    "agent_id": "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230",
    "principal_id": "chris@nomotic.ai",
    "authority_scope": ["booking:write"],
    "session_id": "sess-abc123",
    "task_id": "task-xyz789",
    "request_id": "req-7f3a91b2",
    "headers": {
      "agent-id": "d8dc6f0d...",
      "task-id": "task-xyz789"
    },
    "input": {
      "guest_name": "Chris Hood",
      "check_in": "2026-06-01",
      "check_out": "2026-06-03",
      "room_type": "double"
    }
  },
  "trust": {
    "verified": true,
    "agent_id": "d8dc6f0d...",
    "agent_cert_fingerprint": null,
    "method": "agent_id_header"
  }
}
```

- `request_id` — daemon-allocated, opaque to the module, carried back
  in the response. Used for log correlation and to match `response`
  frames to `request` frames.
- `envelope` — the parsed request, mirroring `EndpointContext` in
  the existing handler API. All fields are pre-validated by the
  daemon. Modules MAY treat them as trusted input.
- `trust` — agent-identity trust state. `verified=true` means the
  daemon authenticated the calling agent. In v1 the only `method`
  is `agent_id_header` (the `Agent-ID` header was present and the
  agent is known). When mTLS lands, `agent_cert_fingerprint` carries
  the verified certificate fingerprint and `method` becomes
  `agent_cert_mtls`.

### 7.2 `response` (module → daemon)

```json
{
  "type": "response",
  "request_id": "req-7f3a91b2",
  "envelope": {
    "status": 200,
    "headers": {
      "Idempotency-Key": "ik-7d3a"
    },
    "body": {
      "reservation_id": "res-2026-06-01-7d3a"
    }
  }
}
```

- `request_id` — MUST match the `request_id` of the request frame.
  Mismatched ids are protocol errors.
- `envelope.status` — defaults to `200`. `201`, `202`, `204`, and any
  status the endpoint contract documents are valid.
- `envelope.headers` — added to the AGTP wire response alongside
  daemon-injected headers (`Server-ID`, `Task-ID` echo,
  `Attribution-Record`).
- `envelope.body` — the response body. The daemon validates it
  against the endpoint's output schema before serializing onto the
  AGTP wire. Schema failures become 500 errors logged against the
  module.

### 7.3 Declared errors (module → daemon)

Errors the endpoint contract describes use the `endpoint_error`
shape:

```json
{
  "type": "response",
  "request_id": "req-7f3a91b2",
  "envelope": {
    "endpoint_error": {
      "code": "room_unavailable",
      "message": "The requested room type is not available.",
      "details": { "room_type": "suite" }
    }
  }
}
```

The daemon translates `endpoint_error` into a 422 wire response with
the structured body shape documented in `agtp/handlers.py`. The
`code` MUST be in the endpoint's `errors` list (declared during
registration); undeclared codes are a protocol violation and become
500 errors logged against the module.

### 7.4 Unexpected handler failures

If user code raises (not returns) — exception, panic, fatal error —
the module sends:

```json
{
  "type": "error",
  "request_id": "req-7f3a91b2",
  "code": "handler_exception",
  "message": "uncaught TypeError: cannot read property 'foo' of null",
  "details": { "exception_type": "TypeError" }
}
```

The daemon returns 500 Server Error to the agent and logs the
module-side detail. The connection stays open; the next request is
served normally.

## 8. Errors and reconnection

### 8.1 `error` frame (either direction)

```json
{
  "type": "error",
  "request_id": "req-7f3a91b2",
  "code": "frame_too_large",
  "message": "payload exceeded 16 MiB limit",
  "details": { "size": 18874368 }
}
```

`request_id` is omitted for protocol-level errors not tied to a
request (`gateway_version_unsupported`, `malformed_frame`).

### 8.2 Protocol error codes (closes connection)

| Code                          | Meaning |
|-------------------------------|---------|
| `gateway_version_unsupported` | No common version between hello / welcome |
| `malformed_frame`             | Frame is not valid JSON or wrong shape |
| `frame_too_large`             | Payload exceeded 16 MiB |
| `phase_violation`             | Frame is wrong for the current phase |
| `request_id_mismatch`         | `response.request_id` did not match any pending `request` |
| `registration_failed`         | Module rejected the endpoint set |

### 8.3 Per-request error codes (connection stays open)

| Code                  | Meaning |
|-----------------------|---------|
| `handler_exception`   | User code raised (not returned an error) |
| `handler_timeout`     | Module did not respond within configured limit |
| `output_schema_failure` | Module returned a body that fails the output schema |
| `undeclared_error_code` | `endpoint_error.code` not in `errors` list |

### 8.4 Reconnection

Modules SHOULD reconnect after unexpected close with exponential
backoff (1s, 2s, 4s, 8s, 16s, cap 30s). The daemon SHOULD accept
reconnections without operator action, restoring registration on
the new connection.

If the daemon restarts, modules see EOF and reconnect. The
registration frame is sent fresh on the new connection — there is
no resumption protocol in v1.

## 9. Trusted headers and mTLS forward-compatibility

In v1, agent identity rides as the `Agent-ID` request header (per
`draft-hood-independent-agtp-08` §10). The daemon resolves the
agent against its registry, populates `envelope.agent_id`, and sets
`trust.method = "agent_id_header"`, `trust.verified = true`.

When mTLS / Agent-Cert lands (`draft-hood-agtp-agent-cert-01`):

- The daemon verifies the client certificate during the AGTP-level
  TLS handshake.
- The verified Agent-ID and certificate fingerprint go into `trust`.
- `trust.method` becomes `agent_cert_mtls`.
- Modules MUST treat the fields in `trust` as authoritative. They
  MUST NOT re-verify the certificate.

The `trust` object is forward-compatible: v1 modules ignore the
`agent_cert_fingerprint` field; v2 modules consume it when present.

The full certificate chain is **not** sent on every request. A
module that needs it (audit modules, advanced policy modules) sends
a `get_certificate_chain` request — a v2 capability, deferred.

## 10. Keepalive

Either side MAY send `ping` during the ready phase:

```json
{ "type": "ping", "nonce": "p-1234" }
```

The peer MUST reply with `pong` within 30 seconds:

```json
{ "type": "pong", "nonce": "p-1234" }
```

Missing `pong` within the timeout is treated as connection death
(§4.5). The default keepalive interval is 60 seconds idle; operators
can tune via `agtp-server.toml`.

## 11. Goodbye

Orderly close:

```json
{ "type": "goodbye", "reason": "manifest_reload" | "shutdown" | "drain" }
```

After sending `goodbye`, the sender stops issuing new frames. The
peer drains any in-flight work, sends a matching `goodbye`, and
closes the underlying socket.

## 12. Versioning

The gateway protocol versions independently of the AGTP wire
format and the method catalog. Three axes:

| Axis | Today | Where it lives |
|------|-------|----------------|
| AGTP wire | `AGTP/1.0` | `core/wire.py` |
| Method catalog | `1.0.0` | `core/methods.json` |
| Gateway protocol | `1.0` | this document |

A breaking change to any axis is a major version bump on that axis
only. A daemon may speak AGTP/1.0 + gateway/1.1 + catalog/1.2.0
simultaneously; modules built against gateway/1.0 keep working.

Capability flags inside the handshake (`capabilities` in `hello` and
`welcome`) handle non-breaking additions without a version bump:
streaming, outbound calls, signing requests all roll in this way.

### 12.1 When v2 cuts

The gateway protocol stays at **`1.0` and iterates in place** for
the foreseeable future. AGTP is pre-stable, no external
implementations exist outside this monorepo, and capability
negotiation in the handshake already handles graceful additions.
"Bumping to v2" buys nothing when we control every implementation.

A real v2 cut waits for the day there's an **external implementation
that a frame-shape change would break**. At that point we'll have a
specific compatibility surface to design against and the version
bump means something. Until then, additions follow this rule:

- **Backward-compatible** changes (new optional fields on existing
  frames, new frame types tied to capability flags modules can
  decline) land in v1.0 directly. The spec gets a `### N.N`
  subsection, the CHANGELOG records what arrived, and modules that
  don't claim the capability continue to work unchanged.

- **Backward-incompatible** changes (wire format break, mandatory
  new fields, removal of frame types) wait for an actual v2 cut
  triggered by an external user we'd otherwise break.

This document is the working spec. Look at the capability-flag table
in §5.2 (`welcome.capabilities`) to see what the current daemon
advertises; the canonical list grows as new capabilities ship.

## 13. Conformance vectors

A v1 conformance harness lives in `tests/gateway/`. Modules pass
conformance by:

1. Connecting and completing the handshake against the test daemon.
2. Accepting a registration of three endpoints (one
   `registered_function`, two more with declared errors).
3. Serving one normal request, returning the expected body.
4. Serving one request whose handler returns `endpoint_error`.
5. Serving one request whose handler raises an unexpected exception.
6. Responding to a `ping` within 30 seconds.
7. Reconnecting after the test daemon closes the connection.
8. Refusing a `register` frame whose handler reference does not
   resolve, with a structured `register_ack` failure.

Reference frame samples for each step are in
[`tests/gateway/vectors/`](../../tests/gateway/vectors/).

## 14. Optional capabilities

Beyond the baseline `registered_function` capability that every
runtime module claims, the protocol defines optional capabilities
modules can opt into via `hello.capabilities`. A module MUST NOT
exercise a capability the daemon did not echo back in
`welcome.capabilities`.

The introduction of a capability is always additive: existing
modules that don't claim it continue to work unchanged.

### 14.1 `sign_request` (module → daemon → module)

When the module claims `sign_request` AND the daemon advertises it,
the module may ask the daemon to sign opaque bytes with the
daemon's Ed25519 signing service. Private keys never leave the
daemon. The flow:

```
  Module                                          Daemon
    |                                               |
    | --- request {request_id, envelope} ---------> |    (existing)
    |   <-- daemon expects the response back --     |
    |                                               |
    | --- sign_request {operation_id, data_b64} --> |    (NEW)
    |   <-- daemon's read-loop services this --     |
    | <-- sign_response {operation_id, ...} ------- |
    |                                               |
    | --- response {request_id, envelope} --------> |    (existing)
    |                                               |
```

Frames:

```json
{
  "type": "sign_request",
  "operation_id": "op-abc123",
  "data_b64": "<base64url of bytes to sign>"
}
```

```json
{
  "type": "sign_response",
  "operation_id": "op-abc123",
  "kid": "ed25519-...",
  "alg": "Ed25519",
  "signature_b64": "<base64url of 64-byte signature>"
}
```

```json
{
  "type": "sign_error",
  "operation_id": "op-abc123",
  "code": "signing_unavailable" | "sign_failure",
  "message": "..."
}
```

The daemon's signing key is the one loaded via `[signing]` in
`agtp-server.toml`. When `[signing].enabled = false`, the daemon
either omits `sign_request` from `welcome.capabilities` (preferred)
or replies with `sign_error` code `signing_unavailable` to any
incoming `sign_request`.

### 14.2 `outbound_call` (module → daemon → upstream → module)

When the module claims `outbound_call` AND the daemon advertises
it, the module may ask the daemon to make an outbound AGTP request
on its behalf. The daemon uses its own client primitives (connection
pool, TLS context, Agent-Cert presentation) to reach the upstream.

```json
{
  "type": "outbound_request",
  "operation_id": "op-def456",
  "uri": "agtp://reservations.partner.com",
  "method": "QUERY",
  "path": "/availability",
  "headers": {"Agent-ID": "...", "Authority-Scope": "..."},
  "body": {"date": "2026-06-01"}
}
```

```json
{
  "type": "outbound_response",
  "operation_id": "op-def456",
  "status": 200,
  "headers": {"Content-Type": "application/json"},
  "body": {"available": true}
}
```

```json
{
  "type": "outbound_error",
  "operation_id": "op-def456",
  "code": "upstream_unreachable" | "upstream_malformed" | "outbound_failure",
  "message": "..."
}
```

Identity propagation is the operator's choice: the module supplies
the headers it wants forwarded. The daemon does not inject the
calling agent's identity automatically — handlers that want to
forward the original agent must explicitly pass the relevant
headers.

### 14.3 Bidirectional read-loop during dispatch

Both `sign_request` and `outbound_call` invert the per-request
flow: the **module** initiates, the **daemon** services, before the
module sends its final `response`. The daemon's dispatch loop, after
writing the inbound `request` frame, reads frames in a loop until
the matching `response` arrives:

```
loop:
    next = read_frame()
    if next.type == "response" and next.request_id == this_request:
        break
    elif next.type == "sign_request":
        handle_sign(next)
    elif next.type == "outbound_request":
        handle_outbound(next)
    elif next.type == "error":
        bail out
    else:
        protocol violation
```

Singleplex still applies — the module issues one sub-request at a
time and waits for its reply before continuing or sending the final
`response`. Operations are matched by module-generated
`operation_id` (analogous to but distinct from `request_id`, which
belongs to the inbound request).

### 14.4 `streaming` — deferred

The wire format is strictly synchronous today (`Content-Length`
framing, no half-close). Streaming responses at the gateway layer
are spec'd here only as a placeholder so future implementers know
where they'll attach: `response_chunk` frames followed by
`response_end`, tied to a `streaming` capability flag, lighting up
when AGTP itself gains streaming at the wire layer. Modules MUST
NOT claim `streaming` today; daemons MUST NOT advertise it.

### 14.5 Other anticipated capabilities

These are sketched but not yet specified in detail. Each will get
its own subsection above when there's a real use case driving the
shape:

- **Certificate chain fetch.** Module asks the daemon for the full
  X.509 chain of the current request's agent. Useful for audit and
  policy modules; everyday handlers don't need it.
- **Multiplex.** Multiple in-flight `request_id`s on one connection.
  Reduces socket count for high-concurrency modules. Trade-off:
  more complex framing on both sides. Waits for a real concurrency
  case that singleplex-with-multiple-connections doesn't already
  serve.

## 15. What this document does not cover

- The AGTP wire format (`draft-hood-independent-agtp-08`).
- The method catalog and path grammar (`AGTP-API` draft and
  `core/methods.json`).
- The operator manifest format itself — the manifest is `agtpd`'s
  config concern, defined in
  [`docs/endpoint-toml.md`](../endpoint-toml.md). The gateway
  protocol carries a subset of the manifest into each module; it
  does not define what the manifest is.
- TLS/mTLS on the AGTP wire. The gateway carries trust state
  established there but does not implement it.
