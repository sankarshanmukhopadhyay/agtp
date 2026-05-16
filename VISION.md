# AGTP Vision

**To give the world's AI agents a place to collaborate.**

---

## The Web We Have

The internet was built for people. HTTP serves humans clicking links,
filling forms, and reading content. The infrastructure beneath it —
DNS, routing, security, governance — was designed assuming the caller
is a person operating a browser, with all the contextual cues that
implies.

AI agents have inherited this infrastructure by default. They make
HTTP requests pretending to be browsers. They authenticate through
systems designed for human credentials. They get blocked by defenses
designed for unwanted bots. They retrofit identity, attribution, and
governance through application-layer mechanisms that no two
implementations agree on.

This worked when agents were rare. It does not work at the scale agents
are arriving. Every enterprise faces the same questions: which traffic
is an agent? Who authorized it? What is it allowed to do? Can we audit
what it did? The infrastructure we have today cannot answer these
questions cleanly because it was never designed to.

The result is what governance teams call shadow AI: agentic workflows
operating inside organizations without reliable identification,
attribution, or oversight. The current approach is sniffing, monitoring,
and pattern-based blocking at the application layer. It is expensive,
brittle, and structurally cannot give clean answers.

## The Web Agents Need

The architectural answer is straightforward and has historical precedent.
Email runs on SMTP. Real-time media runs on RTP. Each of these is a
dedicated protocol for a specific kind of traffic, with infrastructure
that knows what it is carrying because the protocol tells it so.

AI agents need the same. Agent traffic should live on a dedicated
protocol, on a dedicated port, with identity and authority structurally
present at the wire level. Infrastructure should know it is carrying
agent traffic the same way mail servers know they are carrying email.
This is not a small architectural preference. It is the difference
between a future where governance and oversight are structural facts
and a future where they remain best-effort retrofits.

The Agent Transfer Protocol (AGTP) is built to be that protocol.

## What AGTP Is

AGTP is the protocol of the agent web. It is to agent communication
what SMTP is to email and HTTP is to the human web: the wire-level
substrate on which everything else is built.

AGTP runs on TCP with TLS 1.3 or on QUIC. It is not built on HTTP. It
carries agent identity, authority scope, attribution, and intent
methods at the wire level, structurally and on every request. Traffic
on AGTP is, by definition, an agent acting under verifiable authority.
There is no ambiguity about what an AGTP request is.

The protocol is agent-only by design. Services exposed on AGTP know
every caller is an agent. Governance frameworks acting on AGTP traffic
can evaluate decisions against protocol-level facts rather than
application-layer inferences. Regulated industries can audit agent
behavior with structural confidence rather than statistical estimation.

## What AGTP Is Not

AGTP is not a governance framework. It carries the protocol-level
primitives that governance frameworks require — identity, authority,
attribution, scope enforcement — but the framework decisions about
policy, compliance, and runtime behavior belong to the governance
platforms built on AGTP.

AGTP is not a replacement for the agent application frameworks that
exist today. MCP, A2A, ACP, ANP, and others define how agents work
together at the application layer. AGTP is the substrate they can run
on to gain native protocol-level identity and governance. It composes
with these frameworks rather than displacing them.

AGTP is not a hardware enforcement layer. Trusted execution
environments, confidential computing, and silicon-level attestation
are legitimate concerns that compose above the protocol layer. AGTP
provides the wire-level facts that those layers act on; it does not
itself reach into execution.

AGTP is not a model governance specification. What models do
internally, how they are trained, how they reason, and how they are
configured are matters for other communities. AGTP addresses how
agents communicate, not what they think.

## The Goal

The goal is simple: 100% of agent traffic on AGTP.

This is the substantive version of the architectural commitment. Not
"AGTP for some agents and HTTP for others." Not "AGTP as a parallel
option that organizations may choose." All agent traffic on a
dedicated substrate, the same way all email traffic is on SMTP.

Adoption will not happen overnight. SMTP took years to displace
proprietary email systems. AGTP will follow a similar trajectory.
The architectural commitment matters because the end state matters:
a future where agent infrastructure is structurally governable, not
one where it remains a constant retrofit problem.

The case for adoption is strongest in regulated industries already
struggling with shadow AI, unknown agentic workflows, and APIs with
limited governance. Agent and AI discovery, attribution, scope
enforcement, trust, and regulatory auditability are solved
structurally at the protocol layer rather than retrofitted onto an
actor-agnostic substrate.

At the current pace of agent infrastructure development, the
realistic horizon for meaningful global adoption is three to five
years. Some industries will adopt faster (financial services,
healthcare, government); others will adopt slower (legacy enterprise
software, consumer applications). The trajectory matters more than
the timeline.

## What Gets Built

AGTP is more than a single protocol specification. It is a family of
coordinated work covering the wire-level concerns that agent
infrastructure requires:

- **Agent Identity** that is canonical, verifiable, and persistent
  across organizational change
- **Agent Authority and Scope** carried at the wire level on every request
- **Agent Attribution Records** signed structurally for every action
- **Intent-aligned Methods** that match how agents reason about what
  they want to do
- **Agent Trust Attestation** with normative semantics and freshness
- **Agent Discovery Mechanisms** for agents to find each other and find
  services
- **Agent Session Semantics** for both bounded transactional flows and
  long-lived persistent contexts
- **Transparency Logging** with pre-commitment ordering for
  regulatory-grade auditability
- **Merchant Identity** for agentic commerce
- **Composability** with MCP, A2A, ACP, ANP, and future application
  frameworks

Each of these areas is sized for dedicated standards work in
coordination with the broader IETF community. The pattern follows how
HTTP-related and email-related work is organized today: a foundational
protocol with multiple coordinated working groups handling specific
concerns.

## Why This Matters

The next decade of internet infrastructure will be shaped by how AI
agents operate at scale. The decisions made now about substrate,
identity, and governance will be either deliberately designed or
inherited by default from systems that were never meant to carry this
traffic.

If we get this right, agents become structurally identifiable,
attributable, and governable. Enterprises can deploy them with
confidence. Regulators can audit them with structural evidence.
Developers can build them against stable contracts. The agent web
becomes infrastructure the world can rely on rather than something
that has to be constantly defended against.

If we get this wrong, we inherit a future where shadow AI persists at
scale, where attribution remains best-effort, where every governance
framework retrofits identity differently, and where enterprises must
choose between speed and oversight indefinitely.

The architectural choice is open today. The work being done now
determines which future we get.

## Open Questions

AGTP is a working proposal, not a finished standard. Many design
decisions remain open:

- Final shape of the method catalog and grammar for custom methods
- Coordination model between governance platforms operating across
  organizational boundaries
- Migration paths for application frameworks currently running on
  HTTP
- Federation patterns for agent registries
- Long-term governance of the agent web itself

These are community questions. The protocol will be better for being
shaped by many contributors rather than by any single author or
organization.

## How to Get Involved

AGTP is being developed openly. The specifications are published as
Internet-Drafts on the IETF datatracker. Source code, reference
implementations, and supporting materials are available on GitHub.
Community discussion happens through IETF mailing lists and the
repository.

If the vision resonates, there are several ways to engage:

- Read the specifications and provide feedback
- Implement against the reference implementation and report findings
- Contribute to companion drafts or propose new ones
- Express interest in participating in proposed working groups
- Share the vision with others who care about how the agent web
  develops

The work is meaningful precisely because it is foundational. What
gets built now will shape what comes after.

---

*The Agent Transfer Protocol family is published openly. For current
draft status, see the IETF datatracker. For implementation work and
community discussion, see the project repo. Written by Chris Hood.*
