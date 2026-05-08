"""
Synthesis runtime: in-memory registry of accepted PROPOSE syntheses.

Split out of ``server.negotiation`` so the policy code stays focused
on decision-making and the runtime state has a clear home.

Lifecycle:

  * PROPOSE 200 inserts a new ``Synthesis`` into ``SYNTHESES``.
  * Subsequent client requests carrying the ``Synthesis-Id`` header
    are rewritten by the server to dispatch onto
    ``Synthesis.target_method`` with parameter remapping.
  * SUSPEND naming a synthesis removes it from the registry.
  * Server restart clears everything; durable session-bound
    syntheses are future work.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Synthesis:
    """A session-scoped reference to an instantiated proposal."""

    synthesis_id: str
    target_method: str
    parameter_mapping: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    proposal_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "synthesis_id": self.synthesis_id,
            "target_method": self.target_method,
            "parameter_mapping": dict(self.parameter_mapping),
            "description": self.description,
            "proposal_name": self.proposal_name,
        }


class SynthesisRegistry:
    """
    In-memory map of ``synthesis_id -> Synthesis``. Process-scoped.
    Thread-safe so concurrent PROPOSE / SUSPEND requests stay clean.
    """

    def __init__(self) -> None:
        self._items: Dict[str, Synthesis] = {}
        self._lock = threading.Lock()

    def add(self, synth: Synthesis) -> None:
        with self._lock:
            self._items[synth.synthesis_id] = synth

    def get(self, synthesis_id: str) -> Optional[Synthesis]:
        with self._lock:
            return self._items.get(synthesis_id)

    def remove(self, synthesis_id: str) -> bool:
        with self._lock:
            return self._items.pop(synthesis_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# Process-global registry. Tests can call ``SYNTHESES.clear()`` between
# runs to keep state isolated.
SYNTHESES = SynthesisRegistry()


def new_synthesis_id() -> str:
    return f"syn-{secrets.token_urlsafe(12)}"


__all__ = [
    "SYNTHESES",
    "Synthesis",
    "SynthesisRegistry",
    "new_synthesis_id",
]
