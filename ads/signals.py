"""
Per-agent operational signal store for ADS (opt-in).

Signals are captured only for agents that have *subscribed* — ADS is opt-in
(PDD §6.5). The store keeps, per agent:

  * **invocation** signals — the sequence of capabilities the agent has
    exercised (derived from method invocations / audit records), plus a
    first-order transition count used for co-occurrence prediction;
  * **interaction** signals — recent peer agents;
  * **temporal** signals — coarse activity bucket counts.

The store is strictly per-agent: one agent's signals never contribute to
another's model. That containment is the mechanism behind the
"no cross-agent reconstruction" privacy invariant.
"""

from __future__ import annotations

import threading
from collections import Counter, defaultdict, deque
from typing import Deque, Dict, List, Optional


class _AgentSignals:
    def __init__(self, history: int):
        self.recent_caps: Deque[str] = deque(maxlen=history)
        self.cap_freq: Counter = Counter()
        self.transitions: Dict[str, Counter] = defaultdict(Counter)
        self.recent_peers: Deque[str] = deque(maxlen=history)
        self.temporal: Counter = Counter()


class SignalStore:
    """Opt-in, per-agent signal capture."""

    def __init__(self, history: int = 64):
        self._lock = threading.RLock()
        self._history = history
        self._subscribed: set = set()
        self._agents: Dict[str, _AgentSignals] = {}

    # -- subscription (opt-in) ---------------------------------------

    def subscribe(self, agent_id: str) -> None:
        with self._lock:
            self._subscribed.add(agent_id)
            self._agents.setdefault(agent_id, _AgentSignals(self._history))

    def unsubscribe(self, agent_id: str) -> None:
        with self._lock:
            self._subscribed.discard(agent_id)
            self._agents.pop(agent_id, None)  # forget on opt-out

    def is_subscribed(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._subscribed

    # -- capture -----------------------------------------------------

    def observe_capability(self, agent_id: str, capability: str) -> bool:
        """Record that ``agent_id`` exercised ``capability``. No-op (returns
        False) for agents that have not opted in."""
        cap = capability.strip().lower()
        if not cap:
            return False
        with self._lock:
            if agent_id not in self._subscribed:
                return False
            sig = self._agents[agent_id]
            if sig.recent_caps:
                sig.transitions[sig.recent_caps[-1]][cap] += 1
            sig.recent_caps.append(cap)
            sig.cap_freq[cap] += 1
            return True

    def observe_peer(self, agent_id: str, peer_id: str) -> bool:
        with self._lock:
            if agent_id not in self._subscribed:
                return False
            self._agents[agent_id].recent_peers.append(peer_id)
            return True

    def observe_temporal(self, agent_id: str, bucket: str) -> bool:
        with self._lock:
            if agent_id not in self._subscribed:
                return False
            self._agents[agent_id].temporal[bucket] += 1
            return True

    def feed_audit(self, agent_id: str, record: Dict) -> bool:
        """Extract a signal from an audit-record-shaped dict: its ``method``
        becomes a capability signal (the M5 'signal capture from audit
        records' path)."""
        method = record.get("method") if isinstance(record, dict) else None
        if not isinstance(method, str):
            return False
        return self.observe_capability(agent_id, method)

    # -- read (per-agent only) ---------------------------------------

    def last_capability(self, agent_id: str) -> Optional[str]:
        with self._lock:
            sig = self._agents.get(agent_id)
            return sig.recent_caps[-1] if sig and sig.recent_caps else None

    def frequency(self, agent_id: str) -> Counter:
        with self._lock:
            sig = self._agents.get(agent_id)
            return Counter(sig.cap_freq) if sig else Counter()

    def transitions_from(self, agent_id: str, capability: str) -> Counter:
        with self._lock:
            sig = self._agents.get(agent_id)
            if not sig:
                return Counter()
            return Counter(sig.transitions.get(capability.strip().lower(), Counter()))

    def recent_peers(self, agent_id: str) -> List[str]:
        with self._lock:
            sig = self._agents.get(agent_id)
            return list(sig.recent_peers) if sig else []
