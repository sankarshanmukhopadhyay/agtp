# mod_audit changelog

Operational module: append-only audit log of every endpoint dispatch.

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major
version. Minor bumps add features. Patch bumps fix bugs.

## [Unreleased]

### Added — M9 initial release

Initial operational module. Unsigned JSONL audit log with the field
set AGTP-LOG's signed receipts will carry.

- `mod_audit.AuditLog` — thread-safe append-only JSONL writer.
  Lazy file open; degrades silently on I/O failure.
- `mod_audit.AuditHook` — `after_dispatch`-only hook. Writes one
  entry per response. Opt-in flags for including request input and
  response body (off by default; PII risk).
- `mod_audit.install(server_state)` — boot hook called by the
  daemon. Reads `AGTP_AUDIT_*` env vars.

### Added — signing (lands with daemon-side `[signing]`)

- `AuditHook.signing_service` parameter and the
  `AGTP_AUDIT_SIGN_RECEIPTS=1` opt-in. When set, each receipt is
  Ed25519-signed over its canonical-JSON payload. Signed envelopes
  carry `kid`, `alg`, `signature`, `payload`; unsigned receipts
  keep the v1 flat-fields shape.
- `install()` validates that the daemon has a `SigningService`
  before enabling signing. Warns and falls back to unsigned when
  the operator asked for signing but the daemon isn't configured.

### Deferred to a future revision

- **COSE_Sign1 wrapper.** The current signed shape is Ed25519 over
  canonical JSON; the AGTP-LOG draft calls for COSE_Sign1. Same
  key material, same signing service — the wrapper change is at
  the encoding layer.
- **SCITT transparency-log integration.** Tied to the COSE wrapper.
- **Log rotation.** Operators use logrotate or equivalent.
- **Remote sink.** Local file only. Use log-shipping pipelines.
- **Replay verification tool.** Lands when COSE/SCITT does.
