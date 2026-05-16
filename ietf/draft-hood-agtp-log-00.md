---
title: "AGTP Transparency Log Protocol"
abbrev: "AGTP-LOG"
docname: draft-hood-agtp-log-00
category: info
submissiontype: independent
ipr: trust200902
area: "Applications and Real-Time"
workgroup: "Independent Submission"
keyword:
  - AI agents
  - transparency log
  - SCITT
  - agent identity
  - governance

stand_alone: yes
pi:
  toc: yes
  sortrefs: yes
  symrefs: yes

author:
  - fullname: Chris Hood
    organization: Nomotic, Inc.
    email: chris@nomotic.ai
    uri: https://nomotic.ai

normative:
  RFC2119:
  RFC8174:
  RFC8949:
  RFC9052:
  RFC9162:
  RFC9943:
  AGTP:
    title: "Agent Transfer Protocol (AGTP)"
    author:
      fullname: Chris Hood
    seriesinfo:
      Internet-Draft: draft-hood-independent-agtp-07
    date: 2026

informative:
  RFC8615:
  AGTP-CERT:
    title: "AGTP Agent Certificate Extension"
    author:
      fullname: Chris Hood
    seriesinfo:
      Internet-Draft: draft-hood-agtp-agent-cert-00
    date: 2026
  AGTP-TRUST:
    title: "AGTP Trust and Verification Specification"
    author:
      fullname: Chris Hood
    seriesinfo:
      Internet-Draft: draft-hood-agtp-trust-00
    date: 2026

--- abstract

This document specifies the AGTP Transparency Log (AGTP-LOG): the
append-only log protocol that underpins the `log-anchored` Trust Tier 1
verification path defined in {{AGTP}}. AGTP-LOG aligns with {{RFC9162}}
(Certificate Transparency 2.0) as the verifiable data structure and
issues COSE_Sign1 receipts per {{RFC9943}} (SCITT) for cross-ecosystem
interoperability. This document also establishes the AGTP Agent
Identity Taxonomy (Agent Genesis, Canonical Agent-ID, Agent Certificate)
that {{AGTP}} v07 normatively adopts. The log protocol defined here
covers entry submission, receipt retrieval, inclusion-proof retrieval,
consistency-proof retrieval, and discovery via the `log_uri` field of
the Agent Genesis. Per-platform logs are the v00 default; the
federation model follows the SCITT cross-witnessing pattern. Witness
and monitor role definitions, full federation procedures, and the
formal entry-acceptance threat model are deferred to v01.

--- middle

# Introduction

AGTP v07 recognizes `log-anchored` as one of three equivalent Trust
Tier 1 verification paths defined in {{AGTP-TRUST}}. This document
specifies the log protocol that path depends on. Earlier draft
material that referred to an inline AGTP Certificate Transparency Log
(AGTP-CTL) in {{AGTP-CERT}} is superseded by this document. The next
revision of {{AGTP-CERT}} will reference AGTP-LOG normatively rather
than define its own log.

AGTP-LOG inherits the verifiable data structure of {{RFC9162}} and
the receipt format of {{RFC9943}}. This document does not redefine
the cryptographic primitives or the underlying append-only log
construction; it specifies how an AGTP governance platform operates
a log over those primitives, what kinds of statements the log
accepts, how verifiers discover the log, and how AGTP-LOG receipts
are bound to the Agent Identity Document.

The key requirements language follows {{RFC2119}} and {{RFC8174}}.

# Terminology

## AGTP Agent Identity Taxonomy {#identity-taxonomy}

AGTP identity is composed of three distinct elements. This taxonomy
was formalized concurrent with the v07 revision of {{AGTP}} and is
normatively adopted throughout the AGTP draft family.

Agent Genesis:
: The permanent, signed governance-layer document produced at ACTIVATE
  time that establishes an agent's identity. The Agent Genesis is the
  origin record from which all other identity artifacts derive. It is
  issued by the governance platform, signed with the governance
  platform's key, and archived on revocation. An Agent Genesis is
  never reissued; the identity it establishes is permanent for the
  life of the agent.

Canonical Agent-ID:
: A 256-bit cryptographic hash of the Agent Genesis, used as the
  agent's identifier in all AGTP protocol operations. The Canonical
  Agent-ID is the value carried in the `Agent-ID` header on every
  request, the key in the registry, and the subject of transparency
  log entries. The Canonical Agent-ID is derived from the Agent
  Genesis; it is not an independent value.

Agent Certificate:
: An optional X.509 v3 credential defined in {{AGTP-CERT}} that binds
  the Canonical Agent-ID to TLS mutual authentication for transport-
  layer verification and O(1) scope enforcement at Scope-Enforcement
  Points. Agent Certificates are renewable and revocable, with a
  recommended 90-day validity period. The Agent Certificate is a
  derived credential, not an identity substrate; it references the
  Agent Genesis via the `activation-certificate-id` extension.

The relationship among these three elements:

~~~~
ACTIVATE (method call)
   │
   ├──produces──►  Agent Genesis (signed JSON document)
   │                    │
   │                    └──hashed to produce──►  Canonical Agent-ID
   │
   └──optionally, for Level 3 deployments──►  Agent Certificate (X.509)
                                                  │
                                                  └──references──►  Agent Genesis
~~~~

## Log-Specific Terminology

Log:
: An append-only, tamper-evident record of AGTP-LOG statements
  operated by a governance platform. The log implements the
  verifiable data structure of {{RFC9162}} and issues SCITT
  receipts per {{RFC9943}}.

Log Operator:
: The party that operates a log. For AGTP-LOG v00 the log operator
  is normatively the governance platform that issued the Agent
  Genesis statements committed to the log. Future revisions **MAY**
  permit log operation to be delegated to a separate entity.

Statement:
: A single AGTP-LOG entry: a signed assertion by a log operator
  about an agent identity event. Each statement has an event type,
  subject, payload, issuer, and signature. See {{entry-format}}.

Receipt:
: A COSE_Sign1 envelope per {{RFC9943}} issued by a log against a
  statement, attesting that the statement has been committed to the
  log. The receipt is the consumable proof for verifiers. See
  {{receipt-format}}.

Inclusion Proof:
: A cryptographic proof per {{RFC9162}} demonstrating that a given
  statement is present at a specific position in the log's tree
  structure. Embedded within a receipt or retrievable independently
  via the log protocol.

Consistency Proof:
: A cryptographic proof per {{RFC9162}} demonstrating that two
  signed tree heads at different sizes are consistent (the smaller
  tree is a prefix of the larger). Used by monitors to detect
  split-view attacks.

Signed Tree Head (STH):
: The log operator's signed assertion of the current tree size and
  root hash per {{RFC9162}} Section 4.10.

Witness:
: A third party that countersigns the log's signed tree heads to
  attest that it has observed the log in a consistent state. The
  witness role is defined informally here and normatively specified
  in a future revision; see {{open-items}}.

Monitor:
: A party that polls the log's STHs and consistency proofs to detect
  split-view attacks or operator misbehavior. The monitor role is
  defined informally here and normatively specified in a future
  revision; see {{open-items}}.

Verifier:
: A consumer of an AGTP-LOG receipt that confirms a given Agent
  Genesis is committed to a log. Any party holding a Canonical
  Agent-ID and a `log_uri` can act as a verifier.

# Log Scope {#log-scope}

A log **MUST** accept statements for at least the following five
AGTP-LOG event types. The event type is recorded in the statement
envelope (see {{entry-format}}).

| Event Type | Triggered By | Subject |
|---|---|---|
| `agent-genesis-issued` | ACTIVATE method completion | Canonical Agent-ID derived from the new Agent Genesis |
| `agent-genesis-revoked` | REVOKE method completion | Canonical Agent-ID being retired |
| `agent-lifecycle-suspended` | SUSPEND method completion | Canonical Agent-ID entering Suspended state |
| `agent-lifecycle-reinstated` | REINSTATE method completion | Canonical Agent-ID returning to Active state |
| `agent-lifecycle-deprecated` | DEPRECATE method completion | Canonical Agent-ID entering Deprecated state |
{: title="AGTP-LOG v00 Event Types"}

The above five event types form the initial AGTP-LOG event-type
registry (see {{future-registries}}). Future event types **MAY**
be registered as new operational categories arise without requiring
revision of this document.

Implementations **MAY** log additional event types beyond the above
five. Agent Certificate issuance and revocation events, defined in
{{AGTP-CERT}}, are explicit candidates: a deployment that operates
both AGTP-LOG and an Agent Certificate authority **MAY** log
certificate-lifecycle events to the same log. Whether a deployment
logs certificate events is a deployment decision; logging them is
**NOT** required by this document. When logged, the event types
**MUST** be registered in the AGTP-LOG event-type registry; ad-hoc
event-type strings **MUST NOT** be admitted.

Statements **MUST NOT** carry secret material. The Agent Genesis is
public by design; the Canonical Agent-ID is derived from a public
hash; lifecycle states are operational facts. A log operator that
finds itself wanting to commit a secret to the log is misusing the
protocol.

# Log Architecture

## Per-Platform Logs (v00 Default)

Each AGTP governance platform **MUST** operate a log if it issues
Agent Genesis statements with `verification_path: log-anchored`.
The governance platform is the log operator. Statements committed
to the log are signed by the governance platform's key as recorded
in the Agent Genesis `issuer` field.

A governance platform operating a log advertises the log's URI in
the `log_uri` field of every Agent Genesis it produces under the
log-anchored path. See {{discovery}}.

Per-platform logs follow the SCITT issuer-local convention: each
issuer commits its own statements to its own log, signed with its
own key, with receipts referencing that issuer's identity. This
matches typical deployment patterns and minimizes operational
coupling between governance platforms.

## Federation via Cross-Witnessing

When a deployment requires that statements from one governance
platform be verifiable by participants who do not implicitly trust
that platform's log operator, the platform **MAY** invite a witness
to countersign its signed tree heads. The witness adds a second
signature over the STH, attesting that the witness has observed the
log in a consistent state at that tree size.

The cross-witnessing model preserves per-platform log operation
while introducing third-party attestation. A verifier presented
with an Agent Genesis from platform A and the corresponding receipt
**MAY** require the STH to be countersigned by a witness whose key
the verifier trusts before accepting the inclusion proof.

The witness protocol, witness selection, witness key publication,
and the schema of countersigned STHs are deferred to v01 per
{{open-items}}.

# Entry Format {#entry-format}

Every statement is a SCITT-aligned signed envelope per {{RFC9943}}.
The envelope is a COSE_Sign1 structure per {{RFC9052}} containing
the following protected and unprotected header parameters and
payload.

The protected header **MUST** carry:

| Parameter | CBOR Label | Value |
|---|---|---|
| `alg` | 1 | Signing algorithm. **MUST** be `ES256` (-7), `ES384` (-35), `ES512` (-36), or `EdDSA` (-8). |
| `kid` | 4 | Key identifier of the log operator's signing key (governance platform key). |
| `cty` | 3 | Content type. **MUST** be `application/agtp-log-statement+cbor`. |
| `agtp-event-type` | TBD | One of the event-type strings registered in the AGTP-LOG event-type registry. |
| `agtp-subject` | TBD | Canonical Agent-ID, encoded as a 32-byte CBOR byte string. |
| `agtp-issuer` | TBD | URI of the governance platform issuing this statement. |
| `agtp-issued-at` | TBD | Statement issuance time as an RFC 3339 timestamp. |
{: title="AGTP-LOG Statement Protected Header"}

The unprotected header **MAY** carry implementation-specific
parameters; verifiers **MUST** ignore unprotected parameters not
recognized.

The payload **MUST** be a CBOR-encoded map whose contents depend on
the event type:

For `agent-genesis-issued`:

~~~
{
  "agent-genesis": <bytes>,             ; the canonical CBOR-encoded
                                         ; Agent Genesis document
  "log-position": <uint>,                ; assigned by log operator
                                         ; on entry commit
  "previous-tree-size": <uint>           ; tree size before this entry
}
~~~

For `agent-genesis-revoked`, `agent-lifecycle-suspended`,
`agent-lifecycle-reinstated`, and `agent-lifecycle-deprecated`:

~~~
{
  "lifecycle-event": <text>,             ; matches event-type
  "reason": <text>,                      ; operator-supplied reason
  "previous-state": <text>,              ; lifecycle state prior to event
  "new-state": <text>,                   ; lifecycle state after event
  "log-position": <uint>,
  "previous-tree-size": <uint>
}
~~~

The CBOR encoding **MUST** be deterministic per {{RFC8949}}
Section 4.2 (Canonical CBOR). The signature is computed over the
COSE_Sign1 `Sig_structure` per {{RFC9052}} Section 4.4.

A statement is accepted into the log only after the log operator
has verified:

1. The signature is valid against the log operator's published key.
2. The `agtp-issuer` URI matches the log operator's identity.
3. The `agtp-subject` is a well-formed 32-byte Canonical Agent-ID.
4. The `agtp-event-type` is registered in the AGTP-LOG event-type
   registry.
5. The payload structure conforms to the schema for the declared
   event type.
6. For `agent-genesis-issued`, the hash of the embedded Agent
   Genesis matches the `agtp-subject` byte for byte.

Statements failing any of these checks **MUST NOT** be appended to
the log. The log operator **MUST** record the rejection in its
operational logs but **MUST NOT** commit the rejected statement to
the verifiable tree.

# Receipt Format {#receipt-format}

A receipt is a COSE_Sign1 envelope per {{RFC9943}} attesting that
a statement has been committed to the log. Receipts are the
consumable proof for AGTP verifiers.

The receipt's content type is `application/scitt-receipt+cose` per
{{RFC9943}}. The receipt's payload references the statement's
position in the log and embeds an inclusion proof against a signed
tree head.

The protected header **MUST** carry:

| Parameter | CBOR Label | Value |
|---|---|---|
| `alg` | 1 | Signing algorithm; same constraint as statement signing. |
| `kid` | 4 | Key identifier of the log operator's signing key. |
| `cty` | 3 | Content type. **MUST** be `application/scitt-receipt+cose`. |
| `verifiable-data-structure` | TBD | **MUST** be `RFC9162_SHA256` per {{RFC9162}}. |
| `agtp-statement-position` | TBD | The log position (zero-indexed) of the committed statement. |
| `agtp-statement-hash` | TBD | SHA-256 hash of the canonical CBOR-encoded statement. |
| `agtp-signed-tree-head` | TBD | The STH the inclusion proof attests against. |
{: title="AGTP-LOG Receipt Protected Header"}

The payload **MUST** be a CBOR-encoded map carrying the inclusion
proof:

~~~
{
  "tree-size": <uint>,                   ; STH tree size
  "leaf-index": <uint>,                  ; position of statement
  "audit-path": [<bytes>, <bytes>, ...]  ; Merkle audit path per RFC 9162
}
~~~

Receipts **MUST** be retrievable by any party that holds the
Canonical Agent-ID and the `log_uri`. The receipt **MUST NOT**
contain the statement itself; verifiers requiring the statement
**MUST** retrieve it separately. This separation keeps receipts
small enough to embed in the Agent Genesis (`log_inclusion_proof`
field) without bloating the Agent Identity Document.

The receipt for an `agent-genesis-issued` event **MUST** be embedded
in the Agent Genesis's `log_inclusion_proof` field at the moment the
Agent Genesis is finalized. The governance platform commits the
statement to the log, retrieves the resulting receipt, embeds the
receipt in the Agent Genesis, and only then signs the Agent Genesis
and computes the Canonical Agent-ID. This ordering ensures the
Canonical Agent-ID hashes a document containing its own inclusion
proof.

# Log Protocol

The log operator exposes four operations to clients. All operations
**MUST** be available over HTTPS at paths relative to the log's
base URI (the `log_uri` value advertised in the Agent Genesis).
Implementations **SHOULD** also expose these operations over AGTP
at the same logical paths once AGTP transport bindings are stable.

## Submit Statement

`POST {log_uri}/statements`

Request body: a single COSE_Sign1 statement per {{entry-format}}.
Content-Type `application/agtp-log-statement+cose`.

Response on success: HTTP 201 with the resulting receipt per
{{receipt-format}}. Content-Type `application/scitt-receipt+cose`.

Response on signature failure: HTTP 400 with body indicating
which validation step failed (signature, issuer match, subject
format, event type, payload schema, or genesis hash mismatch).

Submission is normatively restricted to the log operator itself:
external parties **MUST NOT** be able to submit statements directly.
The log operator validates and signs internally, then commits. This
preserves the log's role as an attestation of the operator's own
governance decisions.

## Retrieve Receipt

`GET {log_uri}/receipts/{statement-hash}`

Path parameter: hex-encoded SHA-256 hash of the canonical CBOR
statement.

Response on success: HTTP 200 with the receipt per
{{receipt-format}}.

Response on miss: HTTP 404 with body indicating whether the hash is
unknown to this log or refers to a statement that has not yet been
committed.

## Retrieve Inclusion Proof

`GET {log_uri}/proofs/inclusion?leaf-index={index}&tree-size={size}`

Query parameters:

- `leaf-index`: the position of the statement (zero-indexed)
- `tree-size`: the STH tree size against which the proof is computed

Response on success: HTTP 200 with a CBOR-encoded inclusion proof
per {{RFC9162}} Section 4.11. The proof is the audit path; the
caller is responsible for verifying it against the STH at the
requested tree size.

This operation duplicates information already in receipts; it
exists for clients that hold a position and need a fresh proof
against a different STH than the one originally bound in the
receipt.

## Retrieve Consistency Proof

`GET {log_uri}/proofs/consistency?first-tree-size={n}&second-tree-size={m}`

Query parameters:

- `first-tree-size`: the smaller tree size
- `second-tree-size`: the larger tree size

Response on success: HTTP 200 with a CBOR-encoded consistency proof
per {{RFC9162}} Section 4.12. The proof attests that the tree at
size `n` is a prefix of the tree at size `m`.

Used by monitors to detect split-view attacks. A monitor that
observes two different signed tree heads from the same log
operator at the same tree size **MUST** treat that as evidence of
operator misbehavior.

## Retrieve Signed Tree Head

`GET {log_uri}/sth`

Response on success: HTTP 200 with the current STH per {{RFC9162}}
Section 4.10. The response is a CBOR-encoded structure carrying
tree size, root hash, timestamp, and the log operator's signature
over those fields.

The STH is the anchor for all inclusion and consistency proofs
issued by the log. Verifiers **MUST** retrieve and cache the STH
before validating any proof.

# Discovery {#discovery}

A verifier holding a Canonical Agent-ID locates the log containing
the relevant inclusion proof via the `log_uri` field of the Agent
Genesis. This document proposes `log_uri` as a new field to be
added to the Agent Identity Document schema defined in {{AGTP}};
adoption of the field is anticipated in a future revision of
{{AGTP}}, coordinated with the publication of this document.

The `log_uri` field, once adopted, carries an absolute HTTPS URI
pointing to the base of the log's protocol surface. All log
protocol operations defined in this document are exposed at paths
relative to this URI.

A verifier that has retrieved an Agent Genesis (whether via
canonical Agent-ID resolution per {{AGTP}}, via the AGTP DISCOVER
method, or via any other means) **MUST**:

1. Read the `log_uri` field.
2. Validate that the URI uses the `https://` scheme.
3. Open a TLS connection to the log endpoint.
4. Retrieve the STH, verify its signature against the governance
   platform's published key.
5. Retrieve the inclusion proof for the Agent Genesis (either by
   parsing the embedded `log_inclusion_proof` receipt or by
   requesting a fresh proof from the log).
6. Verify the inclusion proof against the STH.

If the `log_uri` field is absent from an Agent Genesis whose
`verification_path` is `log-anchored`, the Agent Genesis is
**malformed** and the verifier **MUST** reject the agent identity.

If the `log_uri` is present but the log is unreachable at
verification time, the verifier **MAY** fall back to the cached
inclusion proof embedded in the Agent Genesis's
`log_inclusion_proof` field if such a cached proof exists and
remains valid against the verifier's last-known good STH.

Until the `log_uri` field is adopted into {{AGTP}}, implementations
**MAY** convey the log URI through other means: as an out-of-band
configuration value keyed on the governance platform's `issuer`
URL, or as a well-known location relative to `issuer` (e.g.,
`{issuer}/log`). These mechanisms are deployment-specific and
**MUST NOT** be relied on for cross-platform interoperability. The
normative discovery mechanism is the `log_uri` field once adopted.

A well-known URI discovery mechanism (e.g.,
`/.well-known/agtp-log`) is not specified in this revision. AGTP
v07 does not currently define any well-known URI conventions, and
introducing one solely for AGTP-LOG discovery is premature. Future
revisions of AGTP-LOG **MAY** define a well-known discovery path
if the broader AGTP family adopts well-known URI conventions.

# Integration with AGTP

The `log_inclusion_proof` field defined in the Agent Identity
Document of {{AGTP}} v07 references the inclusion proof produced by
this log. A verifier retrieves the proof, validates it against the
log's signed tree head, and confirms the Agent Genesis is committed
to the log.

The `verification_path: log-anchored` value defined in {{AGTP-TRUST}}
indicates that the agent's Trust Tier 1 verification depends on a
log-committed Agent Genesis. Verifiers consuming such an agent
**MUST** perform the inclusion-proof verification described in
{{discovery}} before granting Authority-Scope decisions on the
agent's behalf.

The `log_uri` field is proposed by this document as an addition to
the Agent Identity Document defined in {{AGTP}}. Its adoption is
anticipated in a future revision of {{AGTP}}, coordinated with the
publication of this document. Until adoption, deployments **MAY**
convey log location through the alternative mechanisms described
in {{discovery}}.

# Security Considerations {#security}

## Log Operator Compromise

The log operator is also the issuer of statements in the AGTP-LOG
v00 model. A compromised log operator could:

- Sign a fraudulent Agent Genesis for an Agent-ID it does not
  control, commit it to the log, and present the resulting receipt
  as proof of a non-existent agent's existence.
- Refuse to commit valid statements, denying agents legitimate
  Trust Tier 1 standing.
- Backdate statements by manipulating its internal clock prior to
  signing.
- Operate two parallel logs presenting different signed tree heads
  to different verifiers (a split-view attack).

The first three threats are inherent to any single-issuer log model
and are partially mitigated by the witness mechanism (deferred to
v01) and by external monitors that detect inconsistency. The
split-view attack is specifically detectable via the consistency
proof operation: a monitor that observes a second STH at the same
tree size as a previously-observed STH from the same operator
**MUST** treat the divergence as evidence of compromise.

Deployments that require defense against log operator compromise
**SHOULD** require witness countersignatures on STHs (when v01
specifies the witness protocol) and **SHOULD** subscribe to
independent monitors that surface inconsistency evidence.

## Witness Collusion

In a future revision incorporating witnesses, two or more witnesses
colluding with a malicious log operator could countersign
inconsistent STHs. This threat is structurally identical to log
operator compromise from the verifier's perspective: the verifier
trusts the witness set, and a colluding witness set is no better
than a colluding operator. Mitigations are deferred to v01 along
with the witness protocol itself.

## Private Key Handling

The log operator's signing key is the root of all trust in
AGTP-LOG. Operators **MUST** protect this key with controls
appropriate to a long-lived signing identity: hardware security
modules, key ceremonies for key generation and rotation, and
documented operational procedures for key recovery.

Key rotation procedures and the binding of historical STHs to
retired keys are not specified in this revision; see
{{open-items}}.

## Statement Privacy

Statements in AGTP-LOG are public by design. The Agent Genesis
itself is published as the canonical identity record of an agent;
the lifecycle events that follow it are operational facts. AGTP-LOG
is not a confidential audit log.

Operators **MUST NOT** commit statements containing secrets,
operational credentials, or other sensitive material. The validation
gates in {{entry-format}} are insufficient to filter privacy
violations; preventing privacy leaks in statement payloads is the
operator's responsibility.

## Replay and Statement Substitution

A statement signed by a valid log operator and committed to a log
is durable: the log operator cannot retroactively replace it
without breaking the consistency property of the tree. However,
duplicate submissions of the same logical statement at different
positions are not prevented by the protocol; the log operator
**SHOULD** deduplicate statements before commit, using the
canonical CBOR encoding as the deduplication key.

## Threat Model Reference

The full Trust Tier 1 threat model is specified in {{AGTP}}
Section 7 and {{AGTP-TRUST}}. AGTP-LOG is one of three equivalent
verification paths; the threat model considers attacks against the
log-anchored path within the broader context of `dns-anchored` and
`hybrid` alternatives.

# Future Registry Considerations {#future-registries}

The AGTP family includes several constructs (status codes, header
fields, identity document fields, method names, media types) whose
long-term stewardship will require coordinated management once the
ecosystem matures. Several of these constructs are AGTP-specific
and have no current home in existing standards-body registries. The
appropriate venue for AGTP registry stewardship — IANA, an AGTP
governance forum, a foundation, or some combination — is an open
question for the broader AGTP family and is not decided in this
document.

This section enumerates the registry-shaped artifacts AGTP-LOG
defines, so that whichever stewardship model the AGTP family
eventually adopts has a clear inventory of what AGTP-LOG
contributes. No commitment to any specific registry venue is made
by this document.

## AGTP-LOG Event Types

The five v00 event types form an extensible event-type set:

| Event Type | Reference |
|---|---|
| `agent-genesis-issued` | This document, {{log-scope}} |
| `agent-genesis-revoked` | This document, {{log-scope}} |
| `agent-lifecycle-suspended` | This document, {{log-scope}} |
| `agent-lifecycle-reinstated` | This document, {{log-scope}} |
| `agent-lifecycle-deprecated` | This document, {{log-scope}} |
{: title="AGTP-LOG v00 Event Types"}

New event types **MUST** be defined in a published specification
that describes the triggering condition, subject, payload schema,
and the validation rules a log operator applies before commit.
Implementers introducing event types prior to a formal registry
process **SHOULD** prefix experimental types with `x-` to avoid
namespace collisions with future registered types.

## Media Types

This document defines two media types for AGTP-LOG message
serialization:

`application/agtp-log-statement+cose`
: AGTP-LOG statement envelope (COSE_Sign1 per {{RFC9052}}).

`application/agtp-log-statement+cbor`
: AGTP-LOG statement payload (CBOR, deterministic encoding per
  {{RFC8949}} Section 4.2). Used as the payload of the COSE_Sign1
  envelope.

The SCITT receipt media type `application/scitt-receipt+cose` is
defined in {{RFC9943}} and reused without redefinition.

## COSE Header Parameters

The following protected-header parameter names are introduced by
this document. CBOR label assignments are not made by this
document; implementations prior to a formal registry process
**SHOULD** use string-form labels in the protected header rather
than numeric labels, and **MUST** document their string-to-numeric
mapping for downstream tooling.

| Parameter Name | Use |
|---|---|
| `agtp-event-type` | Event type string for the AGTP-LOG statement. |
| `agtp-subject` | Canonical Agent-ID (32 bytes) the statement concerns. |
| `agtp-issuer` | Governance platform URI signing the statement. |
| `agtp-issued-at` | Issuance timestamp per RFC 3339. |
| `verifiable-data-structure` | Receipt parameter; **MUST** be `RFC9162_SHA256`. |
| `agtp-statement-position` | Log position of the statement the receipt covers. |
| `agtp-statement-hash` | SHA-256 of the canonical statement encoding. |
| `agtp-signed-tree-head` | The STH the receipt attests against. |
{: title="AGTP-LOG Protected Header Parameter Names"}

## Identity Document Field

The `log_uri` field defined in {{discovery}} is proposed as an
addition to the Agent Identity Document defined in {{AGTP}}. Its
adoption is anticipated in a future revision of {{AGTP}} and will
be reflected in whichever field-management mechanism the AGTP
family adopts for the Agent Identity Document schema.

# Open Items {#open-items}

The following items are explicitly out of scope for this revision
and are anticipated in future revisions:

- **Witness protocol.** Witness selection, key publication,
  countersigned STH schema, witness inclusion in receipts, and the
  failure modes when witness signatures are inconsistent.

- **Monitor role.** Formal monitor specification: polling cadence,
  consistency-proof retention, split-view reporting protocol, and
  monitor discoverability.

- **Federation across governance platforms.** Cross-platform agent
  identity resolution when multiple governance platforms operate
  independent logs and an agent originates from one but is invoked
  via another. The v00 model assumes per-platform isolation.

- **Key rotation.** Procedures for rotating the log operator's
  signing key, binding historical STHs to retired keys, and
  ensuring older inclusion proofs remain verifiable across key
  rotations.

- **Log operator delegation.** Whether and how a governance
  platform may delegate log operation to a separate entity while
  preserving the issuer-equals-operator property.

- **Statement deduplication.** A normative deduplication algorithm
  that is robust to byte-level variations in CBOR encoding (the
  protocol currently relies on deterministic encoding to make this
  tractable).

- **Discovery via well-known URI.** Specification of a well-known
  discovery endpoint per {{RFC8615}} once the broader AGTP family
  adopts well-known URI conventions.

- **CBOR label assignments for AGTP-LOG header parameters.** The
  table in {{future-registries}} marks these as TBD pending the
  outcome of AGTP family registry stewardship decisions. Until
  numeric labels are assigned, implementations **SHOULD** use the
  string-form parameter names and document their string-to-numeric
  mapping for downstream tooling.

- **Registry stewardship venue.** Whether AGTP-LOG registries
  (event types, media types, header parameters, identity document
  fields) are stewarded through IANA, an AGTP governance forum, or
  another mechanism is an open question for the AGTP family. This
  document enumerates the registry-shaped artifacts it contributes
  without committing to a venue.

# Contributors

Feedback from Scott Courtney (GoDaddy / ANS) on SCITT alignment and
production deployment considerations informed this draft. The
event-type registry structure follows the SCITT philosophy of
deterministic CBOR statement envelopes with externally-managed
event-type names, allowing the AGTP ecosystem to add new event
categories without spec revisions.
