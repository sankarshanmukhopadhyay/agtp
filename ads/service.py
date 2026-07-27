"""
The Anticipatory Discovery Service (prototype).

Ties the signal store and predictor to the discovery substrate: an agent
subscribes, ADS observes its signals, predicts likely-next capabilities,
and **preloads** awareness by proactively resolving those capabilities
across the federation — so the agent's eventual explicit query is a cache
hit rather than a cold lookup.

Privacy is enforced structurally:
  * preloading resolves under the observed agent's own identity
    (``as_agent=agent_id``), so the substrate's visibility filtering applies
    and ADS surfaces only agents the subscriber could already discover;
  * predictions derive from a single agent's signals (see
    :mod:`ads.signals`), so no cross-agent pattern is reconstructed.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ads.predictor import predict_capabilities
from ads.signals import SignalStore


class AnticipatoryDiscoveryService:
    def __init__(self, *, history: int = 64, use_tls: bool = True):
        self.signals = SignalStore(history=history)
        self._use_tls = use_tls
        self._lock = threading.RLock()
        #: (agent_id, capability) -> {results, at}
        self._preloaded: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # -- subscription + capture --------------------------------------

    def subscribe(self, agent_id: str) -> None:
        self.signals.subscribe(agent_id)

    def unsubscribe(self, agent_id: str) -> None:
        self.signals.unsubscribe(agent_id)
        with self._lock:
            for key in [k for k in self._preloaded if k[0] == agent_id]:
                self._preloaded.pop(key, None)

    def observe(self, agent_id: str, capability: str) -> bool:
        return self.signals.observe_capability(agent_id, capability)

    def observe_audit(self, agent_id: str, record: Dict[str, Any]) -> bool:
        return self.signals.feed_audit(agent_id, record)

    # -- prediction --------------------------------------------------

    def predict(self, agent_id: str, *, top: int = 3,
                context: Optional[str] = None) -> List[Tuple[str, float]]:
        if not self.signals.is_subscribed(agent_id):
            return []
        return predict_capabilities(self.signals, agent_id, top=top, context=context)

    # -- preloading --------------------------------------------------

    def preload(self, agent_id: str, dht_node, *, top: int = 3,
                now: Optional[float] = None) -> List[str]:
        """
        Proactively resolve the agent's predicted-next capabilities across
        the federation and cache the results. Returns the capabilities
        preloaded. Only runs for subscribed agents; queries carry the agent's
        identity so results stay within its visibility.
        """
        if not self.signals.is_subscribed(agent_id):
            return []
        from presence.crossscope import cross_scope_discover

        _now = time.time() if now is None else now
        preloaded: List[str] = []
        for capability, _score in self.predict(agent_id, top=top):
            result = cross_scope_discover(
                dht_node, capability, as_agent=agent_id,
                use_tls=self._use_tls, insecure_skip_verify=True,
            )
            with self._lock:
                self._preloaded[(agent_id, capability)] = {
                    "results": result.get("results", []),
                    "providers": result.get("providers", []),
                    "at": _now,
                }
            preloaded.append(capability)
        return preloaded

    def get_preloaded(self, agent_id: str, capability: str) -> Optional[Dict[str, Any]]:
        """Return preloaded results for a capability if ADS anticipated it,
        else None (a cold query the agent must resolve live)."""
        with self._lock:
            entry = self._preloaded.get((agent_id, capability.strip().lower()))
            return dict(entry) if entry else None
