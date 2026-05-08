# AGTP — Agent Transfer Protocol

A dedicated application-layer protocol for AI agent traffic.
Specification, Internet-Draft, and reference implementation.

- **IETF submission:** `draft-hood-independent-agtp-06`
- **IANA-registered ports:** 4480/TCP (`agtp`) and 4480/UDP (`agtp-quic`)
- **Reference implementation:** `v1/` (this repository)
- **First registered agent:** Lauren —
  `agtp://d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230`

## Repository layout

```
agtp/
├── ietf/                IETF Internet-Draft sources (markdown)
├── agtp/                Reference implementation (Python package)
│   ├── ids.py             Agent ID + URI parsing
│   ├── identity.py        Agent Document schema
│   ├── wire.py            AGTP/1.0 wire format
│   ├── render.py          HTML identity card
│   ├── methods.py         12-method registry (AMG-ready)
│   ├── server.py          python -m agtp.server
│   ├── registry.py        python -m agtp.registry
│   ├── client.py          python -m agtp
│   ├── curl.py            agtp-curl diagnostic shim
│   ├── _paths.py          cross-platform path normalization
│   └── examples/          opt-in custom-method modules
├── v1/                  Backward-compat shims and demo
│   ├── server/agents/   *.agent.json files
│   └── run_demo.sh      end-to-end 14-scenario walkthrough
├── elemen/              native AGTP browser (pywebview)
├── docs/                deployment guide and cross-platform notes
├── scripts/             VPS deploy automation
├── pyproject.toml       installable as `pip install -e .`
└── test_methods.py      method-registry tests
```

The protocol specification and the reference implementation live in
the same repository because they evolve together. Future revisions
land in `v2/`, `v3/` etc.; earlier `vN/` directories are kept for
historical reference.

## What v1 demonstrates

- Canonical AGTP URIs (`agtp://{agent-id}`) resolve end-to-end via
  registry lookup
- Form 1a (`agtp://{agent-id}@{host}`) bypasses the registry for direct
  resolution before federated infrastructure exists
- Agent Identity Documents in `application/vnd.agtp.identity+json` carry
  the eleven-field v1 identity schema
- Content negotiation produces JSON, YAML, or rendered HTML from the
  same URI based on the client's `Accept` header
- DESCRIBE method serves Agent Identity Documents over AGTP wire format
  on port 4480

## URI forms

```
agtp://{agent-id}                  Form 1   - canonical, registry lookup
agtp://{agent-id}@{host}[:{port}]  Form 1a  - direct host
agtp://{host}[:{port}]             Form 2   - server-level (no agent ID)
```

Form 2 addresses the server itself. Sending DISCOVER to a Form 2 URI
returns a Server Manifest at media type
`application/vnd.agtp.manifest+json`. The manifest declares the server's
identity, the methods it supports (embedded + custom, bucketed), and
the agents it discloses according to its policy.

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

## Status codes

In addition to the standard 4xx / 5xx codes, AGTP defines:

| Code | Phrase | When |
|---|---|---|
| 451 | Scope Violation | Caller's scope set is missing what the method requires |
| 452 | Method Outside Agent's Declared Need | Soft-deny: method absent from `requires.methods` and wildcards is false |
| 460 | Negotiation Refused | PROPOSE refused (`out_of_scope` / `ambiguous` / `insufficient` / `policy_refused`) |
| 461 | Counter-Proposal | Server suggests a near-match method; body carries a MethodSpec |
| 462 | Wildcards Refused | Agent declares wildcards but server policy refuses them |

Precedence at the inbound gate: **462 > 452 > 451**. Embedded mechanics
plus DISCOVER/DESCRIBE bypass soft-deny because they are protocol
primitives. The server flag `--no-soft-deny` disables 452/462 for
legacy testing.

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
also notes wildcard policy mismatches so callers can predict 462s.

## Negotiation (PROPOSE)

PROPOSE has three documented outcomes:

- **Accept (200)** - server returns a `Synthesis` mapping the proposal
  onto an existing method. Subsequent calls quote the
  `Synthesis-Id` header to invoke through the synthesis.
- **Refuse (460)** - body carries `reason` and `explanation`.
- **Counter (461)** - body carries a `counter_proposal` with the
  MethodSpec the server is willing to admit instead.

The client gains `--negotiate` (auto-issue PROPOSE on 452/462) and
`--auto-accept-counter` (re-invoke under a 461 counter-proposal
without prompting).

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
python -m agtp.registry 8080 &
python -m agtp.server   4480 --agents-dir v1/server/agents &

# Inspect a server with the curl-equivalent.
agtp-curl DISCOVER agtp://localhost:4480/methods

# Invoke a method via the official client.
agtp agtp://{lauren-id}@localhost:4480 QUERY --param intent="hello"

# Or run the bundled 14-scenario demo end-to-end:
cd v1 && ./run_demo.sh
```

Both `python -m agtp.server 4480` (positional) and
`python -m agtp.server --port 4480` work. After install, the same
command is also available as the bare name `agtp-server 4480`.

### Cross-platform notes

Git Bash on Windows reports POSIX-form paths (`/x/agtp/v1`) from
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
