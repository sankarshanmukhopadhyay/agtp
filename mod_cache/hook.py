"""
CacheHook — dispatch hook that consults / populates the cache.

The hook examines the endpoint's semantic block to decide whether to
cache. Only endpoints with ``impact == "informational"`` are cached
unconditionally; ``impact == "reversible"`` endpoints are cached
only when the author has set ``is_idempotent = true``. Endpoints
with ``impact == "irreversible"`` or undeclared semantics are
never cached.

The cache key is a stable digest of the (method, path, sorted input)
tuple. Two requests with identical inputs to the same endpoint
collide on the same cache entry; requests differing in any input
field get separate entries. The cache key does NOT include the
agent identity — caching across agents is fine for informational
endpoints, which is the whole point.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional, Union

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse

from mod_cache.backend import InMemoryCache


class CacheHook:
    """Dispatch hook that wraps :class:`InMemoryCache`."""

    def __init__(self, backend: InMemoryCache) -> None:
        self.backend = backend

    def before_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        server_state: Any,
    ) -> Optional[Union[EndpointResponse, EndpointError]]:
        """Return a cached EndpointResponse on hit, ``None`` on miss /
        ineligibility."""
        if not self._is_cacheable(spec):
            return None
        key = self._cache_key(ctx)
        hit = self.backend.get(key)
        if hit is None:
            return None
        # Only cache success responses. Errors are not cached.
        if isinstance(hit, EndpointResponse):
            return hit
        return None

    def after_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        result: Union[EndpointResponse, EndpointError],
        server_state: Any,
    ) -> None:
        """Store successful responses for eligible endpoints. Errors
        are not cached so transient failures don't get pinned."""
        if not self._is_cacheable(spec):
            return
        if not isinstance(result, EndpointResponse):
            return
        # Don't cache non-2xx responses.
        if not 200 <= result.status < 300:
            return
        key = self._cache_key(ctx)
        self.backend.set(key, result)

    @staticmethod
    def _is_cacheable(spec: Any) -> bool:
        semantic = getattr(spec, "semantic", None)
        if semantic is None:
            return False
        impact = getattr(semantic, "impact", None)
        if impact == "informational":
            return True
        if impact == "reversible" and getattr(semantic, "is_idempotent", False):
            return True
        return False

    @staticmethod
    def _cache_key(ctx: EndpointContext) -> str:
        canonical = json.dumps(
            ctx.input or {}, sort_keys=True, separators=(",", ":"),
        )
        material = f"{ctx.method}\x00{ctx.path}\x00{canonical}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{ctx.method} {ctx.path} {digest[:16]}"
