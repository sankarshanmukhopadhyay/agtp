# AGTP Discovery, Name Service, and Presence: A Self-Evaluation Against DAWN Discovery Requirements

**Author:** Chris Hood (Nomotic AI)
**Version:** 0.2 (Draft for community feedback)
**Related documents:**
- draft-moussa-dawn-gap-analysis-01 (methodology and evaluation framework)
- draft-akhavain-moussa-dawn-problem-statement (DAWN problem statement)
- draft-king-dawn-requirements (REQ-DISC corpus)
- draft-hood-agtp-discovery-01 (AGTP Agent Discovery and Name Service; specifies both the DISCOVER method and ANS)
- draft-hood-agtp-identifiers-02 (AGTP Identifiers; specifies Canonical Agent-ID and identifier resolution targets)
- draft-hood-agtp-presence-00 (AGTP Presence; specifies the substrate-level ambient discovery layer)
- DAWN Working Group charter (dawn-charter-02)

## 1. Purpose and Scope

This document is an independent self-evaluation of three related AGTP (Agent Transfer Protocol) mechanisms against the DAWN discovery requirements corpus. It applies the evaluation methodology defined in draft-moussa-dawn-gap-analysis-01 to three AGTP mechanisms:

- The DISCOVER method, which is the AGTP wire-level interface for capability-based agent queries.
- The Agent Name Service (ANS), which provides governed name-to-Agent-ID resolution analogous to DNS.
- AGTP Presence, which provides substrate-level ambient discovery through a Kademlia-style DHT and gossip protocol with cryptographically declared visibility scoping.

The purpose is to provide the DAWN working group with a data point on how a purpose-built agent substrate scores against the same rubric applied to DNS, mDNS/DNS-SD, SSDP/UPnP, A2A, CATS, WebFinger, and manual configuration-based discovery.

The document takes an honest self-evaluative stance. Where AGTP mechanisms meet DAWN requirements natively, the scoring reflects that. Where AGTP mechanisms have known limitations, weaknesses, or open questions, the scoring reflects those as well. The intent is to inform, rather than to advocate.

This is a self-evaluation by the AGTP author. It is offered for community review, correction, and challenge. A subsequent version may incorporate feedback from DAWN participants and other reviewers.

## 2. Methodology

The methodology follows draft-moussa-dawn-gap-analysis-01 Section 4:

- Coverage of REQ-DISC items rated at three levels: Full, Partial, None.
- Security, privacy, and DAWN suitability rated on a 0-3 scale:
  - 0: Unsuitable. High security or privacy risk; fails to meet DAWN requirements.
  - 1: Low suitability. Requires more than minor changes to meet at least one criterion.
  - 2: Moderate suitability. Meets one criterion natively and can meet at least one of the remaining criteria with minor changes.
  - 3: High suitability. Meets at least two criteria natively and meets the third either natively or with only minor changes.
- Adversary model: passive observer, on-path attacker, operator adversary, malicious publisher, Sybil.
- Mitigation cost per gap: Low, Medium, High.
- Deployment cost per mechanism: Low, Medium, High.

The three AGTP mechanisms are evaluated individually, then evaluated as a composed system in Section 7. Architectural cohesion arguments follow in Section 8.

Scoring in this document is design-based: it evaluates whether the specified mechanism meets DAWN criteria when implemented as drafted. Adoption maturity, ecosystem breadth, and production deployment scale are treated as separate operational considerations rather than as suitability scores. Where deployment maturity is a material concern, it is called out explicitly.

## 3. AGTP Substrate Overview

AGTP (Agent Transfer Protocol) is a substrate for AI agent traffic operating on IANA-registered port 4480. It is designed around the principle that agent coordination is a substrate concern, and that identity, discovery, delegation, trust, communication, and audit belong at the coordination layer rather than being reconstructed by every application above HTTP. The AGTP family is a set of Internet-Draft Independent Submissions currently under review.

The substrate uses semantic method naming as its core primitive. Semantic methods have been empirically validated: a 7,200-trial benchmark showed 10 to 29 percentage point accuracy gains over CRUD methods across a range of coordination tasks.

The AGTP identity primitives (draft-hood-agtp-identifiers-02) define the Canonical Agent-ID as a 256-bit SHA-256 hash of a signed Agent Genesis document. The Agent-ID is content-addressable, exists independently of any registry or DNS name, and resolves to an Agent Identity Document through the mechanisms defined in the base AGTP specification. Identity-lifecycle events are recorded in the AGTP-LOG transparency log.

Three AGTP mechanisms are directly relevant to DAWN and are evaluated in this document:

- **The DISCOVER method** (Section 4) is an AGTP Tier 1 method that queries for agents matching a capability description, trust requirement, and governance context. It returns a ranked list of Agent Manifest Documents. DISCOVER operates in three modes: direct query against the AGTP-Presence overlay, brokered query through an ANS server, and cross-scope resolution for queries that target a scope outside the requesting agent's current overlay membership.

- **The Agent Name Service (ANS)** (Section 5) provides governed resolution of human-readable names to Canonical Agent-IDs, architecturally analogous to DNS for the web. ANS is federated: multiple naming authorities exist, and cross-authority resolution proceeds through bilateral trust establishment between ANS operators. Small organizations may use the governance platform's built-in ANS functionality; large organizations deploy dedicated ANS infrastructure.

- **AGTP Presence** (Section 6) provides substrate-level ambient discovery. Agents joining the AGTP substrate become structurally visible to other agents within their applicable visibility scope without requiring registration with a directory. Presence combines a Kademlia-style DHT keyed by Canonical Agent-ID, a gossip-based protocol for state convergence, and trust-tier-scoped overlay partitioning with cryptographically declared visibility.

The three mechanisms are layered rather than mutually exclusive. Presence provides the substrate-level population view. DISCOVER operates over Presence for capability filtering. ANS provides the naming layer for name-to-Agent-ID resolution. A full deployment uses all three, composed with the broader AGTP identity, delegation, trust, and audit primitives.

## 4. The DISCOVER Method

- **Summary**: DISCOVER is an AGTP Tier 1 method registered in the AGTP method registry. It queries for agents matching a capability description, trust posture, and governance context, and returns a ranked list of Agent Manifest Documents. DISCOVER is the wire-level protocol interface for capability-based agent discovery in the AGTP substrate.

- **How it works**: A requesting agent invokes DISCOVER with parameters including a natural-language intent, structured capability domains, minimum Trust Tier, minimum Behavioral Trust Score, governance zone, and organizational domain constraints. The requesting agent MUST carry the `discovery:query` scope in its Authority-Scope header. The method operates in three modes, distinguished by the endpoint queried:

  - **Direct Presence query.** The DISCOVER request is evaluated against the local AGTP-Presence overlay coordinator. Results are drawn from agents currently visible in the requesting agent's visibility scope, as defined by AGTP Presence's scoped overlay partitioning. This mode requires no ANS server.

  - **ANS-brokered query.** The DISCOVER request is sent to an ANS server, which combines its indexed naming records with current Presence overlay state to return ranked results. ANS-brokered queries provide capability-aware ranking, scope negotiation (the ANS declares what Authority-Scope the requester will need to interact with each discovered agent), and cross-zone federation.

  - **Cross-scope resolution.** When the query targets a capability or industry partition outside the requesting agent's current overlay membership, the cross-scope mechanism in AGTP Presence returns agent records from the target scope, which DISCOVER then ranks and returns.

  Results are ranked by a composite score combining Trust Tier weight (default 0.3), Behavioral Trust Score weight (default 0.4), and capability match score weight (default 0.3). Behavioral Trust Score is a value in the range 0.0 to 1.0 assigned to an agent at packaging time by a governance platform's pre-packaging verification pipeline and embedded in the agent's `.agent` or `.nomo` package. The score is covered by the package integrity hash and cannot be agent-asserted or modified without invalidating the package. The full response set is signed by the ANS server's governance key; the requesting agent MUST verify the `ans_signature` before trusting any result.

- **Security, Privacy, and DAWN suitability Considerations:**

  - **Security:**
    - Strengths: The response is signed by the ANS server's governance key; unsigned or invalid responses MUST be rejected by the requesting agent. Behavioral Trust Score is embedded in the signed Agent Manifest Document at packaging time and cannot be inflated by the advertising agent. Rate limiting per requesting Agent-ID is required to be enforced by ANS servers, reducing enumeration risk. Scope negotiation is informational and the requesting agent MUST evaluate declared required scope against its own authorization before granting.
    - Weaknesses: The mechanism is at the individual-submission stage of standardization and lacks the deployment breadth of DNS or WebFinger. DISCOVER is defined only within the AGTP substrate; adoption in ecosystems anchored in DNS or HTTP requires substrate bridging. The `capability_match_score` computation is implementation-defined, requiring only deterministic behavior for identical inputs; comparability across ANS operators depends on operator disclosure of the ranking implementation.

  - **Privacy:**
    - Strengths: DISCOVER queries can be scoped to specific governance zones and organizational domains, avoiding broadcast to unrelated agents. Cross-scope resolution proceeds through rendezvous indices rather than through population enumeration. Presence-mode declarations enforce filtering at the substrate: an agent whose visibility posture excludes the requester is filtered from results before the query completes.
    - Weaknesses: A DISCOVER response includes Agent-IDs, Owner-ID domains, Trust Tier, Behavioral Trust Score, and supported methods for each returned candidate. Publishers who advertise sensitive capability information into public presence scopes accept a corresponding exposure. Query logging by ANS operators is possible and requires operational policy discipline to control.

  - **DAWN suitability:** High.
    - DISCOVER is purpose-built for the case the DAWN charter identifies as the principal use case: an AI agent finding another AI agent with specific capabilities. Capability-based lookup is native. Attribute-based filtering (Trust Tier, governance zone, organizational domain, minimum Behavioral Trust Score) is native. Signed responses provide attestation evidence. Cross-administrative-boundary operation is supported through ANS-brokered queries with cross-zone federation.
    - Machine-readable metadata about discovered entities is the primary payload of every DISCOVER response.
    - Trust in discovery information is anchored in the Agent Genesis chain of each candidate and in the ANS server's governance key for the response envelope.
    - Higher-layer functions (capability negotiation through scope negotiation, service selection through ranking, orchestration through composition with delegation primitives) compose naturally with DISCOVER.

  - **Security, Privacy, and DAWN suitability score: 3/3**
    - Security and DAWN suitability are met natively. Privacy is met natively for publishers who use scoped presence declarations and the substrate's audience scoping; operational log discipline is required for full effect.

- **Use case fitness (against DAWN charter principal use cases):**
  - "How an AI agent can find another AI agent with specific capabilities": Full.
  - "How a workload orchestrator can locate compute resources in a particular jurisdiction": Full (governance zone and industry filtering).
  - "How a service consumer can discover providers that support a required protocol version": Full (capability domain matching against method library).

- **Mitigations grouped by goal:**

  - **Mitigation for adoption in DNS-anchored ecosystems:**
    - Mitigation: Publish DNS pointer records (SVCB) that direct DNS-anchored clients to ANS endpoints, and support hybrid discovery flows in which DNS bootstraps a DISCOVER query against an ANS server.
    - Cost: Low to Medium. Uses existing DNS extensions with new record semantics.
    - Impact: Enables incremental adoption by ecosystems anchored in DNS.

  - **Mitigation for query-log privacy against ANS operator observation:**
    - Mitigation: Define minimum log-retention and log-minimization requirements for conforming ANS operators. Support encrypted query transport per the AGTP wire specification. Consider anonymous or pseudonymous querying modes in future revisions.
    - Cost: Medium. Requires protocol-level privacy features and operator governance.
    - Impact: Reduces cross-operator correlation risk.

  - **Mitigation for ranking algorithm comparability across ANS operators:**
    - Mitigation: Standardize the `capability_match_score` computation, or require ANS operators to publish their ranking algorithm parameters and behavior. Provide reference implementations.
    - Cost: Medium. Requires community consensus on ranking semantics.
    - Impact: Improves cross-operator result comparability.

- **Overall mitigation score: LOW to MEDIUM.**

## 5. Agent Name Service (ANS)

- **Summary**: The Agent Name Service (ANS) provides governed resolution of human-readable names to Canonical Agent-IDs. ANS is architecturally analogous to DNS for the web: multiple ANS servers exist as authoritative naming servers for their naming authorities, and cross-authority resolution proceeds through federation. ANS servers act as Scope-Enforcement Points for naming traffic, enforcing Trust Tier requirements, Behavioral Trust Score floors, and governance zone constraints before returning results.

- **How it works**: An ANS server is an AGTP endpoint that maintains a registry of name-to-Agent-ID bindings for agents in its naming authority. ANS servers are themselves AGTP agents; they have Canonical Agent-IDs, Agent Genesis records, and Agent Manifest Documents. When an agent completes AGTP ACTIVATE, the governance platform automatically submits the agent's Agent Manifest Document to the designated ANS servers for the agent's governance zone; manual registration is unsupported. The ANS server indexes the agent's capabilities, Trust Tier, and Behavioral Trust Score, and updates its result index within 60 seconds. ANS servers periodically refresh capability data through DESCRIBE requests to indexed agents at a recommended 24-hour interval.

  Cross-organizational resolution proceeds through federated queries. When a local ANS server lacks sufficient results, it MAY forward the query to peer ANS servers with which it has established bilateral federation trust. Federated queries carry the original requesting agent's Agent-ID and scope requirements; forwarding ANS servers MUST NOT expand scope or lower trust requirements in the forwarded query. The local ANS server merges, re-ranks, and re-signs the combined result set before returning it. ANS federation is analogous to the DNS root and TLD hierarchy; population federation is handled separately by AGTP Presence's cross-scope resolution.

  Deployment options are documented in the Discovery draft's ANS Deployment Considerations. Small organizations may use the AGTP governance platform's built-in ANS functionality, in which the governance platform's registry endpoint serves both registration and discovery queries. Large organizations SHOULD deploy dedicated ANS infrastructure with appropriate indexing and caching. The ANS index is a read-heavy workload; standard caching and replication patterns apply.

- **Security, Privacy, and DAWN suitability Considerations:**

  - **Security:**
    - Strengths: All DISCOVER responses returned by an ANS server MUST be signed by the ANS server's governance key. The ANS server's governance key MUST be resolvable via the ANS server's own Agent Manifest Document, creating a verifiable trust chain. Requesting agents MUST verify the `ans_signature` before trusting any result; unsigned or invalid responses MUST be rejected. Behavioral Trust Score comes from the verified Agent Manifest Document rather than from agent-asserted fields; the score cannot be inflated without invalidating the package integrity hash. Deregistration on lifecycle transition (Suspended, Revoked, Deprecated) is required within 60 seconds.
    - Weaknesses: An ANS operator that is compromised or malicious could deny resolution to specific agents (censorship), though forgery of resolution results remains blocked by the required signature verification against the ANS's own Agent Manifest Document. Cross-federation trust establishment requires bilateral protocol work between operators; the federation trust protocol references DNS ownership challenge and mutual certificate exchange as its pattern, but the specific protocol is deferred to future work. Deployment of dedicated ANS infrastructure is early-stage; production experience at cross-organizational scale is limited.

  - **Privacy:**
    - Strengths: ANS queries and responses carry the requesting agent's Agent-ID as part of standard AGTP wire identity, allowing publishers to enforce visibility rules per requester. Federated queries preserve the original requester's identity and scope, preventing forwarding operators from broadening query effect. Deregistration on lifecycle transition prevents Revoked agents from continuing to appear in discovery results.
    - Weaknesses: Query patterns are observable to the ANS operator by default. Cross-registry correlation is possible if operators cooperate or are compromised. Standard operational privacy hygiene (transport encryption, log minimization, rate limiting) is required to reduce exposure.

  - **DAWN suitability:** High for name resolution and for governed capability-based query brokering.
    - ANS is purpose-built for name-to-Agent-ID resolution across administrative boundaries. It composes with DISCOVER (Section 4) for capability-based query, with Presence (Section 6) for population state consultation, and with the broader AGTP trust and audit primitives.
    - Machine-readable metadata about resolved agents is available through the Agent Manifest Document that ANS returns.
    - Federation is architectural rather than assumed; the DNS-analogous root-and-TLD pattern is explicit in the specification.
    - Controlled publication and selective disclosure are supported through Trust Tier enforcement, governance zone constraints, and audience scoping in the referenced Presence layer.

  - **Security, Privacy, and DAWN suitability score: 3/3.**
    - DAWN suitability is met natively for name resolution. Security is met natively through required signature verification; deployment maturity of federated ANS operators is a separate operational concern. Privacy is met with standard operational hygiene.

- **Use case fitness (against DAWN charter principal use cases):**
  - "How an AI agent can find another AI agent with specific capabilities": Full when composed with DISCOVER (Section 4).
  - "How a workload orchestrator can locate compute resources in a particular jurisdiction": Full through governance zone filtering.
  - Name-anchored resolution across administrative boundaries: Full.
  - Identity persistence through infrastructure change: Full (Canonical Agent-ID is stable regardless of endpoint changes).

- **Mitigations grouped by goal:**

  - **Mitigation for cross-organizational federation trust protocol:**
    - Mitigation: Formalize the ANS federation trust protocol as a companion specification. Define the DNS ownership challenge and mutual certificate exchange procedure precisely, and provide a reference implementation.
    - Cost: Medium. Requires protocol specification work and cross-organization coordination for interoperability testing.
    - Impact: Enables production cross-organizational federation at scale.

  - **Mitigation for query-pattern privacy against ANS operator observation:**
    - Mitigation: Encrypted resolution queries analogous to DoH for DNS, query minimization requirements for operators, and standardized log-retention limits.
    - Cost: Low to Medium. Uses existing techniques from DNS privacy work.
    - Impact: Reduces cross-operator correlation and third-party observation.

  - **Mitigation for ANS availability:**
    - Mitigation: Cache resolved bindings at agents that have interacted previously, using TTL-based staleness with re-resolution on cache miss. Support out-of-band Location Record distribution for high-availability requirements. Multiple ANS servers per authority, per the federation model, already provide redundancy.
    - Cost: Low. Standard caching semantics.
    - Impact: Reduces the failure surface presented by any single ANS operator outage.

- **Overall mitigation score: LOW to MEDIUM.**

## 6. AGTP Presence

- **Summary**: AGTP Presence is a substrate-level ambient discovery layer specified in draft-hood-agtp-presence-00. It inverts the pull-based discovery model: when an agent joins the AGTP substrate, it becomes structurally addressable and visible to the relevant scope of other agents immediately, without requiring registration with a central directory. The architecture combines a Kademlia-style DHT keyed by Canonical Agent-ID, a gossip-based protocol for presence convergence, and trust-tier-scoped overlay partitioning with cryptographically declared visibility.

- **How it works**: AGTP Presence defines three new AGTP lifecycle methods: ANNOUNCE (publish presence), WITHDRAW (remove presence before disconnection), and PROBE (query current state of a specific agent). Full-node agents maintain Kademlia k-bucket routing tables (k=20) and perform parallel disjoint lookups (alpha=3) per S/Kademlia. Presence records include the agent's declared visibility posture from its AGTP-CERT extension, a timestamp, and a JWS signature verified against the agent's certificate.

  Visibility is partitioned across overlapping scopes on five dimensions: `tier` (from AGTP-TRUST Trust Tier values), `owner-domain` (Owner-ID domain), `capability` (derived from the AGTP method library rather than free-form text), `industry` (classifications such as NAICS or ISIC), and `region` (ISO 3166 or operator-defined). The `capability` dimension is significant: it is a controlled vocabulary tied to the method library, preventing keyspace fragmentation from arbitrary capability strings and providing the same vocabulary for both filtering and partitioning.

  Three participation modes are defined: Full Node (maintains DHT routing tables, participates in gossip), Light Client (delegates presence and discovery to full-node peers), and Relay-Mediated (operates entirely through a relay full node, appropriate for agents behind NAT or firewall boundaries). Cross-scope resolution provides rendezvous-based lookup when an agent needs to interact with an agent in a scope outside its current overlay membership; latency is O(log N) across the federation of scoped overlays.

  Visibility is declared in an AGTP-CERT certificate extension carrying three axes: presence mode (`public`, `tier-scoped`, `owner-domain`, `explicit-only`, `invisible`), disclosure mode (`full`, `capabilities`, `identity-only`, `existence-only`), and audience scoping (expressions such as `tier:N`, `owner-domain:<domain>`, `capability:<value>`, combinable with AND, OR, NOT). Runtime signaling through the Presence-Mode header allows reducing effective visibility within the certificate-declared envelope.

  Bootstrap peer selection uses a multi-seed model: agents SHOULD maintain a list of at least three bootstrap peers operated by independent parties, obtained through AGTP-CERT certificate authorities, DNS-anchored seed records, deployment tooling, or previously discovered peers. Trust-tier-specific bootstrap and governance-driven bootstrap rotation are documented mitigations against bootstrap capture.

- **Security, Privacy, and DAWN suitability Considerations:**

  - **Security:**
    - Strengths: Canonical Agent-IDs are 256-bit SHA-256 hashes of Agent Genesis documents, making brute-forcing target Agent-IDs for specific DHT regions computationally infeasible. AGTP-CERT requirements bind Agent-IDs to issuing authority chains, requiring Sybil attackers to obtain valid certificates from cooperating authorities. Tier-scoped presence partitioning isolates Tier 3 Experimental attackers from Tier 1 and Tier 2 scopes. Per-Owner-ID announcement rate limits are specified to prevent presence flooding. S/Kademlia parallel disjoint lookups (alpha=3) provide eclipse resistance. Deterministic content-hash Agent-IDs prevent attackers from cheaply generating IDs clustered around a target.
    - Weaknesses: Bootstrap peer capture remains a residual concern despite multi-seed and rotation mitigations. Visibility declaration is enforced by protocol only where implementations conform; non-conforming implementations may ignore declared posture. Sybil resistance depends on the strength of the AGTP-CERT issuance ecosystem; a permissive certificate authority weakens the Sybil bound.

  - **Privacy:**
    - Strengths: The visibility model provides three orthogonal axes of control (presence, disclosure, audience) declared cryptographically in AGTP-CERT extensions. The `invisible` mode filters an agent from discovery queries entirely; PROBE responses for invisible agents are indistinguishable from non-existence responses. Disclosure modes allow presence acknowledgment without capability metadata exposure (`existence-only`, `identity-only`). Audience scoping composes with presence mode: an agent in `public` mode with restricted audience scoping remains visible only to the specified audience.
    - Weaknesses: The draft explicitly acknowledges that AGTP Presence "does not provide complete protection against sophisticated traffic analysis. The visibility model is best-effort with respect to passive network observation." The draft also acknowledges that "Discovery queries carry AGTP wire-level identity, so they cannot be fully anonymous within the AGTP substrate." Query blinding or oblivious lookup mechanisms are noted as possible future work rather than as current specification.

  - **DAWN suitability:** High for decentralized federated deployment.
    - AGTP Presence directly addresses the DAWN requirement for decentralized and federated deployment that avoids reliance on a single centralized registry. It also addresses the requirement for entities that may appear, disappear, or change capabilities over time; presence expiration through TTL-based record aging is a first-class primitive, and WITHDRAW provides explicit graceful exit.
    - Live current-state discovery through the DHT and gossip layer is a distinctive strength: agents that come online briefly, or that vary in availability, are discoverable in real time rather than through eventually-consistent published records.
    - Cross-administrative-boundary operation is native. The DHT and gossip topology is derived from cryptographic identity and declared visibility rather than from administrative structure.
    - Scaling analysis is explicit: single-scope populations up to approximately one million agents are within the proven operational envelope of existing Kademlia deployments; global populations approaching one billion agents are addressed through the federation-of-scoped-overlays model analogous to BGP inter-AS routing.

  - **Security, Privacy, and DAWN suitability score: 2/3.**
    - Security is met natively against tampering (signed records, cryptographic Agent-ID anchoring). Sybil resistance depends on the AGTP-CERT ecosystem and requires operational investment for high assurance. Privacy is met for the presence-record layer but the draft acknowledges limitations around query-time exposure and traffic analysis. DAWN suitability for decentralized deployment is met natively.

- **Use case fitness (against DAWN charter principal use cases):**
  - "How an AI agent can find another AI agent with specific capabilities": Full through capability-partitioned overlay membership.
  - "How a workload orchestrator can locate compute resources in a particular jurisdiction": Full through the `region` and `industry` partition dimensions.
  - "How a service consumer can discover providers that support a required protocol version": Full through the capability partition dimension derived from the method library.
  - Live current-state discovery (as opposed to published static records): Full.
  - Cross-administrative-boundary decentralized federation: Full through federation-of-scoped-overlays.

- **Mitigations grouped by goal:**

  - **Mitigation for Sybil resistance at high-value scopes:**
    - Mitigation: Combine the specified AGTP-CERT authority binding with reputation systems for peer nodes, additional stake or proof-of-work admission barriers for participation in high-value scopes (particularly Tier 1), and operator monitoring of anomalous participation patterns.
    - Cost: Medium to High. Cryptographic techniques carry compute cost; reputation systems require operator investment; admission-barrier policies require governance work.
    - Impact: Substantially reduces Sybil surface in scopes where the base cost of certificate issuance is insufficient.

  - **Mitigation for query-time privacy:**
    - Mitigation: Route discovery queries through privacy-enhancing relays, use cover traffic, specify query blinding or oblivious lookup mechanisms in future revisions of AGTP Presence. Operators concerned about query privacy MAY use these techniques today; specification is future work.
    - Cost: Medium to High. Increases lookup latency and node compute cost.
    - Impact: Reduces cross-node correlation and third-party observation of query intent.

  - **Mitigation for traffic analysis of invisible-mode agents:**
    - Mitigation: The draft recommends TLS connection padding, traffic shaping, and participation through privacy-enhancing relays for agents requiring strong unobservability. Operators SHOULD acknowledge that invisible-mode agents are visible to network operators they transit.
    - Cost: Medium. Additional overhead for traffic obfuscation.
    - Impact: Raises the cost of traffic-analysis-based inference of agent existence and activity.

- **Overall mitigation score: MEDIUM to HIGH.**

## 7. Combined Assessment: The Three as a Composed System

The three mechanisms are complementary and address different DAWN requirements at different layers:

- **AGTP Presence** provides the substrate-level population layer. Every AGTP agent that joins the network is structurally visible within its declared visibility scope, without requiring any directory query.
- **The DISCOVER method** operates over the Presence substrate for capability filtering. DISCOVER queries return ranked Agent Manifest Documents drawn from the live presence layer within the requesting agent's visibility scope.
- **The Agent Name Service (ANS)** provides the naming layer for human-readable name-to-Agent-ID resolution, complementing Presence and DISCOVER with governed name resolution federated across administrative authorities.

Composed, the three cover the REQ-DISC corpus as summarized in draft-moussa-dawn-gap-analysis-01 Section 5:

| REQ-DISC requirement | AGTP mechanism providing it | Score |
|---|---|---|
| Discovery of agents, services, workloads, and named entities across boundaries | Presence (ambient) + DISCOVER (query) + ANS (name resolution) | Full |
| Discovery by identity, attributes, roles, advertised capabilities | DISCOVER (capabilities, attributes) + ANS (names) + Presence (visibility, live state) | Full |
| Machine-readable metadata describing discovered entities | Agent Manifest Documents returned by DISCOVER and ANS; presence records in Presence | Full |
| Operate across heterogeneous administrative domains; decentralized federated deployment avoiding single centralized registry | Presence (federated-of-scoped-overlays, native decentralization) + ANS (DNS-analogous federation) | Full |
| Trust in discovery information through authenticity, integrity, provenance | Signed responses (ans_signature) + signed presence records + Agent Genesis anchoring + AGTP-LOG transparency log | Full |
| Controlled publication and selective disclosure | Presence visibility model (three axes) + ANS Trust Tier enforcement | Full |
| Scale to internet-wide deployment with frequent updates | Presence scaling analysis (Kademlia proven at 1M nodes; federation-of-scoped-overlays for 1B+) | Partial (design meets the requirement; production deployment is early) |
| Accommodate diverse implementation environments | Substrate operates over any reliable AGTP transport | Full |
| Minimize abuse: unauthorized harvesting, spoofing, tampering | Cryptographic signing + Sybil resistance mechanisms + rate limiting + AGTP-CERT authority binding | Full with the noted Sybil resistance investment |
| Foundation for higher-layer functions | Composes with the full AGTP substrate | Full |

The composed system yields an overall self-evaluation score of **3/3** for DAWN suitability, with acknowledged mitigation work required around Presence Sybil resistance at high-value scopes, query-time privacy for high-sensitivity scenarios, and production deployment of dedicated ANS infrastructure. The mitigation cost is Low to Medium overall for the composed system, since the Presence high-cost mitigations become optional at scopes where the base substrate security is sufficient.

## 8. Architectural Cohesion: One Method, Three Substrates

A reviewer of this document may raise a specific concern: three mechanisms (DISCOVER, ANS, Presence) are being described together as if they form one system, and this may look like kitchen-sinking three independent discovery approaches into a single proposal. This section addresses that concern directly.

The three components form one method (DISCOVER) executed across three substrates. Presence provides the live population layer. ANS provides the naming and cross-authority federation layer. DISCOVER is the wire-level query interface that operates against both. The architectural pattern is directly analogous to DNS: DNS is one system, and yet the resolution path involves recursive resolvers, authoritative servers, root and TLD hierarchies, and caching layers. Each component has a specific role in the unified name resolution system, and no one describes DNS as kitchen-sinking those components.

AGTP DISCOVER follows the same architectural pattern:

- **DISCOVER is the method.** It is the wire-level protocol interface for discovery queries. There is one DISCOVER method, registered once in the AGTP method registry, with one wire format, one parameter schema, and one signed response envelope.
- **Presence is the substrate.** It maintains the live population layer that DISCOVER queries against. It plays a supporting role rather than a competing role, providing what makes DISCOVER's ambient-query mode possible.
- **ANS is the naming layer.** It provides governed name resolution and cross-authority federation. It plays a complementary role rather than a competing role, providing what makes DISCOVER's name-anchored and cross-organization modes possible.

The three components share:

- The same wire format (AGTP)
- The same identity primitive (Canonical Agent-ID)
- The same trust anchoring (Agent Genesis chain)
- The same authorization model (Authority-Scope, `discovery:query`)
- The same response payload (Agent Manifest Document)
- The same audit trail (AGTP-LOG transparency log)

A DISCOVER query issued against a Presence overlay returns the same result payload as a DISCOVER query issued against an ANS server. The requesting agent uses the same method invocation, same signature verification, and same result parsing. What differs is the substrate the method runs against, driven by what the query is asking for.

Several technical questions arise here that this section answers directly:

- **Is Agent-ID lookup distinct from domain lookup?** Yes, and both are required. A stable Agent-ID (SHA-256 of Agent Genesis) survives infrastructure change; a domain fails to. DNS provides only domain-based lookup and lacks any mechanism for Agent-ID lookup. ANS handles the domain-analogous case; the Presence layer handles the Agent-ID-anchored case through DHT lookup on the content-addressable Canonical Agent-ID. Both are use cases that agent coordination requires, and DNS alone cannot serve them.

- **Is capability-based lookup distinct from name-based lookup?** Yes, and both are required. A requester seeking "an agent that can audit Solidity smart contracts with Trust Tier at or above 2 and Behavioral Trust Score at or above 0.88" cannot express that query in DNS. DISCOVER expresses it natively. Whether the query is served from the Presence substrate (for ambient candidates) or from an ANS server (for federated candidates) depends on query scope; the query semantic is unchanged.

- **Is tool-access filtering distinct from capability filtering?** In AGTP, tool access is expressed through the capability domain vocabulary (`capability_domains: ["methods", "tools"]`) and returned in the Agent Manifest Document's `supported_methods` field. DISCOVER queries filter on this vocabulary. Whether tools are exposed as capabilities or as attributes is a controlled-vocabulary choice made in the method library rather than a separate discovery mechanism.

- **Does DNS provide any of the above?** DNS provides name-to-address resolution. It offers no facility for capability filtering, Trust Tier constraints, Behavioral Trust Score verification, cross-scope federation with signed responses, or tool-availability queries. Achieving these on top of DNS requires application-layer conventions (well-known URIs, TXT records with structured content, DID methods, custom resource records) that reconstruct at the application layer what AGTP provides at the substrate layer.

The architectural claim here is a narrow one: DNS remains excellent for name-to-address resolution and is used within AGTP itself for bootstrap peer discovery and Owner-ID domain verification. Agent discovery, however, has requirements DNS was never designed to serve, and meeting those requirements at the substrate level yields cleaner properties than reconstructing them at the application layer above DNS or above HTTP.

The pattern of a unified query method executed across multiple specialized substrates is a common design pattern in distributed systems: SQL queries execute against different storage engines through the same query interface; HTTP requests reach static content, dynamic applications, and reverse proxies through the same request format; DNS resolution proceeds through recursive, iterative, and forwarding modes through the same query protocol. AGTP DISCOVER applies this pattern to agent discovery.

## 9. Architectural Divergence from the DAWN Charter's Initial Framing

The DAWN charter states: "The WG will consider the DNS as a likely initial protocol upon which to build a discovery protocol, while examining other solutions to learn lessons." This document takes that language at face value: DNS is the WG's initial framing, and other solutions are candidates for examination.

AGTP represents one such other solution. It takes several architectural choices that diverge from a DNS-anchored discovery approach:

- **Canonical Agent-ID as primary addressable entity.** In AGTP, the SHA-256 hash of an agent's Agent Genesis is the permanent identity primitive. The Agent-ID exists independently of any registry or DNS name; it resolves to an Agent Identity Document, and identity-lifecycle events are recorded in the AGTP-LOG transparency log. Human-readable names resolve to Agent-IDs through ANS, but the Agent-ID is the substrate-level primary key. In a DNS-first approach, the domain name is the primary addressable entity, and agent identity is typically derived from the domain. The two paradigms differ in behavior when an agent's infrastructure changes: under AGTP, identity persists; under a DNS-first approach, identity typically dissociates from history.

- **Intrinsic metadata bound to Agent Genesis.** In AGTP, agent capability metadata is bound to the Agent-ID through the Agent Manifest Document, which is derived from the Agent Genesis and covered by the package integrity hash. Behavioral Trust Score is embedded in the signed package at issuance and cannot be inflated by the agent at runtime. In a DNS-first approach, publishers declare records into DNS or a companion registry, and consumers read what was published. The two paradigms differ in what "trust" is anchored in: AGTP anchors trust in the Agent Genesis chain; DNS-first approaches typically anchor trust in the CA hierarchy for the domain.

- **Substrate-defined semantic vocabulary.** In AGTP, methods (verbs like DISCOVER, ANNOUNCE, PROBE, DELEGATE) are wire-level primitives with defined meaning across the substrate. The capability partition dimension in Presence derives from the method library, providing the same controlled vocabulary for both filtering and partitioning. In a DNS-first approach layered above HTTP or similar transports, the semantic vocabulary is defined per application. The two paradigms differ in behavior when agents from different organizations coordinate: under AGTP, shared semantic verbs enable coordination without prior integration; under a per-application approach, coordination requires prior contract negotiation or interpretation.

These divergences are neither a criticism of DNS nor a proposal that DNS be abandoned. DNS is excellent at what it was designed for (name-to-address resolution), and ANS itself is expressly modeled after DNS federation patterns. The AGTP argument is that agent coordination has requirements DNS was never designed to serve, and that meeting those requirements at the substrate level yields cleaner properties than reconstructing them above DNS or above HTTP.

The DAWN charter's initial framing of DNS as the likely starting point is a reasonable position given DNS's ubiquity and existing IETF alignment. This document offers a data point on where a different architectural starting point leads when scored against the same DAWN requirements.

## 10. Comparison to Other Mechanisms in draft-moussa-dawn-gap-analysis-01

For context, this table summarizes the scores of mechanisms evaluated in draft-moussa-dawn-gap-analysis-01 alongside the three AGTP mechanisms self-evaluated here. All AGTP scores are self-reported and subject to community correction.

| Mechanism | Suitability score (0-3) | Mitigation cost |
|---|---|---|
| DNS (including SVCB/HTTPS and TXT) | 1/3 | HIGH |
| mDNS/DNS-SD | 2/3 | LOW to MEDIUM |
| SSDP/UPnP | 1/3 | MEDIUM |
| A2A (Agent2Agent) | 1/3 | MEDIUM |
| CATS (Compute-Aware Traffic Steering) | 1/3 | MEDIUM to HIGH |
| WebFinger (RFC 7033) | 1/3 | MEDIUM to HIGH |
| Manual / configuration-based discovery | 1/3 | MEDIUM to HIGH |
| **DISCOVER method (self-eval)** | **3/3** | **LOW to MEDIUM** |
| **Agent Name Service (self-eval)** | **3/3** | **LOW to MEDIUM** |
| **AGTP Presence (self-eval)** | **2/3** | **MEDIUM to HIGH** |
| **Composed system (self-eval)** | **3/3** | **LOW to MEDIUM** |

Two observations on the comparison:

- No mechanism evaluated in draft-moussa-dawn-gap-analysis-01 scores 3/3. The composed AGTP mechanisms self-evaluate at 3/3. If the self-evaluation withstands community scrutiny, this suggests that a substrate-first approach warrants serious consideration in the DAWN gap analysis, even where the initial charter framing centers on DNS.
- The DISCOVER method and ANS score at 3/3 individually because both are architecturally well-fitted to DAWN's stated requirements: DISCOVER for capability-based query, ANS for name resolution with federation. AGTP Presence scores 2/3 because the draft itself acknowledges limitations around query-time privacy and traffic analysis; the composed system reaches 3/3 because the composition provides operational alternatives for high-privacy use cases (using ANS-brokered queries for privacy-sensitive lookups, using scoped presence declarations to constrain exposure).

Whether these self-evaluated scores are validated by the DAWN community remains to be seen. This document is offered as input to that conversation.

## 11. Conclusion and Next Steps

Three AGTP mechanisms have been self-evaluated against the DAWN discovery requirements using the methodology of draft-moussa-dawn-gap-analysis-01. Individual scores range from 2/3 (AGTP Presence) to 3/3 (the DISCOVER method and ANS). The composed system meets all evaluated REQ-DISC items with Low to Medium mitigation cost.

The AGTP approach diverges architecturally from the DAWN charter's initial DNS-anchored framing. It takes the Canonical Agent-ID (SHA-256 hash of Agent Genesis) as the primary addressable entity, uses metadata bound to Agent Genesis rather than to published records, and defines wire-level semantic verbs at the substrate. These divergences lead to properties that a DNS-anchored discovery cannot easily match, particularly around identity persistence across infrastructure change, coordination without prior integration, and live current-state discovery through the Presence substrate.

This document is offered to the DAWN working group as a data point. It is a self-evaluation and is subject to community review. Specific requests to the DAWN working group:

- Review the self-evaluation methodology and scoring. Correct where the AGTP author has overstated or understated the mechanisms' properties. The scoring for DISCOVER, ANS, and the composed system at 3/3 is the strongest claim and warrants the most careful review; Presence at 2/3 with acknowledged privacy limitations is the honest scoring drawn from the draft's own security considerations section.
- Consider whether AGTP mechanisms warrant inclusion in draft-moussa-dawn-gap-analysis-01 as additional sections (7.16, 7.17, 7.18 or similar). The authors of that draft have stated the intention is for the analysis to evolve as new solutions come to existence.
- Consider whether the architectural divergence from a DNS-anchored starting point merits examination as part of the "examining other solutions to learn lessons" activity referenced in the DAWN charter.

Feedback is welcome via the DAWN mailing list, the AGTP GitHub repository (github.com/nomoticai/agtp), or directly to the author.
