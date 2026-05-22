# Path grammar

AGTP path grammar exists to prevent **verb leakage**: the situation where a verb token (`GET`, `BOOK`, `RECONCILE`) ends up in a path segment instead of where it belongs, on the method line. The grammar enforces that single rule plus a couple of structural minimums; everything else — casing, separator choice, segment depth, parameter naming — is operator judgment.

AGTP is its own protocol, not HTTP-adjacent. There are no reserved path prefixes (`/.well-known/`, `/_agtp/`); there is no browser-bar use case to accommodate. Server metadata is exposed via AGTP methods at AGTP-native paths (see [Built-in endpoints](#built-in-endpoints)).

## What the grammar enforces

A request path **MUST**:

- begin with `/`,
- not end with `/` (except the bare root `/`),
- not have any segment whose normalized form (uppercase, `-` and `_` stripped) matches an approved method in `core/methods.json` or a legacy HTTP method.

Parameter segments wrapped in braces (`{order_id}`, `{customer-id}`) are exempt from the verb-token check — operators choose parameter names freely.

That's it. The validator is in [`core/path_grammar.py`](../core/path_grammar.py) and is called from the dispatcher gate (Phase 1+) and from the OpenAPI converter (Phase 5).

## What the grammar does NOT enforce

- **Casing.** `/Mixed_Case-Path`, `/UPPER`, `/CamelCase` are all valid.
- **Separator choice.** Hyphens, underscores, mixed — all fine.
- **Segment depth.** A single segment or fifty are equally legal as long as the verb-token rule holds.
- **Parameter naming style.** `{customer_id}`, `{customerId}`, `{customer-id}` — operator's call.
- **No reserved prefixes.** `/_agtp/...` and `/.well-known/...` are ordinary paths. The dispatcher does not special-case them.

The protocol does not impose style preferences. Style is for organizations, style guides, and lint rules — not the wire.

## Query strings

AGTP accepts query strings on the request line:

```
AGTP/1.0 SCHEDULE /meeting?date=050526&attendees=alice%2Cbob
```

The wire parser splits the path at the first `?`. Path stays clean (`/meeting`); query parses into a string-valued dict (`{date: "050526", attendees: "alice,bob"}`) on `AGTPRequest.query`.

At dispatch time the query parameters merge into the request input alongside body parameters, then the merged input goes through the endpoint's input schema validator. **Body wins on key conflicts** — the documented contract is that authoritative input lives in the body; the query string is a convenience for callers that want path-style URLs.

The merged input flows through the same validator that handles body-only inputs; `additionalProperties: false` is intact, so a typo in either source surfaces as `input-validation-failed`.

Repeated query keys collapse to the last value (`?tag=a&tag=b&tag=c` → `{tag: "c"}`). Multi-valued shapes ride in the body.

## Fragments

Fragments (`/path#anchor`) are rejected at the wire layer with a structured `WireFormatError`. URI fragments are client-side-only by URI convention; AGTP traffic carries no browser-bar use case. The `#` character in the request line is always malformed.

## Built-in endpoints

AGTP exposes server metadata via AGTP methods, not HTTP-style well-known locations. The full taxonomy lives in [Endpoint tiers](endpoint-tiers.md) — this section covers the Tier A surface from the path-grammar angle.

The pattern: at server startup, the registry is populated with built-in endpoints alongside operator-authored TOML. Built-ins use ordinary AGTP semantics — they show up in the manifest, agents DISCOVER them naturally, the dispatcher gates fire normally. They differ from operator endpoints only in that the `EndpointSpec` and handler closure are constructed in code rather than loaded from a file.

Today the only built-in is `DISCOVER /methods`, which returns a lightweight `{method, path, description}` listing of every registered endpoint — the same information surfaced by the manifest's `endpoints` array, but without the semantic block / parameters / handler-binding overhead. The endpoint contract:

| Field | Value |
|---|---|
| Method | `DISCOVER` |
| Path | `/methods` |
| Output | `methods: array` — one entry per registered endpoint, with `method`, `path`, and `description` fields |
| Required scopes | none |
| Errors | none |

Operator override: TOML can declare a custom `DISCOVER /methods` endpoint with different semantics; the built-in registration silently skips on duplicate. The operator's choice wins.

Clients wanting the full server policy (allow / disallow / legacy / redirects) fetch the manifest via target-less `DISCOVER` and read the `policies.methods` block. Pre-§6 servers exposed the policy file content via `QUERY /methods`; that built-in is retired (see `agtp-api §6`/`§8`).

The pattern is extensible — see [`server/builtins.py`](../server/builtins.py). Future built-ins might include `DISCOVER /catalog-version` (the catalog version a client should compare against) or domain-specific equivalents.

## What about HTTP gateways?

Out of scope for AGTP itself. Operators that need to expose AGTP behaviors over HTTP (for browser-friendly metadata, for legacy integration, for ops dashboards) should run a separate HTTP service that reads from the AGTP server. The translation logic — what becomes a `/.well-known/...` resource, what becomes a public REST endpoint — is a deployment concern, not a protocol concern.

## Worked examples

| Path | Verdict | Why |
|---|---|---|
| `/` | ✓ | the bare root |
| `/orders` | ✓ | not a verb |
| `/orders/{order_id}` | ✓ | parameterized |
| `/orders/{order_id}/line-items` | ✓ | hyphenated; `line-items` not a verb |
| `/Mixed_Case-Path` | ✓ | casing irrelevant |
| `/orders/get` | ✗ | `get` segment normalizes to `GET` (legacy verb) |
| `/get/orders` | ✗ | same; whole-segment verb |
| `/orders/cancel` | ✗ | `cancel` is `CANCEL` (catalog verb) |
| `/users/{id}/get-history` | ⚠ | path grammar accepts (dashes stripped → `GETHISTORY` not a verb), but the OpenAPI converter flags it as a soft warning |
| `/orders/` | (normalized to `/orders`) | trailing slash refused on non-root |
| `/path?date=2026-05-12` | ✓ | query strings allowed |
| `/path#fragment` | ✗ | fragments rejected at wire layer |

## See also

- [`docs/endpoint-toml.md`](endpoint-toml.md) — endpoint TOML schema; the `path` field references this grammar.
- [`docs/methods.md`](methods.md) — verb catalog + 459 method violation.
- [`docs/catalog-evolution.md`](catalog-evolution.md) — what happens when the catalog adds or removes verbs that interact with paths.
