# Agent Transfer Protocol (AGTP)

**draft-hood-independent-agtp-03** | Informational | Independent Submission

> A dedicated application-layer protocol for autonomous AI agent traffic.

---

## What Is AGTP?

HTTP was designed for humans. AI agents are not humans.

Agent-generated traffic is autonomous, high-frequency, intent-driven,
and stateful across sequences of related requests. HTTP carries no
native semantics to distinguish an agent booking a flight from a
human clicking a link. It provides no protocol-level mechanism for
agent identity, authority scope, or attribution. And it cannot be
evolved to fix this — its method registry is frozen, its
backward-compatibility constraints are decades deep, and
infrastructure-level traffic differentiation is architecturally
impossible within HTTP's design.

AGTP is the dedicated transport layer that AI agents need. It sits
above TLS and below any agent messaging protocol (MCP, ACP, A2A),
providing:

- **Agent-native intent methods** — QUERY, SUMMARIZE, BOOK, SCHEDULE,
  LEARN, DELEGATE, COLLABORATE, CONFIRM, ESCALATE, NOTIFY, DESCRIBE,
  SUSPEND, and PROPOSE — with a growing extended vocabulary organized
  by semantic category
- **Protocol-level agent identity** — Agent-ID, Principal-ID, and
  Authority-Scope on every request, with an optional cryptographic
  certificate extension for verified identity
- **Governance primitives** — ESCALATE as a first-class method,
  authority scope enforcement, delegation chain tracking, and
  attribution records
- **Infrastructure observability** — agent traffic is distinguishable
  from human traffic at the routing layer without application-layer
  parsing
- **Dynamic endpoint negotiation** — PROPOSE method and grammar-based
  validation pathway enabling agents to instantiate endpoints on demand
  without pre-built API definitions

AGTP does not replace MCP, ACP, or A2A. Those are messaging protocols —
they define what agents say. AGTP defines how agent traffic moves.

---

## Status

| Item | Status |
|---|---|
| Internet-Draft | `draft-hood-independent-agtp-05` — active |
| IETF submission | Submitted |
| Working group | Independent submission (no WG assigned yet) |
| Reference implementation | Planned (Python / Go) — contributions welcome |
| Companion specs | See table below |

### Companion Specifications

| Draft | Description |
|---|---|
| `draft-hood-independent-agis-01` | Agentic Grammar and Interface Specification — the native interface definition language for AGTP services |
| `draft-hood-agtp-standard-methods-01` | Tier 2 extended method vocabulary |
| `draft-hood-agtp-agent-cert-00` | X.509 agent certificate extension |
| `draft-hood-agtp-composition-00` | Composition with MCP, A2A, ACP |
| `draft-hood-agtp-discovery-00` | Agent discovery and name service |
| `draft-hood-agtp-web3-bridge-00` | Web3 wallet identity bridge |
| `draft-hood-agtp-merchant-identity-00` | Merchant identity extends agent PURCHASE |

---

## Repository Contents

```
draft-hood-independent-agtp-05.md
draft-hood-independent-agis-01.md
draft-hood-agtp-standard-methods-01.md
draft-hood-agtp-agent-cert-00.md
draft-hood-agtp-composition-00.md
draft-hood-agtp-discovery-00.md
draft-hood-agtp-web3-bridge-00.md
draft-hood-agtp-merchant-identity-00.md

```

---

## The Protocol at a Glance

### Stack Position

```
+-----------------------------------------------------+
|            Agent Application Logic                  |
+-----------------------------------------------------+
|  Messaging Layer  (MCP / ACP / A2A)  [optional]     |
+-----------------------------------------------------+
|   AGTP — Agent Transfer Protocol     [this spec]    |
+-----------------------------------------------------+
|   AGIS — Interface Definition Layer  [companion]    |
+-----------------------------------------------------+
|            TLS 1.3+                  [mandatory]    |
+-----------------------------------------------------+
|         TCP / QUIC / UDP                            |
+-----------------------------------------------------+
```

### Core Methods (Tier 1)

| Method | Category | Intent |
|---|---|---|
| QUERY | Acquire | Semantic data retrieval |
| SUMMARIZE | Compute | Synthesize content |
| BOOK | Transact | Reserve a resource |
| SCHEDULE | Orchestrate | Plan future actions |
| LEARN | Compute | Update agent context |
| DELEGATE | Orchestrate | Transfer task to sub-agent |
| COLLABORATE | Orchestrate | Coordinate peer agents |
| CONFIRM | Transact | Attest to a prior action |
| ESCALATE | Orchestrate | Defer to human authority |
| NOTIFY | Communicate | Push information |
| DESCRIBE | Acquire | Retrieve endpoint capabilities |
| SUSPEND | Orchestrate | Pause session workflow |
| PROPOSE | Orchestrate | Submit dynamic endpoint proposal |

A four-tier method vocabulary extends beyond the core thirteen: Tier 2
standard methods (FETCH, SEARCH, VALIDATE, TRANSFER, MONITOR, RUN, and
~30 others), Tier 3 industry profile methods (healthcare, financial
services, legal, infrastructure), and Tier 4 AGIS-validated custom
methods accepted at the transport layer via the Method-Grammar header
without IANA registration.

### Three Problems AGTP Solves

**1. Undifferentiated agent traffic.** HTTP cannot distinguish agent
requests from human requests at the infrastructure layer. AGTP
provides a dedicated protocol environment — agent traffic is
identifiable at the routing layer without payload parsing.

**2. Semantic mismatch.** HTTP's GET/POST/PUT/DELETE vocabulary was
designed for resource manipulation, not purposeful action. AGTP's
intent-based methods express what an agent is trying to accomplish
at the protocol level.

**3. No protocol-level identity.** HTTP carries no native mechanism
for agent identity, authority scope, or attribution. AGTP embeds
Agent-ID, Principal-ID, and Authority-Scope on every request, with
an optional cryptographic Agent Certificate extension for verified
identity at the transport layer.

---

## New in v03: AGIS Integration and Dynamic Endpoint Negotiation

### AGIS — The Interface Definition Layer

AGTP v03 introduces normative integration with the Agentic Grammar and
Interface Specification (AGIS, `draft-hood-independent-agis-01`). AGIS
is to AGTP what HTML is to HTTP — the native language that AGTP
services use to describe themselves.

An AGIS document served at an AGTP address describes all available
methods, their semantic intent, input/output schemas, confidence
thresholds, and data availability — in a grammar-constrained format
that agents read through natural language inference. No API
documentation required. No pre-training on specific endpoints.

### Grammar-Based Method Validation

The new `Method-Grammar: AGIS/1.0` header enables Tier 4 custom
methods — organization-defined verbs accepted at the transport layer
without IANA registration, provided they conform to AGIS grammar rules
(imperative base-form, action-intent semantic class). This resolves the
fundamental tension between a fixed method registry and the diversity
of real-world agent deployments.

```
Method-Grammar: AGIS/1.0
```

Requests carrying this header are validated against AGIS grammar
rules at the transport layer. Non-conformant methods return
`422 Unprocessable` with `error.code='grammar-violation'`.

### Dynamic Endpoint Negotiation (PROPOSE)

The new PROPOSE method enables agents to request endpoints that have
never been pre-built. A service declares what data it holds in a
Data Manifest block; an agent proposes the endpoint format it needs;
the service instantiates it for the session.

```
Step 1: Agent arrives → reads AGIS document + data manifest
Step 2: Agent sends PROPOSE with desired endpoint definition
Step 3: Service negotiates authorization (262) or accepts (263)
Step 4: Agent calls the newly instantiated endpoint
```

New status codes: `261 Negotiation In Progress`, `262 Authorization
Required for Negotiation`, `263 Endpoint Instantiated`,
and PROPOSE refusals delivered as `422 Unprocessable` with
`error.code='negotiation-refused'` (counter-proposals carry a
`counter_proposal` body on the same status).

### AGIS-Version Header

Services that update their AGIS documents at runtime signal changes
via the `AGIS-Version` response header. Agent runtimes detect version
changes and re-fetch the AGIS document automatically, enabling
real-time service adaptation without push notifications.

---

## Agent Identity and Registration

### Agent Birth Certificate

Every AGTP agent is issued an Agent Birth Certificate at registration
time — a cryptographically signed identity document that establishes
the agent's identity, owner, authorized scope, behavioral archetype,
and governance zone before the agent takes any action. The Birth
Certificate is the genesis record of the agent's existence. Its
`certificate_hash` field is the basis for the agent's canonical
256-bit Agent-ID. Authority is issued through the Birth Certificate;
it is never self-assumed.

Birth Certificate fields map directly to AGTP protocol headers on
every request: `agent_id` → `Agent-ID`; `owner` → `Principal-ID`;
`scope` → `Authority-Scope`.

### URI Structure

AGTP URIs are addresses, not filenames. The canonical forms are:

```
agtp://[256-bit-canonical-id]
agtp://[domain.tld]/agents/[agent-label]
agtp://agtp.[domain.tld]/agents/[agent-label]
```

Resolving an agent URI returns a signed **Agent Manifest Document**
(`application/agtp+json`) derived from the agent's package. The
manifest exposes identity, lifecycle state, trust tier, behavioral
scope, and birth certificate fields. It never exposes executable
content. File extensions (`.agent`, `.nomo`, `.agtp`) must not
appear in canonical URIs.

When the service is AGIS-conformant, resolving the root AGTP address
also returns the AGIS document (`application/agis`) describing all
available endpoints. A lightweight service summary is available at
`/.well-known/agis.json` without credentials.

### Deployment Package Formats

| Format | Type | Description |
|---|---|---|
| `.agent` | Open (patent pending) | Manifest + integrity hash + behavioral trust score |
| `.nomo` | Governed (patent pending) | `.agent` + CA-signed cert chain + governance zone binding |
| `.agtp` | Protocol-native (this spec) | Wire-level manifest document returned by URI resolution |

The name `.nomo` derives from the Greek *nomos* (νόμος), meaning
law or governance — an agent operating under cryptographically
enforced behavioral constraints.

### Trust Tiers

| Tier | Verification | Package |
|---|---|---|
| 1 — Verified | DNS ownership challenge (RFC 8555) | `.nomo` required |
| 2 — Org-Asserted | None | `.agent` or `.nomo` |
| 3 — Experimental | None | Any; X- prefix required; not production-eligible |

---

## Tooling

Python packages for working with AGTP and AGIS are available on PyPI:

| Package | Description |
|---|---|
| `agis-sdk` | Core SDK — parse, validate, generate AGIS documents |
| `agis-validator` | 8-pass AGIS document linter and CLI |
| `agis-mcp` | Auto-generate MCP tools/list from AGIS documents |
| `agtp-client` | AGTP protocol client library |
| `agis-cli` | Command-line tools for AGIS authoring and validation |

```bash
pip install agis-validator
agis validate myservice.agis
```

---

## Intellectual Property

The **core AGTP specification** — all base methods, header fields,
status codes, and IANA registrations defined in this document — is
open and royalty-free.

Certain **extensions and mechanisms** referenced in the specification
may be subject to pending patent applications by the author,
specifically:

- The **Agent Certificate extension** (`draft-hood-agtp-agent-cert-00`)
- The **ACTIVATE method**
- The **Agent Birth Certificate mechanism**
- The **`.agent` file format specification**
- The **`.nomo` file format specification**

The licensor is prepared to grant a royalty-free license to
implementers for any patent claims covering these extensions,
consistent with the IETF's IPR framework under RFC 8179.

IPR disclosures are filed with the IETF Secretariat:
https://datatracker.ietf.org/ipr/

---

## Rebuilding the I-D

Edit `draft-hood-independent-agtp-03.md` and rebuild:

```bash
# Install toolchain (once)
pip install xml2rfc
gem install kramdown-rfc

# Rebuild all formats
kdrfc draft-hood-independent-agtp-03.md

# Or step by step
kramdown-rfc draft-hood-independent-agtp-03.md > draft-hood-independent-agtp-03.xml
xml2rfc draft-hood-independent-agtp-03.xml --text
xml2rfc draft-hood-independent-agtp-03.xml --html
```

The same toolchain applies to companion specs. The IETF Author Tools
service at https://author-tools.ietf.org/ can also convert `.md`
to `.xml`, `.txt`, and `.html` without a local install.

---

## Feedback and Contribution

This specification is in active development and pre-IETF working
group stage. All feedback is welcome:

- **Issues** — open a GitHub issue for questions, corrections, or
  gaps in the specification
- **Pull requests** — editorial improvements and clarifications to
  the spec text
- **Implementation reports** — if you are building an AGTP prototype,
  please share your findings via an issue; implementation reports
  will be incorporated into subsequent draft revisions
- **IETF discussion** — once submitted, discussion will move to the
  IETF DISPATCH mailing list (dispatch@ietf.org)

---

## Author

**Chris Hood** — AI Strategist, Author, Founder of Nomotic AI

- [chrishood.com](https://chrishood.com)
- [nomotic.ai](https://nomotic.ai)
- [linkedin.com/in/chrishood](https://linkedin.com/in/chrishood)

---

## License

The specification text in this repository is licensed under
[Creative Commons Attribution 4.0 International (CC-BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

You are free to share and adapt the material for any purpose,
provided appropriate credit is given, a link to the license is
provided, and any changes are indicated.

This license applies to the **specification text**. It does not
grant rights to any pending patent claims on extensions described
in the specification. See the Intellectual Property section above.
