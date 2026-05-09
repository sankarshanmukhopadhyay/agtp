# Agent Transfer Protocol (AGTP)

## Project Charter

This document describes the goals, scope, and design principles of the
Agent Transfer Protocol (AGTP). It is intended for newcomers, prospective
contributors, and reviewers seeking to understand the work before engaging
with the specifications.

This is a project-level charter. AGTP is currently maintained as a family
of individual Internet-Drafts. Future trajectory toward IETF working group
consideration is possible but not committed.

## The Problem

APIs were designed for human-driven applications calling them through
software written by humans. The HTTP verbs that carry those calls — GET,
POST, PUT, DELETE — describe operations on resources at a low level.
Application code translates user intent into the right combination of
verbs and paths.

When an AI agent calls an API, the translation breaks down. Agents
reason about intent, not about resources. An agent does not naturally
think "POST to /bookings with this payload." It thinks "book this
flight." The semantic gap between agent intent and HTTP verbs has to be
bridged somewhere, and today it is bridged inconsistently inside every
agent framework, every tool integration, every prompt template.

Empirical research into this gap shows that semantically rich, intent-
aligned method names — verbs like BOOK, QUERY, SUMMARIZE, DELEGATE —
produce higher endpoint selection accuracy when consumed by LLM-based
agents than conventional CRUD verbs do. The performance advantage is
statistically significant at frontier model scale. Method names that
match agent intent reduce the translation burden that current
infrastructure pushes onto application code and prompt engineering.

Getting new methods accepted into HTTP is a slow process, contested by
the broader HTTP community whose use cases are not agent-shaped. Even
if new methods were standardized into HTTP, they would still run on a
protocol that cannot tell whether its caller is a human, a system, or
an agent. HTTP is actor-agnostic by design, and necessarily so for its
intended audience.

The architectural answer is a dedicated protocol that ships intent-
aligned methods natively and serves agents specifically.

## What AGTP Is

AGTP is the protocol of the agent web.

HTTP is the protocol of the human web. It serves people interacting
with websites, web applications, and APIs designed for human-initiated
requests. It is actor-agnostic and necessarily so, because most of its
traffic is not agent traffic and does not need agent semantics.

AGTP is the parallel protocol for agents. It serves agents calling
APIs designed for agent consumption, and agents communicating with
each other. Where HTTP is actor-agnostic, AGTP is agent-only by
definition. AGTP requires agent credentials at the wire level. Traffic
on AGTP is, structurally, an agent acting under verifiable authority.
There is no ambiguity about what a request on AGTP is.

This separation is the architectural foundation everything else in
AGTP builds on. Because the protocol knows its traffic is agent
traffic, identity, authority, and attribution can be wire-level facts
rather than application-layer assertions. Every service exposed on
AGTP knows every caller is an agent. Every governance decision can act
on protocol-level identity without first having to determine whether
the caller has identity at all.

AGTP runs over TCP with TLS 1.3 or over QUIC. It is not built on HTTP.
APIs designed for agent consumption live on AGTP rather than on HTTP,
exposed with intent-aligned semantic methods rather than HTTP verbs
adapted to agent use.

## What AGTP Carries

AGTP specifies the wire-level mechanics of agent communication and
agent-API interaction:

- Intent methods, including QUERY, BOOK, DELEGATE, SUMMARIZE, and
  ESCALATE, with extensibility for domain-specific methods
- Agent identity, including the canonical Agent-ID and the Agent
  Genesis origin record from which it derives
- Authority-Scope, the binding-layer vocabulary by which agents declare
  what actions they are permitted to take
- A three-level verification model spanning self-asserted, application-
  layer, and transport-layer cryptographic verification
- Three equivalent verification paths for Trust Tier 1: DNS-anchored,
  log-anchored via transparency log, and hybrid
- Status codes for governance failures, including 455 Scope Violation
  and 551 Authority Chain Broken
- Attribution Records signed on every method invocation
- Delegation Chain semantics with strict-subset scope enforcement
- Trust Tiers expressing verification depth
- Transport binding to TCP/TLS 1.3 and QUIC

The API layer is specified by a companion document, the Agentic
Grammar and Interface Specification (AGIS), which defines how agent-
native APIs are described and exposed on AGTP. AGTP and AGIS are
tightly coupled: AGTP is the protocol, AGIS is the language for
defining APIs that live on it.

Other companion specifications cover the X.509 Agent Certificate
extension for transport-layer verification, the transparency log
protocol aligned with RFC 9162 and RFC 9943 (SCITT), agent discovery
and naming via the Agent Name Service, Web3 identity bridging,
merchant identity for agentic commerce, and standard intent methods
for common operations.

## Scope Boundaries

AGTP does not specify policy. It specifies the protocol over which
policy is carried and enforced. Governance platforms decide what scope
to grant and to whom; AGTP carries those decisions on the wire and
enforces them at the protocol level.

AGTP is not a governance framework. It provides the protocol-level
primitives that governance frameworks require — identity, authority
declaration, scope enforcement, attribution. What a governance
framework does with those primitives is outside AGTP's specification.

AGTP is not a messaging framework. It does not define queues, topics,
brokers, persistence semantics, or pub/sub patterns. It is a protocol
for agent-initiated requests and the responses to them, carrying the
governance metadata those requests need.

AGTP is not an agent orchestration or workflow framework. Multi-step
reasoning, tool composition, and agent collaboration patterns belong
to application-layer frameworks (MCP, A2A, ACP, ANP, others) that
compose on top of AGTP.

AGTP is not a capability specification. What an agent can functionally
do is a matter for application-layer protocols. AGTP specifies how that
capability is identified, authorized, and attributed when invoked.

AGTP does not replace TLS, QUIC, or other underlying transports. It
runs on these substrates and contributes nothing to their
specification.

## Design Principles

*Intent-aligned methods.* Methods are named for what the agent is
trying to accomplish, not for which CRUD operation maps closest. BOOK
means book, QUERY means query, SUMMARIZE means summarize. The semantic
gap between agent intent and protocol verb closes at the protocol
level rather than in application code.

*Agent-only by protocol.* AGTP traffic is agent traffic. The protocol
does not need to determine the actor; the actor is structural.

*Agent-first identity.* The canonical Agent-ID is the authoritative
identifier in every protocol operation. All other identification forms
resolve to a canonical Agent-ID. Identity is permanent and stable
across organizational change, domain transfers, and resolution-path
changes.

*Wire-level governance primitives.* Identity, authority, and
attribution are protocol-layer concerns. They are present on every
request, not retrofitted by application logic. Governance frameworks
consume these primitives to make and enforce policy decisions.

*Composability with adjacent work.* AGTP is designed to serve as
substrate for agent application frameworks. MCP, A2A, ACP, and similar
frameworks gain protocol-level identity and governance support by
running on AGTP rather than retrofitting these onto HTTP.

*Three verification levels, three verification paths.* Implementations
choose verification depth and verification path appropriate to their
deployment profile. No single approach is privileged.

*Scope-bound, not capability-bound.* Authority-Scope declares what an
agent is permitted to do, not what it is functionally capable of
doing. Capability discovery is application-layer; permission
enforcement is protocol-layer.

*Attribution by default.* Every method invocation produces a signed
attribution record. There is no protocol mode in which agent actions
are unattributable.

## Empirical Foundation

The architectural commitment to intent-aligned methods is grounded in
benchmarking research conducted using the Agentic API Test Lab. The
research compared LLM-based agent endpoint selection accuracy across
four conditions: pure CRUD/REST endpoints, pure agentic-named
endpoints, mixed-paradigm endpoints, and description-mismatch ablations.

Findings, confirmed across multiple frontier-class models:

- Agentic naming produces a substantial accuracy advantage in mixed-
  paradigm conditions, statistically significant at conventional
  thresholds.
- The effect is independent of documentation quality. Description-swap
  ablations show CRUD endpoints collapse under documentation
  mismatch while agentic endpoints remain resilient. The method name
  itself carries the intent signal.
- The effect appears to be capability-threshold dependent: it is
  absent at small model scales (around 3 billion parameters) and
  present at frontier scale.

These findings inform the AGTP design choice to ship intent-aligned
methods natively rather than retrofitting them into HTTP or treating
them as application-layer conventions.

## Relationship to Adjacent Work

AGTP coexists with several related efforts. The relationships are
architectural rather than competitive.

*Agent application frameworks (MCP, A2A, ACP, ANP):* These define
agent capabilities, tool invocation patterns, and orchestration
semantics. They currently run on HTTP and inherit HTTP's actor-
agnosticism along with the verb-translation burden. Run on AGTP, they
gain protocol-level identity, governance support, and intent-aligned
methods natively. AGTP does not compete with these frameworks at the
application layer; it provides them with a substrate designed for
agent traffic.

*Transport substrates (HTTP, QUIC, MOQT, WebTransport):* AGTP runs on
TCP/TLS or QUIC. Other agent-related protocols run on HTTP or extend
MOQT. The architectural commitments differ. AGTP commits to a
dedicated agent protocol with native semantic methods; HTTP-based
approaches commit to layering agent semantics atop general-purpose
transport. Both will exist.

*Identity and authentication frameworks (WIMSE, OAuth, SPIFFE):*
These provide proven mechanisms for workload and service identity.
AGTP's identity model is agent-native rather than workload-native, and
the two serve different scopes.

*Transparency standards (RFC 9162 Certificate Transparency 2.0, RFC
9943 SCITT):* AGTP's log-anchored verification path interoperates with
deployed SCITT infrastructure rather than inventing a parallel format.

*Zero-trust frameworks (CSA ZTCPP, ONUG AOMC):* AGTP provides
protocol-level primitives that zero-trust governance can build on.
Cross-implementation policy coordination is naturally complementary
work.

## Status

AGTP is a family of individual Internet-Drafts. The base specification
is draft-hood-independent-agtp, currently at version 05. Companion
drafts cover the AGIS interface description language, the certificate
extension, transparency log protocol, discovery and naming, Web3
bridge, merchant identity, agent composition, and standard methods.

Source documents and specification discussion live in this repository.
Drafts are submitted to the IETF datatracker. Implementation work is
underway at Nomotic, with reference demonstration artifacts planned
for 2026.

The project is currently maintained by Chris Hood (chris@nomotic.ai).
Contributors are welcome and credited; see Contributing below.

## How to Contribute

Three forms of contribution are valuable:

*Issues.* Open a GitHub issue for specification questions, perceived
inconsistencies, threat model gaps, missing edge cases, or areas where
the drafts are unclear. Issues are the lightest-weight contribution
and the most useful for sharpening the specifications.

*Pull requests.* For text edits, schema corrections, or example
contributions, open a pull request against the relevant draft.
Significant architectural changes should be discussed in an issue
first.

*Implementation experience.* If you implement AGTP, partially or
fully, report what you found. Implementation feedback is the most
valuable input a protocol specification receives. Both the parts that
worked and the parts that didn't are useful.

For substantive contributions or proposals to add companion drafts,
contact chris@nomotic.ai directly.

## Governance and IPR

AGTP is published under standard IETF Internet-Draft terms. Patent
claims arising from the specifications are disclosed per BCP 79.
Implementers are advised that royalty-free licensing terms are
intended for any patents covering AGTP mechanisms; specific
commitments are documented in the IPR notices of individual drafts.

Specification-level discussion happens on this GitHub repository.
IETF list discussion happens on the lists where drafts are introduced
(currently agent2agent for charter-level conversation, WIMSE for
identity and authentication topics).

## Trajectory

The near-term trajectory is to mature the specification family
through implementation feedback and community review. Multiple
standardization venues remain viable depending on community
engagement.

The architectural commitment of AGTP is independent of its
standardization venue. The protocol is designed to be useful
regardless of whether it becomes a chartered IETF work item.

## Contact

Chris Hood, Nomotic, Inc.
chris@nomotic.ai

Drafts: https://datatracker.ietf.org/doc/draft-hood-independent-agtp/
Repository: this GitHub repository
