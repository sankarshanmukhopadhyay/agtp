# PROPOSE / Synthesis (§7)

PROPOSE is AGTP's runtime contract-negotiation mechanism. An agent
submits a proposed endpoint contract; the server either accepts it
(producing a `synthesis_id` the agent uses to invoke the composed
endpoint), rejects it with a structured reason, queues it for async
evaluation, or refuses on policy / auth grounds.

This document covers the wire-level surface. The synthesis runtime
internals (composition policies, plan execution) live in
[`server/synthesis/README.md`](../server/synthesis/README.md).

## Status code surface

PROPOSE has its own status-code family distinct from the rest of the
AGTP method registry:

| Code | Name | Body shape | When |
|---|---|---|---|
| **263** | Proposal Approved | `synthesis_id` / `endpoint` / `persistent` / `expires_at` / `granted_duration` | Synthesis instantiated; the `synthesis_id` is immediately invokable via the `Synthesis-Id` header. |
| **261** | Negotiation In Progress | `proposal_id` / `polling_path` / `evaluation_started_at` / `max_evaluation_duration` | Server has queued the proposal for asynchronous evaluation; the agent polls `QUERY /proposals` for the resolution. |
| **463** | Proposal Rejected | `error.code = "proposal-rejected"` / `error.reason` / `error.explanation` / optional `error.counter_proposal` | Server refuses; `reason` is one of `out-of-scope`, `policy-refused`, `composition-impossible`, `ambiguous`. |
| **400** | Bad Request | `error.code = "bad-request"` / `error.issue` | Body well-formedness failed (invalid JSON, missing required field, malformed semantic block, malformed schema). |
| **262** | Authorization Required | `error.code = "authorization-required"` / `error.type` | Agent's authority insufficient. |

The pre-§7 surface (200 OK on accept, 422 Unprocessable on
refuse with `error.code = "negotiation-refused"`) has been retired.
The legacy helpers stay in `core/status.py` for transitional
external callers, but the in-tree dispatcher uses the new surface.

## 263 Proposal Approved

Body shape:

```json
{
  "synthesis_id": "syn-AbCdEfGhIjKlMn",
  "endpoint": { "method": "RECONCILE", "input": {...}, "semantic": {...}, ... },
  "persistent": false,
  "expires_at": "2026-05-11T05:00:00Z",
  "granted_duration": "24h",
  "synthesis": {
    "target_method": "RECONCILE",
    "parameter_mapping": {"account_id": "account_id"},
    "description": "...",
    "proposal_name": "RECONCILE",
    "plan": { "steps": [...], "output_aggregation": "..." }
  },
  "agent_id": "abc123..."
}
```

Notable fields:

- `synthesis_id` — opaque identifier the agent passes back via the
  `Synthesis-Id` header to invoke the composed endpoint.
- `endpoint` — the instantiated endpoint contract (same shape as a
  manifest `endpoints[]` entry).
- `persistent` / `expires_at` / `granted_duration` — see
  [Persistent synthesis](#persistent-synthesis) below.
- `synthesis` — multi-step plan detail for compositions that aren't
  a passthrough.

## 463 Proposal Rejected

Body shape:

```json
{
  "error": {
    "code": "proposal-rejected",
    "reason": "out-of-scope",
    "explanation": "Server cannot compose this endpoint from existing primitives.",
    "counter_proposal": {
      "name": "QUERY",
      "input": {...},
      ...
    }
  }
}
```

The `reason` vocabulary:

| Reason | Meaning |
|---|---|
| `out-of-scope` | Server has no near-match and no synthesizable composition. Often accompanied by `counter_proposal` for typo-shaped names. |
| `policy-refused` | Server policy explicitly refuses this kind of proposal (e.g., `policies.synthesis_enabled = false`). |
| `composition-impossible` | Synthesis runtime declined; the available primitives can't fulfill the request. |
| `ambiguous` | Multiple candidate compositions; server can't decide. (Reserved; not produced by the v00 runtime.) |

`counter_proposal` is optional. When present it carries the server's
suggested alternative — usually a near-name match the server is
willing to accept. Clients can re-issue the proposal with the
suggested `name`.

## 261 Negotiation In Progress

When `[policies.synthesis] async_evaluation_enabled = true`, the
server queues every PROPOSE for asynchronous evaluation and returns:

```json
{
  "proposal_id": "prop-XyZ123",
  "polling_path": "/proposals",
  "evaluation_started_at": "2026-05-10T14:32:18Z",
  "max_evaluation_duration": "10m",
  "explanation": "proposal prop-XyZ123 is under evaluation; poll QUERY /proposals for status."
}
```

The agent polls `QUERY /proposals` with `{"proposal_id": "prop-XyZ123"}`
in the body. The response carries the same body the synchronous
PROPOSE would have returned — `state: "pending"` plus the 261 fields
while in progress, then the full 263 / 463 body once resolved.

```bash
# Agent submits PROPOSE
$ agtp agtp://server PROPOSE -d '{"name": "EVALUATE", ...}'
# → 261 Negotiation In Progress, body carries proposal_id

# Agent polls
$ agtp agtp://server QUERY /proposals -d '{"proposal_id": "prop-XyZ123"}'
# → 261 while pending; 263 / 463 once resolved
```

Servers that don't enable async evaluation (the default) skip this
path entirely; every PROPOSE returns 263 or 463 synchronously.

> **v00 routing note:** the agtp-api spec calls this surface
> `QUERY /proposals/{proposal_id}` with the id as a path
> parameter. The endpoint registry's lookup is exact-match in
> v00, so the implementation accepts `proposal_id` as a body
> parameter on the literal-path endpoint `QUERY /proposals`.
> Path-template routing is a follow-up.

## 400 Bad Request

When the PROPOSE body fails well-formedness validation:

```json
{
  "error": {
    "code": "bad-request",
    "issue": "missing-required-field",
    "explanation": "PROPOSE body missing required field 'name'",
    "details": {"missing": ["name"]}
  }
}
```

`issue` vocabulary:

| Issue | Meaning |
|---|---|
| `invalid-json` | Body could not be parsed as JSON. |
| `missing-required-field` | A required field (`name`, etc.) is absent or empty. |
| `malformed-semantic-block` | The `semantic` field is structurally invalid. |
| `malformed-schema` | An embedded `input_schema` / `output_schema` is not a valid JSON Schema object. |

400 is reserved for body-shape problems. Authority refusals use 262;
composition refusals use 463.

## 262 Authorization Required

Consolidates all authority / credential refusals under one status
code. The body's `error.type` carries the specific reason:

```json
{
  "error": {
    "code": "authorization-required",
    "type": "scope-required",
    "explanation": "BOOK /room requires scope(s) the agent has not declared: bookings:write",
    "details": {
      "method": "BOOK",
      "path": "/room",
      "missing_scopes": ["bookings:write"]
    }
  }
}
```

`type` vocabulary:

| Type | Meaning | Replaces (pre-§7) |
|---|---|---|
| `scope-required` | Endpoint declares `required_scopes` the agent has not declared. | 403 `insufficient_scope` |
| `wildcards-required` | Agent declares `wildcards: true` and server refuses wildcards. | 403 `wildcards-refused` (and earlier 462) |
| `credentials-missing` | Request lacks credentials the server requires. | n/a (new) |
| `anonymous-discovery-disabled` | Server refuses target-less DISCOVER without credentials. | n/a (new) |

The pre-§7 helpers `wildcards_refused` and `insufficient_scope` now
forward to `authorization_required` — call sites don't churn, the
wire status migrates.

## Persistent synthesis

The PROPOSE body may include:

```json
{
  "name": "EVALUATE",
  "...": "...",
  "persistent": true,
  "requested_duration": "7d"
}
```

The server resolves the granted duration:

| Agent's request | Server's response |
|---|---|
| `persistent: false` (default) | granted = `[policies.synthesis] session_duration` |
| `persistent: true`, no `requested_duration` | granted = `persistent_default_duration` |
| `persistent: true`, `requested_duration ≤ persistent_max_duration` | granted = requested |
| `persistent: true`, `requested_duration > persistent_max_duration` | granted = `persistent_max_duration` (capped) |

The 263 response's `granted_duration` field carries the actual value
the server granted. Compare to the request to detect capping.

Duration notation is `<int><unit>` with units `s` / `m` / `h` / `d`.
Compound durations (`1d12h`) are out of scope for v00.

After `expires_at` passes, the runtime evicts the synthesis on next
lookup. Subsequent invocations of the expired `synthesis_id` return
the standard `synthesis-not-found` response — the agent re-PROPOSEs
to instantiate a fresh synthesis.

### v00 storage model

Persistent syntheses live in the server's in-process memory and do
not survive a restart. Durable storage is future work — the v00
contract for persistent simply means "the synthesis is offered with
a longer expiration than `session_duration` and is not bound to the
agent's session."

## Audit log

Every PROPOSE outcome produces a single structured JSON line:

```json
{"timestamp": "2026-05-10T14:32:18Z", "agent_id": "abc123...", "proposal_hash": "1548d443ae4ad8b7", "decision": "accepted", "synthesis_id": "syn-AbCdEfGhIjKlMn", "granted_duration": "24h"}
```

Configured via `[audit] path` in `agtp-server.toml`:

- `"stderr"` (default) — append to `sys.stderr`.
- `"none"` / `""` — disable logging entirely.
- Any other string — filesystem path; entries append as JSONL.

The audit log is intended for operator visibility (auditing unusual
proposal patterns, building a future archive of approved proposals,
debugging negotiation failures). Format is fixed JSONL so log
aggregators can index without parser-specific configuration.

Audit-log failures (IO errors, bad path) are swallowed silently —
audit issues must never affect the request path.

## Config reference

```toml
[policies]
synthesis_enabled         = true   # false → every PROPOSE returns 463 policy-refused
max_synthesis_depth       = 10     # plans with more steps are refused

[synthesis]
policies                     = ["recipes"]
recipes_file                 = "agtp-recipes.toml"
session_duration             = "24h"   # non-persistent TTL
persistent_default_duration  = "7d"    # persistent without requested_duration
persistent_max_duration      = "30d"   # cap for persistent requests
async_evaluation_enabled     = false   # true → PROPOSE returns 261 + polling
max_evaluation_duration      = "10m"   # async deadline before auto-reject

[audit]
path = "stderr"
```

## Migration notes

For clients written against the pre-§7 PROPOSE surface:

- Replace `if resp.status_code == 200:` with `if resp.status_code == 263:`.
- Replace `if resp.status_code == 422 and body["error"]["code"] == "negotiation-refused":` with `if resp.status_code == 463:`.
- The body shape moved `body["synthesis"]["synthesis_id"]` to
  `body["synthesis_id"]` (top level). The nested `synthesis` object
  keeps multi-step plan detail (`target_method`,
  `parameter_mapping`, `plan`).
- 462 Wildcards Refused is retired entirely. Wildcards refusals now
  return 262 with `error.type = "wildcards-required"`.
- 455 Scope Violation stays allocated but is reserved for
  non-authority scope issues (budget, rate limit, quota — future
  work). Authority refusals (missing scopes at endpoint dispatch)
  now use 262.

## Out of scope (v00)

- Library / archive of approved proposals (future).
- Cross-server proposal sharing.
- Persistent synthesis surviving server restart (durable storage).
- Async PROPOSE for servers that don't opt in (sync-only default).
- Path-template routing for `QUERY /proposals/{proposal_id}` —
  the v00 implementation uses `QUERY /proposals` + body param.
