"""
Errors raised by the synthesis runtime.

The runtime distinguishes two failure modes:

  * **Composition refusal** — no policy could produce a plan for the
    proposal. Surfaced through the PROPOSE response (422
    ``negotiation-refused`` or 422 ``counter_proposal``); not raised
    as an exception.
  * **Execution failure** — a plan was instantiated and a synthesis_id
    handed to the agent, but a step in the plan failed at invocation
    time. Raised as :class:`SynthesisError` so the runtime can surface
    the failed step alongside whatever outputs were captured before
    the failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from core import wire


class SynthesisError(Exception):
    """
    Raised when a step in a :class:`SynthesisPlan` fails at execution
    time.

    Carries enough context for the runtime to produce a structured
    error response showing which step failed and what was captured
    before the failure.
    """

    def __init__(
        self,
        *,
        failed_step: int,
        method: str,
        underlying_error: "wire.AGTPResponse",
        captured_outputs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.failed_step = failed_step
        self.method = method
        self.underlying_error = underlying_error
        self.captured_outputs = dict(captured_outputs or {})
        super().__init__(
            f"Synthesis failed at step {failed_step + 1} ({method}): "
            f"underlying status {underlying_error.status_code} "
            f"{underlying_error.status_text}"
        )


__all__ = ["SynthesisError"]
