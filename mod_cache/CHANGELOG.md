# mod_cache changelog

Operational module: response caching for idempotent AGTP methods.

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major
version (operational modules consume the daemon's stable hook API).
Minor bumps add features. Patch bumps fix bugs.

## [Unreleased]

### Added — M9 initial release

Initial operational module. Hook-based caching with an in-memory
LRU + TTL backend.

- `mod_cache.InMemoryCache` — single-process LRU + TTL store with
  hit / miss / eviction / expired stats.
- `mod_cache.CacheHook` — implements both `before_dispatch` (cache
  lookup) and `after_dispatch` (cache populate). Eligibility decided
  per-request from the endpoint's `spec.semantic.impact` /
  `is_idempotent` declarations.
- `mod_cache.install(server_state)` — boot hook called by the
  daemon after `--load-module mod_cache`. Reads `AGTP_CACHE_*`
  env vars and registers the dispatch hook.

### Known limits

- Single-process only. Multi-process / multi-replica deployments
  need an external backend (Redis / memcached) — out of scope for
  v1.
- No invalidation API. Entries expire via TTL only.
- No per-endpoint TTL. All cacheable endpoints share
  `AGTP_CACHE_DEFAULT_TTL`.
- The cache key does not include agent identity. For caching
  across agents that's correct; for caching that varies by agent,
  add `agent_id` to the input dict explicitly in the handler.
