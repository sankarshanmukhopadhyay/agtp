"""
In-memory LRU cache backend with TTL.

Single-process only. For multi-process deployments (PHP-FPM-style
worker pools, multiple agtpd instances behind a load balancer), an
external backend like Redis is the right answer; that backend lives
in its own module and follows the same :class:`InMemoryCache`
public surface.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class CacheEntry:
    """One cached response with its TTL expiry."""

    value: Any
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.monotonic()
        return now >= self.expires_at


class InMemoryCache:
    """LRU + TTL cache. Thread-safe via a single lock."""

    def __init__(
        self,
        *,
        max_entries: int = 1000,
        default_ttl_seconds: float = 300.0,
    ) -> None:
        self.max_entries = max_entries
        self.default_ttl_seconds = default_ttl_seconds
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0, "expired": 0}

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for ``key``, or ``None`` on miss /
        expiry. Moves the entry to most-recently-used on hit."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._stats["misses"] += 1
                return None
            if entry.is_expired():
                del self._entries[key]
                self._stats["expired"] += 1
                self._stats["misses"] += 1
                return None
            self._entries.move_to_end(key)
            self._stats["hits"] += 1
            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Store ``value`` under ``key``. Evicts the LRU entry if the
        cache is at capacity."""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires_at = time.monotonic() + ttl
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self._entries[key] = CacheEntry(value=value, expires_at=expires_at)
                return
            while len(self._entries) >= self.max_entries:
                self._entries.popitem(last=False)
                self._stats["evictions"] += 1
            self._entries[key] = CacheEntry(value=value, expires_at=expires_at)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict:
        """Returns a snapshot of cache stats. Mutating the returned
        dict has no effect on the cache."""
        with self._lock:
            return dict(self._stats)
