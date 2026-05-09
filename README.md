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
AMG (Agent Method Grammar) validator is duplicated under
`server/amg/` and `client/amg/` deliberately, mirroring SMTP's MTA
client / server split: same protocol, two distinct user agents that
may evolve independently.

```
agtp/
├── core/                 AGTP wire-protocol primitives (shared)
│   ├── wire.py             AGTPRequest/Response framing
│   ├── ids.py              URI + agent-ID parsing (Forms 1, 1a, 2)
│   ├── identity.py         Agent Document v2 schema
│   ├── manifest.py         Server Manifest dataclasses
│   ├── status.py           AGTP status code helpers (455/456/457/458/550/551 + HTTP)
│   ├── handshake.py        client-side matching outcome
│   ├── render.py           HTML identity-card renderer
│   └── _paths.py           cross-platform path normalization
│
├── server/               AGTP server product
│   ├── main.py             python -m server  /  agtp-server
│   ├── methods.py          12-method registry + dispatch
│   ├── manifest.py         Server Manifest generation
│   ├── config.py           agtp-server.toml loader
│   ├── negotiation.py      PROPOSE counter/refuse fallback (vestigial)
│   ├── synthesis/          composition runtime (policies, recipes, plan exec)
│   ├── synthesis_runtime.py  back-compat shim (re-exports from server.synthesis)
│   ├── amg/                AMG validator (server-side)
│   ├── examples/           opt-in custom-method modules
│   ├── agents/             reference agent docs (Lauren, Orchestrator, legacy/)
│   ├── agtp-server.toml    reference config
│   ├── agtp-recipes.toml   starter synthesis recipes
│   └── run_demo.sh         end-to-end 26-scenario demo
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
│   └── amg/                AMG validator (client-side; agtp-amg)
│
├── registry/             AGTP registry product
│   └── main.py             python -m registry  /  agtp-registry
│
├── mcp-on-agtp/          MCP-on-AGTP bridge product (in development)
├── ietf/                 IETF Internet-Draft sources
├── docs/                 deployment + cross-platform notes
├── scripts/              VPS deploy automation
├── tests/                cross-product test suite
├── pyproject.toml        installable: `pip install -e .`
└── README.md
```

## Client products

The AGTP client is a single Python package (`client/`) with two
frontends:

- **CLI** (`agtp`, `agtp-curl`, `agtp-migrate`) — for scripts,
  automation, CI, and programmatic use. Lives in `client/cli/`.
- **Elemen** (`elemen`) — graphical desktop browser for AGTP, built
  on pywebview. Lives in `client/elemen/`.

Both frontends call into the same `client.core_client` module for
protocol work (URI resolution, connection handling, response parsing)
and the same `client.amg` package for grammar validation. Updates to
the wire protocol or to AMG land in both interfaces simultaneously.

After `pip install -e .`:

```bash
agtp agtp://agents.agtp.io           # CLI manifest fetch
elemen                               # launch the GUI browser
agtp-amg path/to/method.json         # AMG validation tool
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
- Twelve embedded methods (six cognitive + six mechanics) plus the
  AMG validator that gates every custom method registration and every
  PROPOSE proposal.

## Three URI types, three entity types

AGTP recognizes three URI forms; each addresses a fundamentally
different kind of entity. The elemen browser renders each one
differently, with a tab structure that matches the entity type.

| URI                                  | Entity      | Analogy        |
|--------------------------------------|-------------|----------------|
| `agtp://{host}`                      | **Server**  | a workplace    |
| `agtp://{agent-id}` or `…@{host}`    | **Agent**   | a user         |
| `agtp://{host}` (with `hosts_protocols`) | **Application server** | applications hosted on AGTP (MCP, OpenAPI, GraphQL bridges) |

```
agtp://{agent-id}                  Form 1   - canonical, registry lookup
agtp://{agent-id}@{host}[:{port}]  Form 1a  - direct host
agtp://{host}[:{port}]             Form 2   - server-level (no agent ID)
```

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

## AMG (Agent Method Grammar)

AMG is the validation layer that makes runtime method synthesis safe.
Every method — embedded, custom, or proposed at runtime via PROPOSE —
passes the same nine-pass validator before becoming invocable. The
validator lives in `agtp/amg/`; the public surface is:

```python
from server.amg import AMGMethodSpec, ParamSpec, validate, InvalidMethodError

spec = AMGMethodSpec(
    name="RECONCILE",
    semantic_class="action-intent",
    category="transact",
    description="Reconcile transactions for a given account and period.",
    idempotent=False,
    state_modifying=True,
    required_params=[
        ParamSpec(name="account_id", type="string", description="ledger account"),
        ParamSpec(name="period",     type="string", description="time window"),
    ],
    optional_params=[],
    error_codes=[400, 422, 455],
    source="amg/1.0",
    namespace="acme-finance",
)
result = validate(spec)
print(result.valid, result.error)
```

### The nine passes (in order)

| # | Pass | Checks |
|---|---|---|
| 1 | lexical | name matches `/^[A-Z]{3,32}$/` |
| 2 | reserved | not in `HTTP_METHODS`; not in `EMBEDDED_METHODS` for `source=amg/1.0` |
| 3 | semantic-class | one of `action-intent` / `query-intent` / `protocol-mechanic`; the last is embedded-only |
| 4 | stoplist | not a noun, adjective, or static state (suggestion attached) |
| 5 | required-fields | all required fields present; `error_codes` includes 422; namespace required for `amg/1.0`; namespace forbidden for `agtp/1.0` |
| 6 | description | ≥ 20 chars, non-stub (no TODO / placeholder / etc.) |
| 7 | parameters | snake_case names, recognized types, descriptions present, `object`/`array` carry a JSON Schema, no name collisions |
| 8 | schemas | each schema is valid JSON Schema (Draft 7 when `jsonschema` is installed; structural fallback otherwise) |
| 9 | substitution | substitution targets exist, no self-reference, no duplicates |

Passes run in declared order; the first failure aborts and surfaces a
structured `ValidationError` with a machine-readable code and a
human-readable suggestion.

### Integration with the runtime

- **`register_custom`** runs AMG validation on the proposed spec before
  registering. Failed validations raise `InvalidMethodError` (which
  inherits from `ValueError`, so existing call sites that
  `except ValueError` keep catching refusals).
- **`handle_propose`** filters proposals through AMG before the
  negotiation policy. Lexical / reserved / stoplist / semantic-class
  failures return **422 Unprocessable** with
  `error.code='negotiation-refused'`, `error.reason="ambiguous"`,
  and the AMG error code in the body's `amg_code` field. The benign
  case where a proposal names an embedded method (the
  accept-with-synthesis path) is allowed through.

### `agtp-validate` CLI

After `pip install -e .`:

```bash
agtp-validate path/to/method.json                  # validate one spec
agtp-validate path/to/methods/                     # validate every *.method.json
agtp-validate --check-substitution BOOK            # show substitution candidates
agtp-validate --known-methods extra-methods.json   # extend the universe
```

Output is per-pass (`✓` / `✗` with detail), with a final `VALID` /
`INVALID` summary. Exit code 0 on success, 1 on validation failure,
2 on argument or I/O errors.

### Substitution catalog

`server.amg.DEFAULT_SUBSTITUTIONS` ships seed equivalence classes
(reservation, retrieval, execution, validation, creation). Servers
discover candidates with `find_substitutes(name, registry)`. The
ecosystem catalog (future work) is the canonical extension point.

### Optional dependency

AMG works without external dependencies. Installing the
`amg-schemas` extra (`pip install -e ".[amg-schemas]"`) pulls in
`jsonschema>=4` so Pass 8 uses the Draft 7 metaschema validator
instead of the structural fallback.

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
| 403 | Forbidden | Agent lacks authority for the requested action; carries soft-deny refusals via `error.code` (e.g. `method-not-permitted-for-agent`, `wildcards-refused`) |
| 404 | Not Found | Target resource or agent not found |
| 408 | Timeout | TTL exceeded before method could execute |
| 409 | Conflict | Method conflicts with current state |
| 410 | Gone | Agent has been Revoked or Deprecated; canonical Agent-ID is permanently retired |
| 422 | Unprocessable | Request well-formed but semantically invalid; PROPOSE refusal (`error.code='negotiation-refused'`) and counter-proposal responses (body carries `counter_proposal`) ride this code |
| 429 | Rate Limited | Agent is exceeding permitted request frequency |
| 455 | Scope Violation | Requested action is outside declared Authority-Scope. *AGTP-specific.* |
| 456 | Budget Exceeded | Method execution would exceed the Budget-Limit declared in the request. *AGTP-specific.* |
| 457 | Zone Violation | Request would route outside the AGTP-Zone-ID boundary; SEP-enforced. *AGTP-specific.* |
| 458 | Counterparty Unverified | PURCHASE counterparty failed merchant identity verification (Merchant-ID absent, Merchant-Manifest-Fingerprint mismatch, or merchant in non-Active lifecycle state). *AGTP-specific.* |
| 459 | Grammar Violation | Method-Grammar header pathway: an unrecognized method name accompanied by `Method-Grammar: AMG/1.0` failed AMG's lexical / reserved / stoplist check. *AGTP-specific.* |
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
| 460 | Reserved |
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
| 454 Grammar Violation | **459** Grammar Violation | Method-Grammar header pathway (pre-dispatch). Application-layer grammar checks may still use 422 + `error.code='grammar-violation'`. |
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

## Negotiation (PROPOSE)

PROPOSE has three documented outcomes:

- **Accept (200)** - server returns a `Synthesis` mapping the proposal
  onto an existing method. Subsequent calls quote the
  `Synthesis-Id` header to invoke through the synthesis.
- **Refuse (422 `negotiation-refused`)** - body carries
  `error.reason` and `error.explanation`.
- **Counter (422 with `counter_proposal` body)** - body carries a
  `counter_proposal` with the MethodSpec the server is willing to
  admit instead.

The client gains `--negotiate` (auto-issue PROPOSE on a 403 soft-deny)
and `--auto-accept-counter` (re-invoke under a 422 counter-proposal
without prompting).

### Method-Grammar header pathway

Lighter-weight than PROPOSE: a client that wants to invoke an
AMG-conformant method against a server *without* prior negotiation
includes the `Method-Grammar: AMG/1.0` header on the request. When
the dispatcher sees an unrecognized method name plus this header, it:

1. Runs AMG's name-targeted passes (lexical, reserved, stoplist) on
   the method name. Failure returns **459 Grammar Violation** with
   the failed pass and AMG error code in the body.
2. Verifies `requires.wildcards: true` on the agent document.
   Failure returns **403 wildcards-refused**.
3. Verifies `policy.wildcards_accepted: true` on the server.
   Failure returns **403 wildcards-refused**.

If all three checks pass, the server returns **200** with a
structured invitation:

```json
{
  "method": "RECONCILE",
  "status": "amg-conformant",
  "executable": false,
  "next_action": "PROPOSE",
  "negotiable": true,
  "explanation": "Method name 'RECONCILE' is AMG-conformant but no handler is registered on this server. Issue a PROPOSE to negotiate instantiation, or check the server manifest for similar registered methods."
}
```

`next_action` becomes `"no_negotiation_available"` and `negotiable`
becomes `false` when the server's `policy.negotiable` is `false`.

The `Method-Grammar: AGIS/1.0` header value is also accepted during
the AGIS → AMG transition; the response carries an HTTP-style
`Warning: 299 - "Method-Grammar value AGIS/1.0 is deprecated; use
AMG/1.0"` header advising clients to upgrade.

PROPOSE is still the path that *instantiates* a method; Method-Grammar
is the path that *probes* whether a method could be instantiated.

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

### Cross-platform notes

Git Bash on Windows reports POSIX-form paths (`/x/agtp/server`) from
`pwd`; Python on Windows would otherwise misinterpret those as paths
on the current drive. Anywhere a path crosses the shell-to-Python
boundary, use `agtp._paths.normalize()` (the demo script and the
package internals do).

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
