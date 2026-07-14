# AGTP v0.1.0 — Security Enforcement Baseline

## Summary

This update converts lifecycle status from an audit-only fact into an enforceable authorization control and adds machine-readable assurance disclosure for relying parties. It addresses the critical enforcement and identity-disclosure findings in the governance/security evaluation while preserving wire compatibility for existing manifest consumers.

## Security changes

- Enforces `retired` and `deprecated` status with `410 Gone`.
- Enforces `suspended` status with `503 Unavailable`.
- Preserves `DISCOVER` and `DESCRIBE` for lifecycle introspection.
- Applies lifecycle enforcement before Synthesis-Id execution, preventing revoked agents from invoking negotiated RCNS contracts.
- Adds manifest `security.identity_binding`.
- Adds manifest `assurance` with identity binding, attribution-record status, anonymous-discovery status, and derived posture.
- Adds `identity_assurance` to attribution-record extras, distinguishing certificate-bound from unverified identity.
- Documents plaintext Agent-ID spoofing risk and SCITT guidance.
- Adds regression tests and updates the Server Manifest JSON Schema.

## Compatibility

The new manifest fields and attribution-record field are additive. Lifecycle enforcement is intentionally behavior-changing: requests from identities previously marked retired, deprecated, or suspended will now be refused. Clients should already handle 410 and 503 according to the AGTP status registry.

## Deferred findings

Intent Assertion replay prevention, distributed RCNS rate-limit state, chain-integrity recovery tooling, strict OAuth no-op boot refusal, and wire-path activation remain follow-up work. These are documented in `docs/security-hardening.md`; no claim is made that this update closes every finding in the evaluation.
