# mod_merchant — Changelog

## v1.0.0 — Phase 7 landing

Initial release. Operational module that gates PURCHASE requests
against merchant counterparty identity (`draft-hood-agtp-merchant-identity-02`):

- Reads dispatched AgentDocument via `server_state.lookup()`.
- No-op for non-PURCHASE methods and for `role: agent` targets.
- For `role: merchant` targets:
  - Returns 458 if `status != active`.
  - Returns 458 if inbound `Merchant-ID` ≠ agent's Canonical Agent-ID.
  - Returns 458 if inbound `Merchant-Manifest-Fingerprint` ≠ `sha256(canonical AgentDocument JSON)`.
- Strict mode (`AGTP_MOD_MERCHANT_STRICT=1`) additionally refuses
  PURCHASE with missing Merchant-ID / fingerprint headers.

Pairs with the `agtp.intent` Intent Assertion helper for the buyer
side. Role assignment is a manifest-level operation: set
``"role": "merchant"`` on the agent.json file; Genesis stays
untouched (identity-only).
