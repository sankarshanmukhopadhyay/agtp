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
