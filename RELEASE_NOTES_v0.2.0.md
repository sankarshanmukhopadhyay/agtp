# AGTP v0.2.0 — Deferred Security Controls Completion

## Summary

AGTP v0.2.0 completes the security work deferred from the v0.1.0 enforcement baseline. The release activates replay protection on the merchant wire path, introduces shared durable RCNS abuse-control state, adds safe audit-chain head recovery, retains strict OAuth no-op boot refusal, and aligns operator documentation with the controls that are now executable and testable.

## Security and assurance changes

- **Intent Assertion replay prevention:** `mod_merchant` now installs a durable SQLite replay store by default. Verified JWT `jti` values are atomically consumed across daemon processes and survive restarts. Operators can relocate the database with `AGTP_MOD_MERCHANT_REPLAY_DB`.
- **Distributed RCNS state:** `[policies.rcns].state_backend = "sqlite"` provides cross-process rolling rate limits and idempotency records. The SQLite backend uses transactional reservations, WAL mode, and a configurable `state_path`.
- **Audit-chain recovery:** `agtp-chain-integrity-check --repair-heads` reconstructs missing or stale chain-head pointers only where persisted Attribution-Records prove one complete, unambiguous chain to genesis. Ambiguous branches and broken chains remain untouched and return failure evidence.
- **OAuth boot refusal:** OAuth configured with the `noop` validator continues to fail startup unless the operator explicitly enables the development-only override.
- **Wire-path activation:** merchant replay checks run in the registered `before_dispatch` hook before application fulfillment; RCNS rate and idempotency enforcement run in the negotiation gate before synthesis work.
- **Evidence:** dedicated regression tests cover cross-instance state sharing, expiry, safe repair, strict refusal, and existing wire-path behavior.

## Configuration

```toml
[policies.rcns]
enabled = true
state_backend = "sqlite"
state_path = "/var/lib/agtp/rcns-state.sqlite3"
```

```bash
export AGTP_MOD_MERCHANT_REPLAY_DB=/var/lib/agtp/merchant-replay.sqlite3
```

Production deployments should place both databases on durable storage with permissions restricted to the AGTP service account. SQLite coordinates multiple local daemon processes; horizontally distributed hosts should use a deployment-specific shared backend implementing the same atomic semantics.

## Compatibility

The release is wire-compatible. Security behavior becomes stricter when `mod_merchant` is loaded: a previously consumed Intent Assertion is refused. RCNS defaults to the existing in-memory state backend unless `state_backend = "sqlite"` is selected, preserving development behavior while providing an explicit production-grade local option.

## Validation

- Deferred-control regression suite passes.
- Full Python suite passes with repository package roots configured through the documented test environment.
- Chain recovery is fail-safe: it writes no head when records contain forks, missing predecessors, mixed agent identifiers, or no unique tip.
