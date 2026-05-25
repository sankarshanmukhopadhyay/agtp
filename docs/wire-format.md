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

## Required response headers

| Header | Required | Notes |
|---|---|---|
| `Server-ID` | MUST | Identifies the server that produced the response. Value mirrors the server's configured `server_id` (per `agtp-api §7` manifest discussion). Useful for audit, load-balanced deployments, and verifying which server processed a request. The dispatcher injects this header at response-finalization time so every code path that produces a response carries it. |
| `Response-ID` | MUST | A daemon-synthesized identifier (`resp-<12-hex>`) for this specific response. Distinct from `Request-ID`; multiple responses to the same request (retries, intermediate 202s) MUST each carry a fresh Response-ID. Used by audit chains, replay-detection, and correlation across parallel requests. |

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

## Optional request headers — correlation

### Request-ID

Caller-supplied correlation identifier for this specific request.
The daemon echoes it back as the `Request-ID` response header so
callers can correlate request/response pairs across parallel
operations. When the caller omits it, no echo is emitted; callers
that need correlation MUST send the header.

```
Request-ID: req-2026-05-21-001
```

## Optional request headers — PURCHASE counterparty verification

These two headers ride on **PURCHASE** requests. Per
`draft-hood-agtp-merchant-identity-02 §4`, they let the receiving
merchant verify that the buyer's intent matches the actual
counterparty (substitution-attack defense). The headers are
processed by `mod_merchant`; servers without that module loaded
treat them as advisory only.

### Merchant-ID

The Canonical Agent-ID (64-hex) the buyer believes they are
addressing. The merchant's daemon refuses with **458 Counterparty
Unverified** if the value doesn't equal the receiving agent's
Agent-ID. Protects against DNS poisoning / SEP routing errors that
silently redirect a buyer to a different merchant.

```
Merchant-ID: 0a3f...
```

### Merchant-Manifest-Fingerprint

`sha256` of the merchant's canonical Agent Document JSON at the
time the buyer fetched it. Sent with PURCHASE so the merchant can
verify the manifest hasn't changed between fetch and purchase.
Mismatch returns **458 Counterparty Unverified** with
`error.reason = "merchant-manifest-fingerprint-mismatch"`. Compute
by hashing the same bytes `DESCRIBE` returned, in canonical JSON
form.

```
Merchant-Manifest-Fingerprint: 0933f121379f54a0b90d2971ae1fdbf3b2a9c0512e024e2acd4cfec3ca107db4
```

## Optional response headers

### Attribution-Record

A signed receipt of the response's origin, emitted as JWS Compact
Serialization (RFC 7515 §3.1). Opt-in via
`[audit] attribution_records_enabled = true` in `agtp-server.toml`.
When enabled, every response carries:

```
Attribution-Record: <base64url(header)>.<base64url(payload)>.<base64url(signature)>
```

The header is a JOSE object:

```json
{ "alg": "EdDSA", "typ": "JWT", "kid": "<server signing key id>" }
```

When `[signing].enabled` is true the daemon signs with the loaded
Ed25519 key. Otherwise the daemon emits an unsecured JWS
(`alg: "none"` per RFC 7515 §6) with an empty signature segment so
verifiers see one structural shape regardless of signing state.
Unsecured JWSes MUST be treated as advisory only.

The payload carries the AGTP identifier chain. Empty-valued fields
are omitted so verifiers see only values the daemon actually
observed:

| Field | When present | Source |
|---|---|---|
| `server_id` | always | daemon config |
| `issued_at` | always | UTC ISO 8601 timestamp |
| `status` | always | response status code |
| `agent_id` | when known | request's `Agent-ID` header |
| `owner_id` | when known | agent's registered owner (legal entity) |
| `principal_id` | when known | resolved from the Agent Document |
| `session_id` | when set | request's `Session-ID` header |
| `task_id` | when set | request's `Task-ID` header |
| `request_id` | when set | request's `Request-ID` header |
| `response_id` | always | daemon-synthesized |
| `previous_audit_id` | after agent's first record | per-agent chain head |
| `extra` | when handler supplied | `EndpointResponse.attribution_extra` |

When `attribution_records_enabled = false` (default), both the
`Attribution-Record` and `Audit-ID` headers are absent from
responses.

### Audit-ID

`sha256(Attribution-Record)` hex-encoded. Stamped on every
response that carries an Attribution-Record. The next record from
the same agent references this value as its `previous_audit_id`,
forming a per-agent hash chain.

```
Audit-ID: e42bac416ea7c9249f182a4d93e12fd749bcb0e5d6254b21fc98a898a5f93617
```

Chain heads are persisted by the daemon at
`[audit].chain_head_root` (default `~/.agtp/audit/chain_heads/` on
POSIX, `%APPDATA%\agtp\audit\chain_heads\` on Windows). Operators
running multiple daemons on one host MUST set this explicitly to
prevent chain collisions.

The full JWS for every audit_id is additionally persisted under
`[audit].records_root` (default `~/.agtp/audit/records/`, sharded
by 2-char hex prefix). The Phase-6 **INSPECT** method reads from
this store:

```
AGTP/1.0 INSPECT
Agent-ID: <agent>
Content-Type: application/agtp+json

{"target": "audit", "audit_id": "<64-hex>"}
```

Response body carries `{jws, header, payload, ...}`. A second
shape, `{"target": "chain_head", "agent_id": "<64-hex>"}`, returns
the latest audit_id for the given agent. A third shape,
`{"target": "lifecycle", "agent_id": "<64-hex>"}`, returns the
agent's full identity-lifecycle event stream (ACTIVATE,
DEACTIVATE, REVOKE history) — oldest first, one parsed JWS per
event. All three are read by the chain inspector at
`tools/chain_inspector/` to walk the chain backwards.

## Identity lifecycle methods (Phase 8)

Three embedded verbs transition an agent's lifecycle state:

| Method | Sets `status` to | Notes |
|---|---|---|
| `ACTIVATE` | `active` | Standard "agent ready to serve" state. |
| `DEACTIVATE` | `suspended` | Recoverable inactive state. Re-ACTIVATE later. |
| `REVOKE` | `retired` | Permanent. Agent-ID is never reused per AGTP-LOG §2. |

Each call:

* Updates the AgentDocument's `status` field in memory.
* Appends a signed JWS lifecycle event to
  `[audit].lifecycle_root/{agent_id}.jsonl`.
* Returns 200 with `previous_status`, `status`, `event_type`,
  `audit_id`, and (when provided) `reason`.
* No-ops cleanly: invoking ACTIVATE on an already-active agent
  returns 200 with `noop: true` and emits no event.

Optional body: `{"reason": "..."}` rides into the lifecycle event's
JWS payload `extra.reason` field for audit visibility.

Authorization is open in v1 — the audit trail is the
accountability mechanism. Future revisions can layer cert-based or
Authority-Scope-based gates via `mod_agent_cert`.

### Owner-ID

When the agent's owner (the legal entity that registered the agent)
is known, the daemon stamps it on the response so callers see who is
legally responsible for the agent's behavior without parsing the
body. Sourced from the agent's Agent Document / Agent Genesis;
clients MUST NOT supply this on requests — it is a daemon-stamped
property of the agent's identity.

```
Owner-ID: nomotic.inc
```

## Headers that are NOT part of §10

Pre-§10 drafts mentioned several headers that the §10 model
rejects or moves elsewhere:

| Header | Reason it's not in §10 |
|---|---|
| `AGTP-Version` | The version is in the request/response line. |
| `AGTP-Method` | The method is in the request line. |
| `AGTP-Status` | The status is in the response line. |
| `Principal-ID` | The principal is resolved from the Agent Document; servers look it up by `Agent-ID`. |
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
- **COSE_Sign1 / SCITT envelope** — per-action Attribution-Records
  are emitted as JWS Compact (RFC 7515) on the wire. The lifecycle
  stream (ACTIVATE / DEACTIVATE / REVOKE / REINSTATE / DEPRECATE)
  also supports a SCITT form via `[audit].mode = scitt`: each
  on-disk line becomes `cose:<base64url(COSE_Sign1 bytes)>` per
  RFC 9943, signed by the same daemon Ed25519 key. JWS stays the
  default and the wire form; SCITT mode is for operators who want
  their lifecycle log directly consumable by a SCITT verifier
  without AGTP-specific tooling. Mixed `jws` + `cose:` lines in a
  single file are tolerated across a mode flip — the INSPECT
  reader sniffs each line by prefix.

## Migration from pre-§10

Operators upgrading from pre-§10 servers:

1. **Update header emission**: clients should emit `Agent-ID`
   instead of `Target-Agent`. The server-side back-compat keeps
   old clients working with a deprecation warning per request.
2. **Verify Server-ID and Response-ID in responses**: every response
   now carries `Server-ID` and `Response-ID`. Clients that pin
   headers should accept them.
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
