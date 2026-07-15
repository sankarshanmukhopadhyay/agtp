# Security Hardening and Assurance Posture

This document describes AGTP's executable security controls, their enforcement points, produced evidence, and remaining deployment obligations.

## Enforced controls

### Lifecycle authorization

The dispatcher checks `AgentDocument.status` before synthesis, catalog validation, path validation, policy evaluation, or handler resolution. `retired` and `deprecated` identities receive `410 Gone`; `suspended` identities receive `503 Unavailable`. `DISCOVER` and `DESCRIBE` remain available for lifecycle introspection.

**Evidence:** structured `agent-revoked` or `agent-suspended` errors containing `agent_id` and `agent_status`.

### Intent Assertion verification and replay prevention

When `mod_merchant` is loaded in its default fail-closed mode, PURCHASE requests require a verifiable Ed25519-signed Intent Assertion. Audience, expiry, merchant binding, and signed `jti` are checked before dispatch. The `jti` is atomically consumed in a durable SQLite store, preventing duplicate fulfillment across processes and restarts.

Set `AGTP_MOD_MERCHANT_INTENT_VERIFY_KEY` to the verification-key path and optionally set `AGTP_MOD_MERCHANT_REPLAY_DB` to relocate the replay database. `AGTP_MOD_MERCHANT_ALLOW_UNVERIFIED_INTENT=1` is an explicit legacy/development downgrade and must not be used for consequential fulfillment.

**Evidence:** status `458` with `intent-assertion-replayed`, `intent-assertion-invalid`, or binding-specific refusal reasons.

### Shared RCNS abuse-control state

RCNS rolling rate limits and idempotency keys can use transactional SQLite state shared by multiple daemon processes on one host:

```toml
[policies.rcns]
state_backend = "sqlite"
state_path = "/var/lib/agtp/rcns-state.sqlite3"
```

The rate-limit operation atomically prunes expired entries, checks the current count, and reserves an attempt. Idempotency values are durable until their configured expiry. The default `memory` backend remains suitable only for tests and single-process development.

**Evidence:** deterministic `429` responses with `scope = "rcns"`, plus stable synthesis identifiers for repeated `(agent_id, idempotency_key)` calls.

### Audit-chain verification and recovery

`agtp-chain-integrity-check` walks each per-agent chain to genesis and reports missing records, cycles, orphaned heads, and optional signature failures. `--repair-heads` reconstructs a pointer only when the record set proves exactly one complete chain with one unique tip. It refuses to infer authority across forks or incomplete histories.

```bash
agtp-chain-integrity-check --json
agtp-chain-integrity-check --repair-heads
```

**Evidence:** machine-readable chain results, non-zero exit status for unresolved integrity failures, and an explicit repaired-head count.

### Strict OAuth validator posture

`[policies.oauth] enabled = true` with `validator = "noop"` refuses server startup unless `allow_noop_validator = true` is explicitly set. The override is limited to development and CI because the no-op validator accepts any non-empty token.

### Machine-readable deployment posture

Server manifests expose identity binding, attribution-record availability, anonymous-discovery posture, and a derived assurance posture. Attribution records distinguish certificate-bound identity from unverified header identity through `extra.identity_assurance`.

## Enforcement sequence

For consequential calls, controls execute before application handlers: wire parsing and identity resolution, lifecycle authorization, OAuth and module hooks, RCNS gating where applicable, then endpoint dispatch. Merchant replay consumption therefore occurs before fulfillment, and RCNS state is checked before synthesis cost is incurred.

## Operator obligations and residual boundaries

- Plaintext or `mtls.mode=disabled` deployments remain Agent-ID spoofable and are suitable only for local development.
- SQLite provides durable cross-process coordination on one host. Multi-host deployments require a shared backend with equivalent atomic check-and-record semantics.
- JWS audit mode does not independently prove filesystem completeness. Use externally anchored evidence such as SCITT for dispute-grade non-equivocation.
- Recovery tooling restores pointers from retained records; it cannot restore deleted records or resolve genuine forks automatically.
- Protect state databases and audit stores with service-account permissions, backup controls, and monitored filesystem integrity.

## Conformance evidence

A hardened deployment can demonstrate that:

1. Revoked and suspended identities are refused before operational dispatch.
2. Replaying a verified Intent Assertion produces a deterministic refusal before fulfillment.
3. RCNS limits and idempotency remain consistent across local daemon processes and restarts.
4. Missing chain heads can be reconstructed only from a unique, complete retained chain.
5. OAuth no-op configuration fails closed unless a development override is explicit.
6. Manifest posture and Attribution-Record identity assurance match live configuration.
