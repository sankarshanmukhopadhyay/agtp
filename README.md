# AGTP — Agent Transfer Protocol

A dedicated application-layer protocol for AI agent traffic.
Specification, Internet-Draft, and reference implementation.

- **IETF submission:** `draft-hood-independent-agtp-06`
- **IANA-registered ports:** 4480/TCP (`agtp`) and 4480/UDP (`agtp-quic`)
- **Reference implementation:** `core/`, `server/`, `client/`, `registry/` (this repository)
- **First registered agent:** Lauren —
  `agtp://d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230`

## Repository layout

This repo is a **monorepo of products**, all sharing the AGTP wire
format defined in `core/`. Each product is its own top-level
directory with its own entry point, agents, and configuration. The
client / server split mirrors SMTP's MTA: same protocol, two
distinct user agents that may evolve independently.

```
agtp/
├── core/                 AGTP wire-protocol primitives (shared)
│   ├── wire.py             AGTPRequest/Response framing
│   ├── ids.py              URI + agent-ID parsing (Forms 1, 1a, 2)
│   ├── identity.py         Agent Document v2 schema
│   ├── manifest.py         Server Manifest dataclasses
│   ├── status.py           AGTP status code helpers (455–460, 550/551 + HTTP)
│   ├── handshake.py        client-side matching outcome
│   ├── render.py           HTML identity-card renderer
│   ├── methods.json        curated AGTP method catalog (~425 methods)
│   ├── methods.py          method-name validator (catalog lookup)
│   ├── path_grammar.py     path validator (verb-in-path rejection)
│   ├── endpoint.py         SemanticBlock + EndpointSpec primitives
│   └── _paths.py           cross-platform path normalization
│
├── server/               AGTP server product
│   ├── main.py             python -m server  /  agtp-server
│   ├── methods.py          12-method registry + dispatch
│   ├── manifest.py         Server Manifest generation
│   ├── config.py           agtp-server.toml loader (incl. [policies.methods])
│   ├── negotiation.py      find_counter_proposal helper
│   ├── synthesis/          composition runtime (policies, recipes, plan exec)
│   ├── synthesis_runtime.py  back-compat shim (re-exports from server.synthesis)
│   ├── examples/           opt-in custom-method modules
│   ├── agents/             reference agent docs (Lauren, Orchestrator, legacy/)
│   ├── agtp-server.toml    reference config
│   ├── agtp-recipes.toml   starter synthesis recipes
│   └── run_demo.sh         end-to-end 29-scenario demo
│
├── client/               AGTP client product (one package, two frontends)
│   ├── core_client.py      shared protocol logic (URI resolution, connections,
│   │                       FetchResult envelope)
│   ├── cli/                terminal frontends
│   │   ├── main.py           agtp                       (python -m client)
│   │   ├── curl.py           agtp-curl diagnostic shim  (python -m client.cli.curl)
│   │   └── migrate.py        agtp-migrate v1->v2 tool   (python -m client.cli.migrate)
│   ├── elemen/             desktop GUI frontend
│   │   ├── app.py            pywebview entry            (elemen / python -m client.elemen.app)
│   │   ├── bridge.py         pywebview <-> Python adapter
│   │   └── ui/               HTML / CSS / JS
│
├── scripts/              build_methods.py + deployment automation
├── registry/             AGTP registry product
│   └── main.py             python -m registry  /  agtp-registry
│
├── agtp/                 Python handler SDK (import name = `agtp`)
│
├── sdk/                  Handler libraries — one per language
│   ├── agtp-go/            Go library + tests
│   ├── agtp-node/          npm package (TypeScript)
│   └── agtp-rust/          Cargo crate
│   (PHP SDK lives in the external agtp-php repo — see NAMING.md)
│
├── runtimes/             Gateway-protocol clients — bridge agtpd to a language
│   ├── mod_go/             Go binary
│   ├── mod_node/           Node CLI
│   ├── mod_python/         python -m mod_python
│   └── mod_rust/           Rust binary
│   (PHP runtime lives in the external agtp-php repo)
│
├── operational/          Daemon-side plugins — load via --load-module
│   ├── mod_audit/          Append-only JSONL audit log (Ed25519-signed)
│   ├── mod_cache/          Response cache (LRU + TTL)
│   └── mod_proxy/          Forward AGTP requests to upstream agtpd
│
├── connectors/           Framework + cross-protocol bridges (in-tree)
│   └── agtp-a2a/           A2A-on-AGTP bridge
│   (Framework integrations — Drupal, Symfony, Laravel, WordPress —
│    and the MCP bridge live in their own external repos. See
│    "External repos" in NAMING.md.)
│
├── ietf/                 IETF Internet-Draft sources
├── docs/                 deployment + cross-platform notes
├── tests/                cross-product test suite
├── samples/              reference handler programs for each runtime
├── tools/                catalog diff, openapi import, keygen, agent-cert gen
├── NAMING.md             which prefix / underscore / hyphen and why
├── pyproject.toml        installable: `pip install -e .`
└── README.md
```

See [`NAMING.md`](NAMING.md) for the naming conventions across all
these directories — why some are forced (Drupal modules require
underscores; Python imports require valid identifiers) and which
packages live in their own external repos.

## Client products

The AGTP client is a single Python package (`client/`) with two
frontends:

- **CLI** (`agtp`, `agtp-curl`, `agtp-migrate`) — for scripts,
  automation, CI, and programmatic use. Lives in `client/cli/`.
- **Elemen** (`elemen`) — graphical desktop browser for AGTP, built
  on pywebview. Lives in `client/elemen/`.

Both frontends call into the same `client.core_client` module for
protocol work (URI resolution, connection handling, response parsing)
and the same `core.methods` / `core.path_grammar` modules for verb
and path validation. Updates to the wire protocol or to the verb
catalog land in both interfaces simultaneously.

After `pip install -e .`:

```bash
agtp agtp://agents.agtp.io                    # CLI manifest fetch
agtp agtp://agents.agtp.io RECONCILE          # invoke a method
agtp agtp://agents.agtp.io RECONCILE --grammar-check   # probe a verb
elemen                                        # launch the GUI browser
```

On Windows, launch the GUI without a console window via the
windowed Python launcher:

```powershell
pyw -3.13 -m client.elemen.app
```

## What this repo demonstrates

- Canonical AGTP URIs (`agtp://{agent-id}`) resolve end-to-end via
  registry lookup.
- Form 1a (`agtp://{agent-id}@{host}`) bypasses the registry for direct
  resolution before federated infrastructure exists.
- Form 2 (`agtp://{host}`) addresses the server itself; DISCOVER
  returns a Server Manifest at `application/vnd.agtp.manifest+json`.
- Agent Identity Documents in `application/vnd.agtp.identity+json`
  carry the v2 identity schema (skills + requires + scopes_accepted).
- Content negotiation produces JSON, YAML, or rendered HTML from the
  same URI based on the client's `Accept` header.
- Twelve embedded methods (six cognitive + six mechanics) plus a
  curated catalog of ~425 verbs the dispatcher validates against;
  unknown verbs return 459, paths with verb-tokens return 460.

## Three URI types, three entity types

AGTP recognizes six URI forms (per `agtp §11`); each addresses a fundamentally
different kind of entity. The elemen browser renders each one
differently, with a tab structure that matches the entity type.

| URI                                  | Entity      | Analogy        |
|--------------------------------------|-------------|----------------|
| `agtp://{host}`                      | **Server**  | a workplace    |
| `agtp://{agent-id}` or `…@{host}`    | **Agent**   | a user         |
| `agtp://{host}` (with `hosted_protocols`) | **Application server** | applications hosted on AGTP (MCP, OpenAPI, GraphQL bridges) |

```
agtp://{agent-id}                       Form 1   - canonical identity
agtp://{agent-id}@{host}                Form 1a  - identity + host
agtp://{host}                           Form 2   - server-level discovery
agtp://{domain}                         Form 2a  - organization-level
agtp://{domain}/agents/{name}           Form 3   - domain-anchored agent
agtp://agtp.{domain}/agents/{name}      Form 4   - subdomain-anchored agent
```

Canonical URIs omit the port — the default 4480 is implicit
(mirroring how HTTPS URIs omit `:443`). The parser tolerates
`:port` for dev / test fixtures but `format_uri` never emits it.

See [`docs/uri-forms.md`](docs/uri-forms.md) for the full reference
including the server-side resolution flow for Forms 3 / 4.

### Agents are users, not APIs

This is the conceptual frame that makes the protocol coherent: agents
do not "have methods" in the way HTTP services do. **Servers have
methods. Agents have permissions to invoke methods at servers.**

That distinction shows up everywhere:

- An agent's `requires.methods` is the set of method names the
  principal has authorized that agent to invoke. Soft-deny refusals
  return **403 Forbidden** with `error.code='method-not-permitted-for-agent'`;
  the body explanation says the principal has not authorized the
  method, not that the agent "lacks" anything.
- The elemen browser renders agents as user profiles
  (Identity, Goals, Skills, Permissions, Credentials). It does not
  show a Methods tab on agent URIs because the concept does not
  apply.
- Servers render as workplace dashboards (Server identity, Methods
  inventory, APIs preview, Hosted agents, Hosted protocols, Policies).
  Methods, APIs, and protocols are all server-level concepts.
- Application servers (servers that bridge a non-AGTP protocol like
  MCP) render their bridged protocol's catalog in a dedicated tab.

### Server URIs (workplaces)

Form 2 addresses the server itself. Sending DISCOVER to a Form 2 URI
returns a Server Manifest at media type
`application/vnd.agtp.manifest+json`. The manifest declares the server's
identity, the methods it supports (embedded + custom, bucketed), the
agents it discloses according to its policy, and (when populated) the
APIs it exposes and any non-AGTP protocols it bridges.

```bash
# Server Manifest (defaults to DISCOVER on Form 2 URIs):
agtp agtp://localhost:4480

# Equivalent, explicit:
agtp agtp://localhost:4480 DISCOVER

# Per-agent identity (Form 1 / 1a remain unchanged):
agtp agtp://{lauren-id}
```

A `agtp-server.toml` next to the working directory (or pointed at by
`--config`) declares the issuer, operator, contact, policy posture, and
agent disclosure level that surface in the manifest. When no config is
present the server uses sensible defaults so local development needs no
ceremony.

## Agent Document v2

The Agent Document schema is versioned. v2 replaces the v1 `capabilities`
field with two complementary declarations:

- **`skills`** - prose, human-readable, the primary "what does this
  agent do" surface.
- **`requires`** - structured: `methods`, `scopes`, and a `wildcards`
  flag for orchestrators that accept any method.

v1 documents continue to load. `from_dict` detects the older shape and
converts on the fly: `capabilities` lifts to `requires.methods`,
`skills` is seeded from the description, and the result carries
`document_version: "v1-migrated"`.

To migrate a v1 file to v2 on disk:

```bash
agtp-migrate path/to/agent.json
agtp-migrate path/to/dir/                # all *.agent.json under dir
agtp-migrate --check path/to/agent.json  # report only
```

A `.v1.bak` backup is written alongside each migrated file unless
`--no-backup` is set.

## Method validation: catalog + path grammar

The protocol's method vocabulary is the curated method list at
[`core/methods.json`](core/methods.json) — ~425 approved AGTP methods
plus the 12 embedded primitives plus 5 legacy HTTP methods.
Validation reduces to two cheap checks:

  * **Method-name lookup** ([`core/methods.py`](core/methods.py)):
    `is_approved_verb(name)` against the catalog. Verbs absent from
    the catalog return **459 Method Grammar Violation** with
    close-match suggestions in the body.
  * **Path grammar** ([`core/path_grammar.py`](core/path_grammar.py)):
    `validate_path(path)` rejects paths that don't start with `/`,
    have a trailing slash on a non-root path, or embed a verb token
    in any segment. Failures return **460 Endpoint Grammar Violation**.

> **Status of 460.** The path-grammar validator and the **460
> Endpoint Grammar Violation** status code are wired through every
> layer (dispatcher, status helpers, CLI, drawer) and tested in
> isolation. They are reserved and ready, but **460 does not yet
> fire on real wire traffic**: the current AGTPRequest carries a
> method line without a path component, so `validate_path("/")`
> always succeeds at dispatch time. Full operational use of 460
> lands when the endpoint registry binds methods to specific paths
> and the wire format starts carrying that path on the request
> line. Until then, treat 460 as part of the protocol's documented
> status surface, not part of the daily traffic.

The catalog is a curated list with a small runtime helper surface:

```python
from core.methods import (
    is_approved_verb, categorize, get_legacy_preferred,
    find_close_matches,
)

is_approved_verb("RECONCILE")     # True
categorize("AUDIT")                # ['analysis', 'domain_spanning']
get_legacy_preferred("GET")        # 'FETCH'
find_close_matches("PROPOSEX")     # ['PROPOSE']
```

### Per-server method policy: `[policies.methods]`

Each server declares its method policy under `[policies.methods]`
in [`agtp-server.toml`](server/agtp-server.toml) — which catalog
verbs it admits, which legacy HTTP methods it opts into, and which
`(method, path)` pairs are rewritten before dispatch. The block:

```toml
[policies.methods]
allow    = "*"                      # or a list: ["QUERY", "RECONCILE"]
disallow = ["PATCH", "TRANSFER"]    # explicit refusals
legacy   = ["GET", "POST"]          # opt-in HTTP verbs; "*" / "NONE" also accepted

[[policies.methods.redirects]]
from_method = "BOOK"
from_path   = "/room"               # optional; method-only redirect omits both _path fields
to_method   = "RESERVE"
to_path     = "/room"
```

The same content surfaces in the server manifest under
`policies.methods`, so clients can introspect the policy without
side-channel access to the config file. Embedded methods (the 12
protocol primitives) bypass the policy gate so a mis-authored
disallow can't take a server off-protocol.

> Pre-§6 servers used a separate `methods.txt` file format with
> `Allow:` / `Disallow:` / `Legacy:` / `Redirect:` directives. That
> file format is retired (see `agtp-api §8`); move its content into
> `[policies.methods]` of `agtp-server.toml`.

### Dispatcher resolution order

The dispatcher applies these gates in order:

  1. **Synthesis-Id** — route to the synthesis runtime if the
     header names an active synthesis.
  2. **459 Method Grammar Violation** — verb not in the catalog
     (and not opted into via `policies.methods.legacy`).
  3. **460 Endpoint Grammar Violation** — path malformed or
     contains a verb token.
  4. **405 Method Not Allowed** — `policies.methods` refuses this
     verb on this server.
  5. **Redirect** — `policies.methods.redirects` rewrites
     `(method, path)`.
  6. **Registry lookup** — handler resolves and runs.

The ``Method-Grammar`` header pathway the protocol previously
shipped was retired in this revision; the catalog gate at the top
of dispatch carries the same job without the wire-level header.

### Building the catalog

The canonical method list lives in [`scripts/methods_source.py`](scripts/methods_source.py).
Run [`scripts/build_methods.py`](scripts/build_methods.py) after editing
the source list to regenerate `core/methods.json`:

```bash
python scripts/build_methods.py
```

The build script merges duplicates (verbs that appear under multiple
categories), excludes legacy HTTP names from the curated catalog
(POST / PATCH / etc. are legacy-only by spec), and emits the JSON
in canonical order: embedded first, then alphabetical within each
category.

### Catalog evolution

The catalog uses semver. Verbs can be deprecated with
``deprecated_in`` / ``removed_in`` / ``successor`` metadata; the
dispatcher stamps an ``AGTP-Catalog-Warning`` advisory header on
responses for deprecated invocations. The server manifest exposes
its catalog version on every DISCOVER. The
[`agtp-catalog-diff`](tools/catalog_diff.py) CLI compares two
catalogs and scans a deployment for breakage before upgrade.

Run before deploying a new catalog:

```bash
agtp-catalog-diff core/methods.json proposed/methods.json \
    --against-deployment ./agtp-server/
```

Full operator runbook in [`docs/catalog-evolution.md`](docs/catalog-evolution.md).

## Status codes

AGTP mixes standard HTTP status codes with a small set of
AGTP-specific codes drawn from ranges unassigned in the IANA HTTP
Status Code Registry, so AGTP-specific numbers cannot collide with
HTTP codes carried in payloads.

### Active codes

| Code | Name | Meaning |
|------|------|---------|
| 200 | OK | Method executed successfully |
| 202 | Accepted | Method accepted; execution is asynchronous |
| 204 | No Content | Method executed; no response body |
| 400 | Bad Request | Malformed AGTP request |
| 401 | Unauthorized | Agent-ID not recognized or not authenticated |
| 261 | Negotiation In Progress | PROPOSE queued for async evaluation; body carries `proposal_id` and polling instructions. *AGTP-specific (§7).* |
| 262 | Authorization Required | Agent's authority insufficient — scope-required, wildcards-required, credentials-missing, anonymous-discovery-disabled. *AGTP-specific (§7).* |
| 263 | Proposal Approved | PROPOSE accepted; body carries `synthesis_id`, `endpoint`, `persistent`, `expires_at`. *AGTP-specific (§7).* |
| 400 | Bad Request | Body well-formedness failure; PROPOSE bodies use `error.code='bad-request'` with `error.issue` |
| 403 | Forbidden | Agent lacks authority for the requested action; carries soft-deny refusals via `error.code` (e.g. `method-not-permitted-for-agent`). Pre-§7 also covered scope-required / wildcards-refused; those now use 262 |
| 404 | Not Found | Target resource or agent not found |
| 408 | Timeout | TTL exceeded before method could execute |
| 409 | Conflict | Method conflicts with current state |
| 410 | Gone | Agent has been Revoked or Deprecated; canonical Agent-ID is permanently retired |
| 422 | Unprocessable | Request well-formed but semantically invalid. Pre-§7 also carried PROPOSE refusals (`error.code='negotiation-refused'`); §7 moves those to 463 |
| 429 | Rate Limited | Agent is exceeding permitted request frequency |
| 455 | Scope Violation | Requested action is outside declared Authority-Scope. *AGTP-specific.* |
| 456 | Budget Exceeded | Method execution would exceed the Budget-Limit declared in the request. *AGTP-specific.* |
| 457 | Zone Violation | Request would route outside the AGTP-Zone-ID boundary; SEP-enforced. *AGTP-specific.* |
| 458 | Counterparty Unverified | PURCHASE counterparty failed merchant identity verification (Merchant-ID absent, Merchant-Manifest-Fingerprint mismatch, or merchant in non-Active lifecycle state). *AGTP-specific.* |
| 459 | Method Grammar Violation | Method name is not in the AGTP verb catalog. The body carries close-match suggestions (Levenshtein-2 against the approved set; legacy verbs surface their canonical replacement first). *AGTP-specific.* |
| 460 | Endpoint Grammar Violation | Path violates AGTP path grammar — must begin with `/`, must not end with `/` (except the root), must not embed a verb token in any segment. *AGTP-specific.* |
| 463 | Proposal Rejected | PROPOSE refused; body carries `error.code='proposal-rejected'`, `error.reason` (one of `out-of-scope` / `policy-refused` / `composition-impossible` / `ambiguous`), and optional `error.counter_proposal`. *AGTP-specific (§7).* |
| 500 | Server Error | Internal failure in the responding system |
| 503 | Unavailable | Responding agent or system temporarily unavailable or Suspended |
| 550 | Delegation Failure | A delegated sub-agent failed to complete the requested action. *AGTP-specific.* |
| 551 | Authority Chain Broken | Delegation chain contains an unverifiable or broken identity link. *AGTP-specific.* |

### Reserved for AGTP expansion

These codes are present in the AGTP-specific ranges but are not yet
assigned. They are reserved in the IANA AGTP Status Code Registry
and **MUST NOT** be returned by current implementations.

| Code | Status |
|------|--------|
| 552 | Reserved |
| 553 | Reserved |
| 554 | Reserved |
| 555 | Reserved |

### Migration from earlier drafts

Earlier AGTP drafts used codes that the current registry no longer
admits. Their semantics now ride existing codes, with the body's
`error.code` field preserving the prior framing:

| Old | New | Notes |
|-----|-----|-------|
| 451 Scope Violation | **455** Scope Violation | Renumbered |
| 452 Method Not Permitted for Agent | **403** + `error.code='method-not-permitted-for-agent'` | Folded into Forbidden |
| 453 Zone Violation | **457** Zone Violation | Renumbered |
| 454 Grammar Violation | (split) | Method-name failures now ride **459 Method Grammar Violation**; path failures ride **460 Endpoint Grammar Violation**. The Method-Grammar header pathway was retired; the catalog gate at the top of dispatch carries the same job. |
| 460 Negotiation Refused | **422** + `error.code='negotiation-refused'` | Folded into Unprocessable |
| 461 Counter-Proposal | **422** with `counter_proposal` body | Folded into Unprocessable |
| 462 Wildcards Refused | **403** + `error.code='wildcards-refused'` | Folded into Forbidden |

Precedence at the inbound gate: **wildcards-refused > method-not-permitted-for-agent > 455**.
Embedded mechanics plus DISCOVER/DESCRIBE bypass soft-deny because
they are protocol primitives. The server flag `--no-soft-deny`
disables soft-deny refusals for legacy testing.

## Matching handshake

Before invoking, a client can compare the agent's `requires.methods`
against the server's manifest universe:

```bash
agtp agtp://{agent-id} --match-check
# Match: FULL  (matched=8 missing=0 universe=12)
# Matched (8): CONFIRM, DESCRIBE, DISCOVER, EXECUTE, NOTIFY, PLAN, QUERY, SUMMARIZE
# Server has (12): ...
```

`MatchOutcome.kind` is one of `full` / `partial` / `none`. The match
also notes wildcard policy mismatches so callers can predict 403
`wildcards-refused` responses.

## Wire format and header model

AGTP uses an HTTP-shaped wire format (request line, response line,
RFC 7230 header encoding, Content-Length framing) over TLS 1.3+ on
TCP/4480. The header vocabulary is AGTP-specific and intentionally
small — see [`docs/wire-format.md`](docs/wire-format.md) for the
full surface.

Required on requests:

- **`Agent-ID`** — identifies the invoking agent. Pre-§10 servers
  used `Target-Agent`; the §10 fallback accepts that name with a
  deprecation warning.

Optional on requests:

- **`Authority-Scope`** — claimed scopes for this request,
  validated against the agent's declared scope set.
- **`Session-ID`** — opaque operational session grouping.
- **`Task-ID`** — task tracing; echoed in the response.
- **`Delegation-Chain`** — reserved for v01; v00 rejects with 501.

Required on responses:

- **`Server-ID`** — identifies the server that produced the response.

Optional on responses:

- **`Attribution-Record`** — signed attestation of response origin
  (opt-in via `[audit] attribution_records_enabled`; placeholder
  until §5 JWS signing lands).

Headers retired from earlier drafts: `AGTP-Version`, `AGTP-Method`,
`AGTP-Status` (info is in the request/response line);
`Principal-ID` (info is in the agent document); `Priority`, `TTL`,
`Budget-Limit`, `AGTP-Zone-ID` (not in v00 scope).

## Negotiation (PROPOSE)

PROPOSE has its own status-code family (per `agtp-api §7`):

- **263 Proposal Approved** — server returns a synthesis mapping the
  proposal onto an existing method or composition. Subsequent calls
  carry the `Synthesis-Id` header to invoke through it. Body carries
  `synthesis_id`, `endpoint`, `persistent`, `expires_at`, and
  `granted_duration`.
- **463 Proposal Rejected** — body carries
  `error.code = "proposal-rejected"`, `error.reason` (one of
  `out-of-scope`, `policy-refused`, `composition-impossible`,
  `ambiguous`), `error.explanation`, and an optional
  `error.counter_proposal` with the server's suggested alternative.
- **261 Negotiation In Progress** — server queued the proposal for
  async evaluation; body carries `proposal_id` and the polling path
  (`QUERY /proposals`). Only emitted when
  `[policies.synthesis] async_evaluation_enabled = true`.
- **400 Bad Request** — body well-formedness failure
  (`invalid-json`, `missing-required-field`,
  `malformed-semantic-block`, `malformed-schema`).
- **262 Authorization Required** — agent's authority insufficient
  (also used elsewhere for `wildcards-required` and
  `scope-required`).

See [`docs/propose.md`](docs/propose.md) for the full body shapes,
the reason / issue / type vocabularies, persistent synthesis (with
operator-controlled duration caps), audit logging, and the v00
migration notes.

The client gains `--negotiate` (auto-issue PROPOSE on a 403 soft-deny)
and `--auto-accept-counter` (re-invoke under a 463 counter-proposal
without prompting).

### Probing a verb without committing

The `Method-Grammar` header pathway the protocol previously shipped
was retired. The catalog-based dispatcher carries the same job at
the top of every request: an unknown verb gets **459** with
close-match suggestions, a verb that's in the catalog but has no
handler on this server gets **405**, and an admissible verb runs
normally. The CLI's `agtp <uri> METHOD --grammar-check` flag still
sends a probe and renders the response — a single command that
tells you whether the verb is in the catalog and whether the
server admits it.

### Synthesis runtime

PROPOSE acceptance flows through the
[synthesis runtime](server/synthesis/) — a pluggable composition
layer that builds a [`SynthesisPlan`](server/synthesis/plan.py) from
the proposal and registers it under a `synthesis_id`. Subsequent
calls carrying `Synthesis-Id` execute the plan: each
[`CompositionStep`](server/synthesis/plan.py) is dispatched through
the same machinery as a direct external invocation, so capability
checks, scope assertions, and authority enforcement fire per step.
A synthesis cannot launder authority.

Two composition policies ship today:

  * **Recipe-based** — hand-authored TOML recipes
    ([`server/agtp-recipes.toml`](server/agtp-recipes.toml)) match
    against the proposal and template a multi-step plan. Three
    starter recipes (`EVALUATE`, `AUDIT`, `INSPECT`) demonstrate
    output-threaded, merged, and listed aggregation modes.
  * **Passthrough** — appended automatically as the final fallback;
    a proposal whose name matches an existing method becomes a
    one-step identity plan. This preserves the v1 accept-on-exact-
    match wire shape.

Configure policies in `agtp-server.toml`:

```toml
[synthesis]
policies     = ["recipes"]
recipes_file = "agtp-recipes.toml"
```

Future policies (capability-graph, LLM-driven) plug in via the
[`CompositionPolicy`](server/synthesis/policies.py) protocol without
disturbing the runtime.

Synthesis lifecycle: process-scoped, in-memory, cleared by
`SUSPEND --param synthesis_id=<id>` or by server restart. Future
work introduces durable syntheses tied to long-running session
tokens.

## Quick start

The invocation idiom mirrors `python -m http.server 8000`:

```bash
# Install once (editable; picks up local changes immediately).
pip install -e .

# Start the registry and an agent server. Loopback binds default to
# plaintext, so no --insecure flag is needed for local development.
python -m registry 8080 &
python -m server   4480 --agents-dir server/agents &

# Inspect a server with the curl-equivalent.
agtp-curl DISCOVER agtp://localhost:4480/methods

# Invoke a method via the official client.
agtp agtp://{lauren-id}@localhost:4480 QUERY --param intent="hello"

# Or run the bundled 14-scenario demo end-to-end:
cd v1 && ./run_demo.sh
```

Both `python -m server 4480` (positional) and
`python -m server --port 4480` work. After install, the same
command is also available as the bare name `agtp-server 4480`.

### Gateway mode (recommended for production)

The above runs handlers in the daemon's own process. The recommended
production shape, as of M3 step (b), is to run `agtpd` and a runtime
module as **separate processes** that talk over a Unix-socket gateway.
That matches the httpd + PHP-FPM model: the daemon owns the AGTP
protocol; the runtime module owns the language runtime; a crashing
handler can never take down the daemon.

```bash
# Terminal 1: daemon with gateway socket enabled.
python -m server 4480 \
    --agents-dir server/agents \
    --endpoints-dir endpoints \
    --gateway-socket /tmp/agtpd.sock

# Terminal 2: Python runtime module, loading sample handlers.
python -m mod_python \
    --gateway-socket /tmp/agtpd.sock \
    --load-module samples.gateway_demo
```

When `--gateway-socket` is set, the daemon routes `registered_function`
endpoints over the gateway instead of importing them in-daemon.
Composition recipes, external_service bindings, and the 12 embedded
methods continue to run in-daemon regardless.

See
[`docs/architecture/server-modules.md`](docs/architecture/server-modules.md)
and
[`docs/architecture/gateway-protocol-v1.md`](docs/architecture/gateway-protocol-v1.md)
for the full architecture and protocol references.

### Cross-platform notes

Git Bash on Windows reports POSIX-form paths (`/x/agtp/server`) from
`pwd`; Python on Windows would otherwise misinterpret those as paths
on the current drive. Anywhere a path crosses the shell-to-Python
boundary, use `agtp._paths.normalize()` (the demo script and the
package internals do).

## Architecture

The long-term deployment shape — daemon, modules, and language /
framework libraries — is described in
[`docs/architecture/server-modules.md`](docs/architecture/server-modules.md).
The current Python implementation in `core/` + `server/` is the
reference; the architecture doc describes how it decomposes into an
`agtpd` daemon and language modules (`mod_php`, `mod_python`,
`mod_go`, ...) over a Unix-socket gateway.

Why some directories are hyphenated (`agtp-go/`, `agtp-php/`) and
others use underscores (`mod_python/`, `agtp_drupal/`) — and which
are forced by language / framework rules — is documented in
[`NAMING.md`](NAMING.md).

## Public deployment

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for a step-by-step walkthrough
from fresh Ubuntu 24.04 LTS VPS to AGTP running publicly under your
own domain.

The reference public deployment is at:

- **Registry:** `https://registry.agtp.io`
- **Agents:** `agents.agtp.io:4480`

## Lauren's identity

```
agtp_version:    1.0
agent_id:        d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230
name:            Lauren
principal:       Chris Hood
description:     The first AGTP-identified agent.
status:          active
capabilities:    [DESCRIBE]
scopes_accepted: [identity:read, capability:read]
issuer:          agtp.io
```

## What's not in v1

Deliberate scope cuts, listed for future revisions:

- **Cryptographic signatures.** Agent Documents are unsigned in v1;
  v2 wires in Birth Certificate signing.
- **Trust scores.** Mentioned in the spec but not yet computed.
- **Public registration UI** at `https://register.agtp.io`.
- **AGTP-CERT integration** at `https://ca.agtp.io`.
- **Methods beyond DESCRIBE** — QUERY, BOOK, DELEGATE, etc.
- **`.well-known/agtp` bootstrap** for non-AGTP-native domains.
- **Federated registries** — v1 hardcodes one registry.

## License and IPR

The core protocol specification is open and royalty-free. See
[`ietf/`](ietf/) for the Internet-Drafts and their IPR sections.

## Contributing

The protocol is in active development under Independent Submission to
the IETF. Issues and discussion welcome. Implementation reports —
"I tried to implement v06 and ran into..." — are especially valued.

## Contact

Chris Hood — chris@nomotic.ai
