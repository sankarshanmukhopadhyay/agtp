# AGTP v0.1.0 — Security Enforcement Baseline

## Overview

AGTP v0.1.0 consolidates two independent security-hardening efforts into one coherent enforcement baseline. The release focuses on closing the gap between governance state, runtime authorization, relying-party disclosure, and audit evidence.

## Security changes

### Agent lifecycle enforcement

The dispatcher now evaluates lifecycle state before synthesis execution, method catalog validation, path handling, policy evaluation, or endpoint resolution.

- `retired` and `deprecated` agents receive `410 Gone`.
- `suspended` agents receive `503 Unavailable`.
- `DISCOVER` and `DESCRIBE` remain available for lifecycle introspection.
- Revoked agents cannot execute previously negotiated synthesis contracts.

### Intent Assertion verification and replay protection

The reference implementation now provides Ed25519 Intent Assertion verification covering signature, expiry, not-before, audience, and merchant identity.

Merchant enforcement derives the replay identifier and retention interval from the verified JWT's signed `jti` and `exp` claims. Caller-controlled JTI and TTL headers are not treated as authority. Replaying the same signed assertion is rejected with status 458.

`mod_merchant` now fails closed unless an Intent Assertion verification key is configured. Legacy or development operation requires an explicit opt-out.

### Assurance posture disclosure

Server Manifests expose machine-readable identity and assurance information, including identity binding, attribution-record availability, anonymous discovery, and a derived deployment posture.

Attribution Records include `identity_assurance`, distinguishing certificate-bound identity from unverified header identity.

### RCNS identity binding

The shipped server configuration requires verified identity for RCNS negotiation. This prevents a caller from bypassing per-agent negotiation limits by rotating an unauthenticated Agent-ID header. Programmatic configuration retains an explicit compatibility switch for test and embedded deployments.

### OAuth fail-closed configuration

OAuth cannot be enabled with the no-op validator unless the operator explicitly opts in. Production deployments must configure a real validator or acknowledge the insecure development posture.

### Audit-chain integrity

Audit appends are serialized across the complete head-read, record-build, record-write, and head-update transaction, preventing concurrent requests from creating sibling records and silently orphaning one branch.

A new `agtp-chain-integrity-check` command verifies chain continuity, missing records, orphaned heads, cycles, malformed records, and optional signatures.

## Documentation and schema changes

- Added security-hardening guidance and deployment posture documentation.
- Updated the Server Manifest schema for assurance metadata.
- Added operational guidance for verified merchant Intent Assertions.
- Added repository-level release metadata and validation notes.

## Compatibility

The manifest and Attribution Record fields are additive.

Lifecycle enforcement, OAuth no-op refusal, merchant Intent Assertion requirements, and RCNS verified-identity defaults are intentionally behavior-changing security controls. Operators using legacy or local-development configurations must explicitly opt into weaker behavior.

No AGTP wire-format version change is introduced.

## Validation

- Security-focused regression suite: **48 passed**.
- Broad segmented validation: **519 passed, 11 skipped**, plus additional suites progressing without reported failures before execution limits.
- Python compile validation: passed.
- ZIP integrity validation: passed.

## Deferred work

The following remain roadmap items rather than claimed closures:

- federated certificate-status resolution and certificate lifetime policy;
- shared/distributed RCNS rate-limit and idempotency stores;
- external audit anchoring and independent orphan-record discovery;
- wire-level path activation and complete 460 enforcement;
- policy-scoped 459 close-match suggestions.
