"""
Rendezvous index for cross-scope resolution (PDD §6.1 "Cross-scope
resolution", §10 "Population federation").

The substrate is a federation of scoped overlays (by capability, tier,
industry, region). An agent lives in its own scopes' overlays; a query that
targets a scope the requester has not joined resolves through a rendezvous
point rather than by flooding every overlay.

A scope's rendezvous key is its overlay id — a 256-bit hash (see
:func:`presence.scopes.overlay_id`) — so the DHT already routes to the
coordinator responsible for any scope. That coordinator holds this index:
``scope_key -> {provider coordinator endpoints}``. A coordinator serving a
scope PUBLISHes itself to the scope's rendezvous point; a cross-scope query
LOCATEs the rendezvous point and reads back the providers.

Unlike gossip (which syncs *all* records between direct peers), rendezvous
is targeted by scope and needs no peering between the requester and the
providers — the two coordinators need only share the DHT.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


#: How long a published provider entry is honored without a refresh.
DEFAULT_PROVIDER_TTL = 300


@dataclass
class _Providers:
    #: endpoint -> last-refreshed epoch
    endpoints: Dict[str, float] = field(default_factory=dict)
    #: human-readable capability/scope label, for observability
    label: str = ""


class RendezvousIndex:
    """scope_key -> provider coordinator endpoints, held by the rendezvous
    coordinator for those scopes."""

    def __init__(self, ttl: int = DEFAULT_PROVIDER_TTL):
        self._lock = threading.RLock()
        self._by_scope: Dict[str, _Providers] = {}
        self.ttl = ttl

    def register_provider(
        self, scope_key: str, endpoint: str, *, label: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Record that ``endpoint`` serves the scope keyed by ``scope_key``."""
        _now = time.time() if now is None else now
        with self._lock:
            entry = self._by_scope.setdefault(scope_key, _Providers())
            entry.endpoints[endpoint] = _now
            if label:
                entry.label = label

    def providers(self, scope_key: str, *, now: Optional[float] = None) -> List[str]:
        """Live provider endpoints for a scope (expired entries swept)."""
        _now = time.time() if now is None else now
        with self._lock:
            entry = self._by_scope.get(scope_key)
            if entry is None:
                return []
            fresh = [
                ep for ep, seen in entry.endpoints.items()
                if self.ttl <= 0 or _now - seen < self.ttl
            ]
            # prune expired
            entry.endpoints = {
                ep: entry.endpoints[ep] for ep in fresh
            }
            if not entry.endpoints:
                self._by_scope.pop(scope_key, None)
            return sorted(fresh)

    def label(self, scope_key: str) -> str:
        with self._lock:
            entry = self._by_scope.get(scope_key)
            return entry.label if entry else ""

    def scope_count(self) -> int:
        with self._lock:
            return len(self._by_scope)
