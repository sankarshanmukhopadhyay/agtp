# AGTP wire format and header model (§10)

This document describes the AGTP wire format and the §10 header
model. The wire format is HTTP-shaped — same line structure, same
header encoding, same Content-Length framing — but the header
vocabulary is AGTP-specific.

## Request line

```
AGTP/1.0 <METHOD> <PATH>\r\n
```

Three space-separated tokens:

- **`AGTP/1.0`** — protocol identifier and version. The version
  rides in the request line; there is no separate `AGTP-Version`
  header. (Pre-§10 drafts mentioned `AGTP-Version` as a header;
  that field is retired.)
- **`<METHOD>`** — the AGTP verb (e.g. `QUERY`, `PROPOSE`,
  `RECONCILE`). The method is in the request line; there is no
  separate `AGTP-Method` header.
- **`<PATH>`** — request path. MUST begin with `/`. See
  [`docs/path-grammar.md`](path-grammar.md) for the path grammar.

## Response line

```
AGTP/1.0 <STATUS> <STATUS-TEXT>\r\n
```

The status code is in the response line; there is no separate
`AGTP-Status` header.

## Required request header

| Header | Required | Notes |
|---|---|---|
| `Agent-ID` | MUST (non-anonymous) | Identifies the invoking agent. Pre-§10 implementations used `Target-Agent`; the §10 server-side parser accepts either with a deprecation warning for the legacy name. |

Server-level operations (target-less `DISCOVER` against the
manifest) MAY omit `Agent-ID` when
`policies.anonymous_discovery = true`; otherwise the dispatcher
refuses with 262 `anonymous-discovery-disabled`.

## Required response header

| Header | Required | Notes |
|---|---|---|
| `Server-ID` | MUST | Identifies the server that produced the response. Value mirrors the server's configured `server_id` (per `agtp-api §7` manifest discussion). Useful for audit, load-balanced deployments, and verifying which server processed a request. The dispatcher injects this header at response-finalization time so every code path that produces a response carries it. |

## Optional request headers

### Authority-Scope

Declared claim of scopes for this specific request. Comma-separated
list:

```
Authority-Scope: bookings:write, ledger:read
```

The dispatcher validates that every claimed scope appears in the
agent's `requires.scopes`. On mismatch the response is 262
`authorization-required` with `error.type = "scope-required"` and
`error.details.code = "scope-claim-invalid"` plus the
`invalid` / `declared` lists. On match the validated list is
surfaced to handlers as `EndpointContext.authority_scope`.

### Session-ID

Opaque session identifier for operational grouping. The server
doesn't interpret the value; it's passed through to handlers as
`EndpointContext.session_id` for handler-level session tracking.

```
Session-ID: sess-d8dc6f0d
```

### Task-ID

Identifies a specific task across multiple requests. Used for
tracing and audit. The server:

- Surfaces it to handlers as `EndpointContext.task_id`.
- **Echoes** it in the response (`Task-ID` on the response) so
  callers can correlate request/response pairs even when
  responses arrive out of order.

```
Task-ID: task-abc-123
```

### Delegation-Chain

Reserved for v01. v00 implementations **MUST** reject requests
carrying this header with 501 Not Implemented:

```json
{
  "error": {
    "code": "delegation-not-supported",
    "message": "the Delegation-Chain header is reserved for future AGTP revisions; this server (v00) does not support delegated authority."
  }
}
```

The header is documented as Future Work in §10 — its eventual
shape (delegation chain format, signature scheme, authority
verification rules) is undefined in v00.

## Optional response header

### Attribution-Record

A signed attestation of the response's origin. Opt-in via
`[audit] attribution_records_enabled = true` in `agtp-server.toml`.
When enabled, every response carries:

```
Attribution-Record: <JSON>
```

The v00 attestation is a JSON-encoded placeholder containing
`server_id`, `issued_at`, `status`, and a `signature` placeholder.
A future revision will replace the payload with a JWS-signed
compact serialization once §5 manifest signing infrastructure
lands.

When `attribution_records_enabled = false` (default), the header
is absent from responses.

## Headers that are NOT part of §10

Pre-§10 drafts mentioned several headers that the §10 model
rejects or moves elsewhere:

| Header | Reason it's not in §10 |
|---|---|
| `AGTP-Version` | The version is in the request/response line. |
| `AGTP-Method` | The method is in the request line. |
| `AGTP-Status` | The status is in the response line. |
| `Principal-ID` | The principal is in the agent document; servers look it up by `Agent-ID`. |
| `Priority`, `TTL`, `Budget-Limit` | Not in scope for v00; reserved for future or implementation-specific use. |
| `AGTP-Zone-ID` | Not in scope for v00. |
| `Content-Schema` | Not in scope for v00. |

Implementations MAY include implementation-specific headers
(`X-*`-prefixed by convention) but they have no protocol meaning.

## Framing

- **Content-Length** is mandatory on every request and response
  carrying a body. Chunked transfer is not part of v00.
- **TLS 1.3 or later** is required on the wire. Plaintext AGTP is
  refused at registration for `external_service` bindings and is
  not part of the protocol contract.
- **TCP/4480** is the default port. Per `agtp §10`, QUIC transport
  on UDP/4480 is reserved for future revisions.
- Servers **MUST NOT** half-close TLS to signal end-of-request.
  Content-Length framing is mandatory.

## v00 → future revisions

§10 reserves the following for future work:

- **Delegation-Chain format** — the header is rejected in v00 but
  the dispatcher path is wired so v01 can implement the chain
  parsing and authority-verification logic without touching the
  request-handling skeleton.
- **QUIC transport** — stream multiplexing, 0-RTT, connection
  migration. Planned for v01.
- **Additional optional headers** if deployment experience
  surfaces real needs (Priority, TTL, Budget-Limit, Zone-ID —
  currently uncertain).
- **JWS signing for Attribution-Record** — the header structure is
  fixed in v00 but the signature payload is a placeholder until
  the §5 manifest-signing infrastructure lands.

## Migration from pre-§10

Operators upgrading from pre-§10 servers:

1. **Update header emission**: clients should emit `Agent-ID`
   instead of `Target-Agent`. The server-side back-compat keeps
   old clients working with a deprecation warning per request.
2. **Verify Server-ID in responses**: every response now carries
   `Server-ID`. Clients that pin headers should accept it.
3. **Reject Delegation-Chain** is a new behavior: clients that
   experimentally sent the header will now receive 501. Strip it
   before sending if you depended on the previous "header silently
   ignored" behavior.
4. **Authority-Scope is new**: clients that don't send it are
   unaffected. Clients that do send it MUST send scopes the agent
   has declared.

The deprecation window for `Target-Agent` is one release cycle.
The fallback will be removed in a future revision; new clients
should emit `Agent-ID` exclusively.
