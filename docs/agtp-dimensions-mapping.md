# AGTP Mapping to draft-foroughi-agent-protocol-dimensions

**Version:** 0.1 (July 2026)
**Status:** Working draft; community input welcome
**Primary reference:** [draft-foroughi-agent-protocol-dimensions-00](https://datatracker.ietf.org/doc/draft-foroughi-agent-protocol-dimensions/)

## 1. Purpose

This document maps the Agent Transfer Protocol (AGTP) family of specifications to the dimensional model in draft-foroughi-agent-protocol-dimensions-00. The mapping is offered as an input to a future revision of the substrate-bindings table (Table 2 of the referenced draft) and as a per-extension coverage view, in response to community suggestion that coverage be graded per extension rather than per protocol.

AGTP is a substrate proposal. It sits below the agent protocol layer in the referenced draft's Section 3 layered view. It provides substrate facets (transport, identity, discovery, audit, security) that agent protocols such as A2A, MCP, and ACP can bind to rather than reimplement.

Throughout this document, references to "the referenced draft" mean draft-foroughi-agent-protocol-dimensions-00. See Section 8 for full citations.

## 2. Scope of the Mapping

The mapping addresses three questions:

1. **Substrate bindings for Table 2:** how AGTP-composed variants of the representative agent protocols (A2A-over-AGTP, MCP-over-AGTP, ACP-over-AGTP) would appear in the substrate-bindings table.
2. **Per-extension coverage:** which AGTP family document provides which substrate facet, dimensional value, or extension primitive from the referenced draft's model.
3. **Open questions from Section 12 of the referenced draft:** how AGTP addresses each.

Cells use the following labels: *Specified* (defined in a published draft), *Specified, Implemented* (defined and running in reference implementation), *In work* (drafted with substantive text; refinement continuing), *Architectural assumption* (deliberate design position rather than an implemented mechanism), *Inherited* (provided by another AGTP extension), *n/a* (outside AGTP's scope by design).

## 3. AGTP as Substrate: Table 2 Additions

The following three rows are candidate additions to the substrate-bindings table. Each row characterizes a specific composition of an agent protocol over AGTP; each is architecturally symmetrical with the corresponding baseline row already in the referenced draft.

| Proposal | Transport substrate | Identity substrate | Discovery substrate |
| -------- | ------------------- | ------------------ | ------------------- |
| A2A over AGTP | AGTP core protocol on port 4480 with TCP/TLS or QUIC bindings (draft-hood-independent-agtp, draft-hood-agtp-bindings) | AGTP Canonical Agent-ID with X.509 v3 Agent Certificates (draft-hood-agtp-agent-cert); Authority-Scope and Delegation-Chain carried at the wire | AGTP Agent Naming System and Presence overlay (draft-hood-agtp-discovery, draft-hood-agtp-presence) |
| MCP over AGTP | Same as above; MCP-over-AGTP is running as a working reference implementation (mcp.nomotic.ai) via draft-hood-agtp-composition | Same as above; MCP tool identity carried through draft-hood-agtp-agent-cert | Same as above |
| ACP (AGNTCY) over AGTP | Same as above; ACP invocation surface carried via draft-hood-agtp-composition | Same as above; AGNTCY Identity carriage composable alongside AGTP identity per draft-hood-agtp-composition External IdP profile | Same as above; Agent Directory Service composable via draft-hood-agtp-discovery |

Notes on the three rows:

- All three substrate columns are provided by the same AGTP family, so the substrate columns converge under AGTP composition. The referenced draft's Section 6 observation that convergence in a substrate column indicates a binding question rather than a primary protocol-design question applies directly.
- The composition profile itself is specified in draft-hood-agtp-composition. This document defines the AGMP composition family for MCP, A2A, and ACP carrying their messages on AGTP without modification, alongside External Identity Provider composition and HTTP Gateway composition.
- MCP-over-AGTP is running today. Reference implementation at github.com/nomoticai/agtp with a working deployment at mcp.nomotic.ai. The A2A and ACP composition rows are specified with reference implementations in work.

## 4. Per-Extension Substrate Facet Coverage

The following table maps each AGTP family document to the substrate facets identified in Section 4 of the referenced draft. This view answers the question "which document in the AGTP family provides which substrate behaviour."

| Substrate facet | AGTP extension(s) | Status |
| --------------- | ----------------- | ------ |
| Transport | draft-hood-independent-agtp (core wire protocol); draft-hood-agtp-bindings (TCP/TLS and QUIC bindings with replay-safety profile) | Specified, Implemented |
| Identity carriage | draft-hood-independent-agtp (Canonical Agent-ID, Owner-ID, Agent Identity Document, Agent Genesis); draft-hood-agtp-agent-cert (X.509 v3 extensions binding Canonical Agent-ID and Authority-Scope) | Specified, Implemented |
| Discovery | draft-hood-agtp-discovery (Agent Naming System, DISCOVER method); draft-hood-agtp-presence (Kademlia DHT plus gossip ambient discovery, partitioned across capability, industry, region, owner-domain, trust tier) | Specified, In work |
| Audit | draft-hood-agtp-identifiers (ten-identifier tamper-evident model with per-agent hash chain); draft-hood-agtp-log (append-only transparency log aligned with RFC 9162 and RFC 9943) | Specified, In work |
| Security channel | draft-hood-agtp-bindings (TLS 1.3 with mTLS); draft-hood-agtp-agent-cert (mutual TLS binding to Canonical Agent-ID; O(1) scope enforcement at Scope Enforcement Points) | Specified, Implemented |
| Trust and verification | draft-hood-agtp-trust (three trust tiers, four verification paths, trust_score field with normative range and freshness) | Specified, In work |
| Authorization derivation | draft-hood-independent-agtp (Delegation-Chain header, Authority-Scope narrowing); draft-hood-agtp-agent-cert (attenuated authority in certificate lifecycle) | Specified, Implemented (Delegation-Chain pre-authorization semantics targeted for the v10 draft revision) |
| Session semantics | draft-hood-agtp-session (bounded and persistent sessions inheriting identity, authority, and attribution) | Specified |
| Real-time communication | draft-hood-agtp-communication (multi-modal voice, video, and structured streams on the agent-native substrate) | Specified |
| Content addressability | draft-hood-independent-agtp (Attribution-Record referenceable via Audit-ID); draft-hood-agtp-identifiers (extended identifier chain including Response-ID, Action-ID) | Specified, In work |
| Commerce and merchant identity | draft-hood-agtp-commerce (pricing manifests, budget signaling, transaction commitments); draft-hood-agtp-merchant-identity (dual-party attribution for PURCHASE); draft-hood-agtp-lei (GLEIF vLEI binding for regulated entities) | Specified, In work |
| Composition profiles | draft-hood-agtp-composition (AGMP profiles for MCP, A2A, ACP; External IdP composition; HTTP Gateway composition) | Specified, Implemented (MCP profile running today) |
| Web3 bridge | draft-hood-agtp-web3-bridge (hybrid verification bridging PKI trust model with Web3 wallet-based identity) | Specified |

## 5. Per-Extension Dimensional Coverage

Following the community suggestion to grade per extension rather than per protocol, the following table maps AGTP family documents against the seven dimensions and six extensions defined in the referenced draft. A cell notes what a given AGTP document enables or provides for that dimension; blank cells indicate the document is silent on that axis.

Dimensions D1 through D7 are properties of an agent protocol exchange. When an agent protocol is composed over AGTP, several dimensional values become achievable that are absent or unspecified in the baseline (unbound) agent protocol row. The per-extension view makes explicit which AGTP document is responsible for each achieved value.

### 5.1 Dimensions D1 through D7

| AGTP Extension | D1 Origination | D2 Cadence | D3 Lifecycle | D4 Authz Derivation | D5 Endpoint Binding | D6 State Locality | D7 Result Addressability |
| -------------- | -------------- | ---------- | ------------ | ------------------- | ------------------- | ----------------- | ------------------------ |
| draft-hood-independent-agtp | supports within-association (COLLABORATE and semantic methods carry callee-originated messages within an association) | supports streamed (per-method output cadence, streaming default for COLLABORATE) | inherits from draft-hood-agtp-session | supports direct and derived-1hop (Delegation-Chain header, Authority-Scope narrowing per hop) | inherits from draft-hood-agtp-discovery | supports callee-held (Session-ID, Task-ID identifiers) | supports referenceable-artifact via Audit-ID chain |
| draft-hood-agtp-api | supports within-association (PROPOSE and synthesis protocol) | supports atomic and streamed (per-method cadence) | inherits from draft-hood-agtp-session | inherits | inherits | supports callee-held | supports referenceable-artifact |
| draft-hood-agtp-bindings | enables within-association at the transport layer (QUIC streams; TLS 1.3 with early-data safety profile) | enables streamed (QUIC stream multiplexing) | | | | | |
| draft-hood-agtp-session | supports within-association | supports streamed | supports suspend-resume and durable-reattach (bounded sessions and persistent sessions; Session-ID reattachment) | inherits | | supports callee-held (Session-ID) | inherits |
| draft-hood-agtp-discovery | | | | | supports pre-bound and registry-resolved (Agent Naming System; DISCOVER method) | | |
| draft-hood-agtp-presence | | | | | supports registry-resolved (DHT-based ambient discovery) | | |
| draft-hood-agtp-agent-cert | | | | supports direct, derived-1hop, and derived-chain (X.509 v3 extensions binding Canonical Agent-ID and Authority-Scope; attenuated authority via certificate chain) | | | |
| draft-hood-agtp-identifiers | | | | augments derivation with cryptographic parent reference (hash-chained Attribution-Records) | | supports callee-held (Task-ID, Action-ID, Decision-ID) | supports referenceable-artifact (Audit-ID as content-addressable identifier) |
| draft-hood-agtp-trust | | | | augments (verifiable trust_score usable as input to authorization decisions at receiver) | augments (trust-tier filter on endpoint selection during discovery) | | |
| draft-hood-agtp-log | | | | | | | supports referenceable-artifact (transparency log entries and receipts) |
| draft-hood-agtp-communication | supports within-association (bidirectional multi-modal streams) | supports streamed natively | supports durable-reattach for persistent media sessions | inherits | inherits | supports callee-held | inherits |
| draft-hood-agtp-composition | inherits from composed protocol | inherits | inherits | inherits AGTP delegation-chain when composed | inherits AGTP discovery when composed | inherits | inherits |
| draft-hood-agtp-commerce | supports within-association (transaction dialogs) | supports atomic and streamed | supports suspend-resume (approval-gated purchase) | supports direct and derived (merchant authorization) | supports pre-bound and registry-resolved | supports callee-held (transaction session) | supports referenceable-artifact (transaction receipts) |
| draft-hood-agtp-lei | | | | augments derivation with GLEIF vLEI Legal Entity credentials for regulated entities | | | |
| draft-hood-agtp-merchant-identity | | | | supports derived (merchant credentials in dual-party attribution) | | | supports referenceable-artifact (dual-party attribution records) |
| draft-hood-agtp-web3-bridge | | | | augments derivation with Web3 wallet identity for hybrid verification | | | |

### 5.2 Extensions EXT-CHKPT, EXT-REATTACH, EXT-CAPREG, EXT-AUDIT, EXT-XLATE, EXT-MODNEG

| Extension | AGTP coverage |
| --------- | ------------- |
| EXT-CHKPT (authorization checkpoint) | draft-hood-independent-agtp lifecycle methods; draft-hood-agtp-session suspend-resume semantics |
| EXT-REATTACH (persistent task identifier) | draft-hood-agtp-session persistent sessions with Session-ID reattachment |
| EXT-CAPREG (capability registration) | draft-hood-agtp-discovery Agent Naming System registration; draft-hood-agtp-presence DHT publication with capability filters |
| EXT-AUDIT (structured audit record exchange) | draft-hood-agtp-identifiers Attribution-Record format; draft-hood-agtp-log transparency log format and receipt exchange |
| EXT-XLATE (error vocabulary for translation and validation) | draft-hood-independent-agtp status code vocabulary distinct from authorization failure codes |
| EXT-MODNEG (modality negotiation) | draft-hood-agtp-communication multi-modal negotiation at session setup |

## 6. Responses to Section 12 Open Questions

Section 12 of the referenced draft records six open questions surfaced by the model. The following notes the AGTP position on each. Where AGTP has a specified answer, the answer is stated. Where AGTP treats the question as broader scope.

### 6.1 Mediator behaviour on D4

The referenced draft asks whether a mediator receiving an inbound request and issuing an outbound request preserves the originator's authorization chain to the downstream target, or terminates the chain at the mediator.

AGTP position: preserve. The Delegation-Chain header in draft-hood-independent-agtp carries each hop as an appended cryptographically signed record with progressive Authority-Scope narrowing and cryptographic parent reference. A receiving agent evaluates lineage independently of trusting any intermediate. A mediator that terminates the chain and presents its own credentials is a supported deployment choice but is discouraged in AGTP where the mediator's own identity would then appear as the originating party in the receiver's attribution record. The two behaviours are distinguishable at the receiver via the Delegation-Chain header.

Related work in the community: draft-rampalli-pedigree defines similar chain semantics with a pre-authorization model. the AGTP v10 draft revision, in progress, formalizes pre-authorization semantics for Delegation-Chain and cross-references PEDIGREE where the mechanics align.

### 6.2 Realization of long-running interactions on D3

The referenced draft asks whether long-running tasks are realized as durable-reattach with a persistent task identifier at the protocol layer, or as repeated independent exchanges above the protocol layer.

AGTP position: both are supported and the choice is a deployment decision. draft-hood-agtp-session defines persistent sessions with Session-ID reattachment for the protocol-layer realization; the identifier chain in draft-hood-agtp-identifiers provides Task-ID and Response-ID for the application-layer realization. AGTP is silent on which pattern a specific deployment should adopt; the choice depends on session lifetime, transport interruption expectations, and observability requirements.

### 6.3 Baseline and extension boundary

The referenced draft asks which of the extensions in Section 8 warrant cross-proposal standardisation.

AGTP position: EXT-AUDIT and EXT-CAPREG have the strongest cross-proposal case because they surface primitives (audit format, capability registration) that any agent-protocol composition needs and that fragment costly when each protocol defines its own version. AGTP addresses both at the substrate: draft-hood-agtp-identifiers and draft-hood-agtp-log for audit; draft-hood-agtp-discovery and draft-hood-agtp-presence for capability. Cross-proposal standardisation of these at the extension level would produce redundancy with a substrate that carries them.

EXT-CHKPT and EXT-REATTACH are naturally session-scoped and align cleanly with draft-hood-agtp-session. Standardising them at the substrate rather than per-protocol reduces protocol duplication.

EXT-XLATE (error vocabulary) is a status code question; AGTP defines its own status codes distinct from authorization failure at the substrate.

EXT-MODNEG (modality negotiation) is addressed by draft-hood-agtp-communication for real-time multi-modal exchanges.

### 6.4 Substrate profiling scope

The referenced draft asks which substrate bindings are profiled and which are left to implementations.

AGTP position: draft-hood-agtp-bindings profiles TCP/TLS and QUIC as the two candidate transport substrates. draft-hood-agtp-composition profiles composition surfaces for MCP, A2A, ACP over AGTP, alongside External IdP composition (OAuth, OIDC, SPIFFE, enterprise IdPs) and HTTP Gateway composition for translating HTTP traffic into AGTP method invocations. draft-hood-agtp-web3-bridge profiles a hybrid verification path composing PKI with Web3 wallet identity. draft-hood-agtp-lei profiles the GLEIF vLEI substrate for regulated entities.

### 6.5 Candidate dimensions pending evidence

The referenced draft records several candidate dimensions set aside pending evidence (communication cardinality, delegation transitivity, mediation role, modality profile, failure and delivery semantics, trust domain span, callback and reverse-initiation).

AGTP observations:

- Delegation transitivity is addressed by draft-hood-agtp-agent-cert and Delegation-Chain semantics. AGTP's Delegation-Chain records lineage continuity across hops; the pre-authorization work in the AGTP v10 draft revision addresses cumulative behavior across the chain. If future revisions of the dimensional model add a chain-scoped dimension, AGTP has specified mechanisms available for characterization.
- Mediation role is addressed at the substrate through Attribution-Record chain semantics: forwarding, translating, and validating mediators leave distinct traces in the attribution record without requiring a dimension.
- Modality profile is addressed by draft-hood-agtp-communication at the extension level (EXT-MODNEG).

### 6.6 Broader-scope concerns

The referenced draft records that concerns whose scope is a delegation lineage, a cross-crossing behavioral aggregate, or a data-use policy persisting across multiple exchanges sit outside the dimensions of Section 5. These are treated as work belonging to companion effort or a future revision.

AGTP position: several of these are addressed at the AGTP substrate and are candidate content for a companion document.

- **Cumulative behavioral bounds across a delegation chain**: draft-hood-agtp-agent-cert Authority-Scope carries per-hop scope that narrows monotonically; the receiver evaluates cumulative scope by intersecting scopes across the Delegation-Chain. the AGTP v10 draft revision formalizes pre-authorization semantics for the cumulative bound.
- **Source-asserted constraints carried with data across subsequent crossings**: draft-hood-agtp-identifiers Attribution-Records carry source-asserted claims cryptographically bound to the originating Agent-ID. Constraints propagate with the data across crossings via the record chain.
- **Revocation freshness with lineage**: draft-hood-agtp-trust defines freshness on trust_score; draft-hood-agtp-log provides transparency-log receipts with defined freshness bounds. Revocation propagation across a lineage is the seam between these two, and is a candidate area for coordination with SCITT.

## 7. Areas Outside AGTP's Scope by Design

Consistent with the principle applied throughout this mapping, the following areas sit outside AGTP's coverage by design and are named here so that reviewers can evaluate coverage accurately.

### 7.1 Content substrate distinction

AGTP is content-neutral at the substrate: it makes no attempt to inspect payloads, define content types, or constrain the semantic vocabulary a composed agent protocol carries. Beyond this operational neutrality, AGTP is architecturally distinct from a content substrate in the sense HTTP embodies. HTTP was designed to move documents to a human consumer through a user-interface layer, and its content types, caching semantics, and content negotiation primitives serve that purpose. AGTP is designed for agents coordinating with other agents. Coordination messages, delegations, session state, authority scope, and attribution records are the primary payloads; human-consumable rendering is a downstream concern rather than a substrate concern.

Where agent output needs to reach a human, HTTP composed above AGTP is the natural user-interface layer. The relationship is analogous to the way HTTP-based interfaces such as web mail clients sit above SMTP for human interaction with underlying mail transport. draft-hood-agtp-composition already defines an HTTP Gateway composition profile that supports one direction of this pattern (HTTP traffic translated into AGTP method invocations); a symmetric profile carrying AGTP results into an HTTP-rendered surface is a candidate for future work.

Real-time modalities carried by draft-hood-agtp-communication (voice, video, structured streams) are output mechanisms for agent-to-agent or agent-to-human relay rather than content in the HTTP document sense. The distinction between coordination substrate and content substrate is treated here as an architectural assumption. A future companion draft is a candidate vehicle for specifying the content-substrate distinction and the HTTP-as-UI-layer composition pattern in normative terms.

### 7.2 Orchestration and workflow composition

These sit above the agent protocol layer per the referenced draft's Section 3 layered view. AGTP provides Task-ID and Session-ID identifiers usable by an orchestration layer while leaving orchestration semantics to that layer.

### 7.3 Cross-domain admission decisions

AGTP provides identity, delegation, and trust primitives that an admission layer would consume. The admission policy itself sits outside AGTP's scope.

### 7.4 Governance-layer legal semantics

Regulatory obligations, jurisdictional compliance frameworks, and legal-instrument encoding are handled by companion work such as the SOOS family of drafts. AGTP addresses the substrate primitives on which such governance layers can enforce.

## 8. References

### 8.1 Primary Reference

- **draft-foroughi-agent-protocol-dimensions**: P. Foroughi (Nokia). "A Dimensional Model for Characterizing AI Agent Protocol Proposals and Their Substrates." IETF Internet-Draft, 2026. https://datatracker.ietf.org/doc/draft-foroughi-agent-protocol-dimensions/

### 8.2 AGTP Family

All AGTP specifications are published as IETF Independent Submissions by C. Hood (Nomotic, Inc.).

- draft-hood-independent-agtp: Base protocol specification (v09)
- draft-hood-agtp-api: Methods, paths, endpoints, and synthesis
- draft-hood-agtp-bindings: Transport bindings for TCP/TLS and QUIC
- draft-hood-agtp-session: Bounded and persistent sessions
- draft-hood-agtp-discovery: Agent discovery and naming
- draft-hood-agtp-presence: Ambient discovery and visibility
- draft-hood-agtp-agent-cert: X.509 v3 Agent Certificate extensions
- draft-hood-agtp-identifiers: Ten-identifier tamper-evident chain
- draft-hood-agtp-trust: Three-tier trust model with verification paths
- draft-hood-agtp-log: Transparency log and audit
- draft-hood-agtp-communication: Real-time multi-modal communication
- draft-hood-agtp-composition: AGMP, External IdP, and HTTP Gateway composition
- draft-hood-agtp-commerce: Open commerce for agent-to-agent transactions
- draft-hood-agtp-lei: Verifiable LEI binding
- draft-hood-agtp-merchant-identity: Merchant identity and agentic commerce binding
- draft-hood-agtp-web3-bridge: Web3 bridge specification

Repository: github.com/nomoticai/agtp

### 8.3 Related Work Referenced in This Document

- **draft-rampalli-pedigree** (PEDIGREE): delegation-chain semantics with pre-authorization model. Composable with AGTP Delegation-Chain.
- **RFC 9943 (SCITT)**: Supply Chain Integrity, Transparency, and Trust Architecture. draft-hood-agtp-log aligns with RFC 9943 for receipt interoperability.
- **RFC 9942 (COSE Receipts)**: COSE Receipts format. Used by draft-hood-agtp-log for cross-ecosystem receipt exchange.
- **RFC 9162**: Certificate Transparency 2.0. Used by draft-hood-agtp-log for the verifiable data structure.
- **SOOS family (draft-sato-soos-\*)**: Jurisdictional governance layer above the substrate. AGTP substrate primitives feed SOOS governance enforcement points.
- **draft-somoza-dmsc-atn**: Agent Trust Negotiation. Composable with draft-hood-agtp-trust verification paths.
- **draft-jeskey-anml**: Agent Notation Markup Language. Composable with AGTP as a semantic overlay on the substrate.

## 9. How to Read the Coverage

The per-extension coverage view intends only to make explicit, at the extension level, which specific AGTP document provides which specific dimensional value or substrate facet. It makes no claim that AGTP is complete or that other approaches are inadequate. The community can evaluate coverage accurately, identify genuine gaps, and coordinate rather than duplicate at the extension boundary.

Cells marked *In work* are drafted with substantive text and refinement continues. Cells marked *Specified, Implemented* have reference implementations running. Cells marked *Architectural assumption* record deliberate design positions where AGTP treats a concern as outside its scope by design.

## 10. Feedback and Iteration

This mapping is a working draft. Corrections, additions, and disagreements are welcome via GitHub issues on the AGTP repository at github.com/nomoticai/agtp. Corrections will be folded into subsequent revisions of this document, and updates will be shared on the relevant IETF mailing lists.
