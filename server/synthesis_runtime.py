"""
Backward-compat shim for the synthesis runtime.

The runtime moved to :mod:`server.synthesis.runtime` to make room for
the composition policies, plan types, and recipe loader added under
the ``server.synthesis`` package. The legacy data types
(:class:`Synthesis`, :class:`SynthesisRegistry`, :data:`SYNTHESES`,
:func:`new_synthesis_id`) preserve their v1 shape and are re-exported
here so existing imports such as
``from server.synthesis_runtime import SYNTHESES`` continue to
resolve.

New code should prefer the :mod:`server.synthesis` package directly.
"""

from __future__ import annotations

from server.synthesis.runtime import (
    SYNTHESES,
    Synthesis,
    SynthesisRegistry,
    new_synthesis_id,
)


__all__ = [
    "SYNTHESES",
    "Synthesis",
    "SynthesisRegistry",
    "new_synthesis_id",
]
