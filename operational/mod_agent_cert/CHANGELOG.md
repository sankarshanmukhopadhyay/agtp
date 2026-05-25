# mod_agent_cert — Changelog

## v1.0.0 — Phase 3 landing

Initial release. Operational module that gates AGTP requests against
Agent Certificate extensions (`draft-hood-agtp-agent-cert-01`):

- Reads `EndpointContext.agent_cert_extensions` populated by the
  daemon's dispatcher from the verified peer cert.
- Enforces `Authority-Scope` ⊆ `authority-scope-commitment`. Returns
  455 Scope Violation on excess.
- Enforces `AGTP-Zone-ID` == `governance-zone` (when both present).
  Returns 457 Zone Violation on mismatch.
- No-op when no verified cert or no relevant extensions are present.

Pairs with the `subject-agent-id` extension landed in
`server/mtls.py` (Phase 3): the cross-check between extension and
key-derived Agent-ID runs at the TLS layer, before this hook
executes.
