"""
mod_cache — response caching for idempotent AGTP methods.

Loaded by ``agtpd`` via ``--load-module mod_cache``. The module's
``install(server_state)`` function registers a :class:`CacheHook`
against the daemon's :class:`HookRegistry`. The hook checks an
in-memory LRU cache before each handler runs and stores the result
after.

Cache eligibility is decided per-request by inspecting the
endpoint's semantic block: only endpoints whose semantic declares
``impact == "informational"`` (or `"reversible"` with explicit
``is_idempotent=true``) are cached. Endpoints with side effects are
never cached regardless of how the operator configures TTL.

Configuration via environment variables (default values shown):

  * ``AGTP_CACHE_MAX_ENTRIES`` — soft cap on entries before eviction (1000)
  * ``AGTP_CACHE_DEFAULT_TTL`` — seconds (300)
  * ``AGTP_CACHE_ENABLED`` — set to ``"0"`` to disable without unloading (1)

Sites that need richer configuration (per-endpoint TTL, Redis backend,
stats endpoint) should fork this module or replace it.
"""

from __future__ import annotations

import os
from typing import Any

from mod_cache.hook import CacheHook
from mod_cache.backend import InMemoryCache


__all__ = ["CacheHook", "InMemoryCache", "install"]


def install(server_state: Any) -> None:
    """Boot hook: instantiate the cache backend and register the
    dispatch hook. Called by agtpd after ``--load-module mod_cache``.

    Reads configuration from environment variables (see module
    docstring). A future revision may consult the server config
    object directly for richer per-deployment settings.
    """
    if os.environ.get("AGTP_CACHE_ENABLED", "1") == "0":
        return
    backend = InMemoryCache(
        max_entries=int(os.environ.get("AGTP_CACHE_MAX_ENTRIES", "1000")),
        default_ttl_seconds=float(os.environ.get("AGTP_CACHE_DEFAULT_TTL", "300")),
    )
    hook = CacheHook(backend=backend)
    server_state.hook_registry.register(hook)
