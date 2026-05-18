# mod_cache

Response caching for idempotent AGTP methods.

`mod_cache` is an **operational module** — loaded into `agtpd`'s
own Python process via `--load-module`, not a separate runtime
process like `mod_python` / `mod_php` / etc. The architecture
distinction is documented in
[`docs/architecture/server-modules.md`](../../docs/architecture/server-modules.md).

## What it does

Hooks into the daemon's dispatch pipeline. Before each handler
runs:
- If the endpoint's semantic block declares it cacheable AND the
  cache has a fresh entry for this `(method, path, input)` tuple,
  serve the cached response and skip the handler entirely.

After each handler runs:
- If the response is a success (2xx) and the endpoint is cacheable,
  store the response with the configured TTL.

## Cache eligibility

An endpoint is cacheable when its semantic block says so. The hook
checks two declarations on `spec.semantic`:

| `impact`          | `is_idempotent` | Cached? |
|-------------------|-----------------|---------|
| `informational`   | (any)           | ✅       |
| `reversible`      | `true`          | ✅       |
| `reversible`      | `false`         | ❌       |
| `irreversible`    | (any)           | ❌       |
| (no semantic)     | —               | ❌       |

The cache key is `SHA-256(method | path | canonical-JSON(input))`
truncated to 16 hex chars (plus the method+path as a prefix for
log readability). Agent identity is **not** part of the key —
caching across agents is correct behavior for informational
endpoints.

Errors are never cached. A handler that returns `EndpointError`
gets called on every subsequent equivalent request until it
succeeds.

## Install

```bash
python -m server 4480 \
    --agents-dir agents/ \
    --endpoints-dir endpoints/ \
    --load-module mod_cache
```

Configuration via environment variables:

| Variable                    | Default | Meaning                          |
|-----------------------------|---------|----------------------------------|
| `AGTP_CACHE_ENABLED`        | `1`     | Set to `0` to disable at boot    |
| `AGTP_CACHE_MAX_ENTRIES`    | `1000`  | Soft cap before LRU eviction     |
| `AGTP_CACHE_DEFAULT_TTL`    | `300`   | Seconds                          |

## What this module does not do

- **No multi-process coordination.** The cache is in-memory and
  per-process. Running multiple `agtpd` instances means N separate
  caches; that's fine for most cases but won't give you a single
  consistent cache across replicas. Use Redis or memcached for that
  (a future `mod_cache_redis` or operator-built variant).
- **No cache invalidation API.** Entries expire via TTL only. An
  endpoint that wants to invalidate a cache entry would need to
  reach into the backend, which `mod_cache` doesn't expose. Keep
  TTLs short for fast-changing data.
- **No per-endpoint TTL override.** Every cacheable endpoint uses
  `AGTP_CACHE_DEFAULT_TTL`. Per-endpoint TTL belongs on
  `EndpointSpec`'s semantic block; a future revision can pick it up.

## Implementation notes

- The backend is an `OrderedDict`-based LRU with TTL. Single-lock,
  single-process. ~150 lines of code.
- The hook is a `DispatchHook` (see [`server/hooks.py`](../../server/hooks.py))
  with both `before_dispatch` and `after_dispatch` implemented.
- Hook errors are caught and logged by the dispatcher; a misbehaving
  cache never breaks dispatch.
