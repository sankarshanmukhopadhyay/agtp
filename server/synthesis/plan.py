"""
Plan types: the shapes a composition policy returns when it can fulfill
a proposal.

A :class:`SynthesisPlan` is the recipe a :class:`SynthesisRuntime` will
execute: an ordered list of :class:`CompositionStep` entries, each
naming an underlying method and the parameter sources that fill its
arguments. Each step may capture its output under a name that later
steps can reference via a ``previous_step`` :class:`ParameterSource`.

These types are deliberately data-shaped and free of behavior so they
serialize cleanly into the PROPOSE response body, into recipe TOML,
and into test fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from server.amg.grammar import AMGMethodSpec


# ---------------------------------------------------------------------------
# ParameterSource
# ---------------------------------------------------------------------------


@dataclass
class ParameterSource:
    """
    Where a step's parameter gets its value at execution time.

    Three kinds are supported:

      * ``proposal`` — the value is whatever the caller put in the
        request body under ``value`` (the proposal-side parameter
        name). This is the most common case.
      * ``constant`` — the value is a literal supplied in the recipe;
        ``value`` is the literal itself.
      * ``previous_step`` — the value is the captured output of an
        earlier step; ``value`` is the captured-name string.
    """

    kind: str
    value: Any

    def __post_init__(self) -> None:
        if self.kind not in ("proposal", "constant", "previous_step"):
            raise ValueError(
                f"ParameterSource.kind must be one of "
                f"('proposal', 'constant', 'previous_step'); got {self.kind!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParameterSource":
        return cls(kind=str(data.get("kind", "")), value=data.get("value"))


# ---------------------------------------------------------------------------
# CompositionStep
# ---------------------------------------------------------------------------


@dataclass
class CompositionStep:
    """
    One step in a :class:`SynthesisPlan`.

    ``method_name`` names an underlying method the runtime will
    dispatch (the same dispatcher every external invocation goes
    through, so authority checks fire normally). ``parameter_source``
    maps each *target* parameter name to the :class:`ParameterSource`
    that supplies it. ``capture_output_as``, when set, names the
    variable later steps can reference via
    ``ParameterSource(kind='previous_step', value=...)``.
    """

    method_name: str
    parameter_source: Dict[str, ParameterSource] = field(default_factory=dict)
    capture_output_as: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "method": self.method_name,
            "parameters": {
                k: v.to_dict() for k, v in self.parameter_source.items()
            },
        }
        if self.capture_output_as:
            out["capture_as"] = self.capture_output_as
        return out


# ---------------------------------------------------------------------------
# SynthesisPlan
# ---------------------------------------------------------------------------


_AGGREGATION_MODES = ("last", "merge", "list")


@dataclass
class SynthesisPlan:
    """
    The composition recipe a synthesis will execute.

    A plan binds a proposed method (the AMG-validated spec the agent
    submitted) to a sequence of underlying invocations. Plans flow
    through the runtime as the canonical "this synthesis works" object.
    """

    proposed_method: AMGMethodSpec
    steps: List[CompositionStep]
    output_aggregation: str = "last"
    description: Optional[str] = None
    policy_name: Optional[str] = None  # which policy produced this plan

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("SynthesisPlan must declare at least one step")
        if self.output_aggregation not in _AGGREGATION_MODES:
            raise ValueError(
                f"output_aggregation must be one of {_AGGREGATION_MODES} "
                f"(got {self.output_aggregation!r})"
            )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "proposed_method": self.proposed_method.name,
            "steps": [s.to_dict() for s in self.steps],
            "output_aggregation": self.output_aggregation,
        }
        if self.description:
            out["description"] = self.description
        if self.policy_name:
            out["policy"] = self.policy_name
        return out

    @property
    def underlying_methods(self) -> List[str]:
        """Distinct method names referenced across all steps."""
        seen: List[str] = []
        for s in self.steps:
            if s.method_name not in seen:
                seen.append(s.method_name)
        return seen


__all__ = [
    "CompositionStep",
    "ParameterSource",
    "SynthesisPlan",
]
