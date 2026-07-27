"""
The ANS name-binding store.

Maps human-readable names to Canonical Agent-IDs, carrying enough of each
agent's manifest (capabilities, methods, trust posture) to answer
resolution without a follow-up round-trip. Thread-safe: the AGTP server
handles each connection on its own thread.

Freshness bookkeeping (``registered_at`` / ``refreshed_at``) is recorded so
a DESCRIBE-driven refresh loop can find stale bindings; the wire-refresh
loop itself is a later slice.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NameBinding:
    """A single name → Agent-ID binding plus the agent's manifest summary."""

    name: str
    agent_id: str
    manifest: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    registered_at: float = field(default_factory=time.time)
    refreshed_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "manifest": dict(self.manifest),
            "status": self.status,
            "registered_at": self.registered_at,
            "refreshed_at": self.refreshed_at,
        }


class NameStore:
    """Name → Agent-ID bindings for one naming authority."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_name: Dict[str, NameBinding] = {}
        self._name_by_id: Dict[str, str] = {}

    def register(
        self,
        name: str,
        agent_id: str,
        manifest: Optional[Dict[str, Any]] = None,
        *,
        now: Optional[float] = None,
    ) -> NameBinding:
        """Create or update a binding (idempotent on name)."""
        _now = time.time() if now is None else now
        key = name.strip().lower()
        with self._lock:
            existing = self._by_name.get(key)
            registered_at = existing.registered_at if existing else _now
            binding = NameBinding(
                name=name,
                agent_id=agent_id,
                manifest=dict(manifest or {}),
                status="active",
                registered_at=registered_at,
                refreshed_at=_now,
            )
            self._by_name[key] = binding
            self._name_by_id[agent_id] = key
            return binding

    def resolve(self, name: str) -> Optional[NameBinding]:
        """Resolve a name to its active binding, or None."""
        with self._lock:
            binding = self._by_name.get(name.strip().lower())
            if binding is None or binding.status != "active":
                return None
            return binding

    def resolve_by_id(self, agent_id: str) -> Optional[NameBinding]:
        with self._lock:
            key = self._name_by_id.get(agent_id)
            return self._by_name.get(key) if key else None

    def deregister(self, *, agent_id: Optional[str] = None,
                   name: Optional[str] = None) -> bool:
        """
        Remove a binding by agent_id or name. Returns True if one was
        removed. Used for urgent deregistration on lifecycle transition.
        """
        with self._lock:
            key = None
            if name:
                key = name.strip().lower()
            elif agent_id:
                key = self._name_by_id.get(agent_id)
            if not key or key not in self._by_name:
                return False
            binding = self._by_name.pop(key)
            self._name_by_id.pop(binding.agent_id, None)
            return True

    def all_active(self) -> List[NameBinding]:
        with self._lock:
            return [b for b in self._by_name.values() if b.status == "active"]

    def stale_bindings(self, *, max_age: float, now: Optional[float] = None
                       ) -> List[NameBinding]:
        """Bindings whose manifest data is older than ``max_age`` seconds —
        the DESCRIBE-refresh work list (wire-refresh loop is a later slice)."""
        _now = time.time() if now is None else now
        with self._lock:
            return [b for b in self._by_name.values()
                    if _now - b.refreshed_at >= max_age]

    def count(self) -> int:
        with self._lock:
            return len(self._by_name)
