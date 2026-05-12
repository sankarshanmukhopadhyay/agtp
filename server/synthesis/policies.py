"""
Composition policy protocol.

A composition policy is a strategy for fulfilling a proposed method
via composition over the server's available methods. The runtime
tries policies in order; the first one to return a
:class:`SynthesisPlan` wins.

The protocol is intentionally minimal so different strategies plug
in cleanly:

  * :class:`server.synthesis.recipes.RecipeBasedPolicy` — hand-authored
    recipes loaded from TOML (the default).
  * :class:`PassthroughPolicy` — single-step plan when the proposed
    name matches an existing method exactly. This is the fallback
    that preserves the v1 PROPOSE accept-on-exact-match behavior.

Future deployments can add capability-graph and LLM-driven policies
by implementing the same protocol.
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from core.endpoint import EndpointSpec
from server.synthesis.plan import (
    CompositionStep,
    ParameterSource,
    SynthesisPlan,
)


class CompositionPolicy(Protocol):
    """Strategy for fulfilling a proposed method via composition."""

    name: str

    def can_fulfill(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> bool:
        """
        Quick check: can this policy plausibly fulfill the proposal?
        Should be cheap. Used by the runtime to skip expensive
        policies when they are obviously not applicable.
        """
        ...

    def compose(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> Optional[SynthesisPlan]:
        """
        Build a synthesis plan, or return None if no composition is
        found. Called when ``can_fulfill`` returned True; closer
        inspection may still find no viable plan.
        """
        ...


# ---------------------------------------------------------------------------
# PassthroughPolicy
# ---------------------------------------------------------------------------


class PassthroughPolicy:
    """
    Single-step plan when the proposed name matches an existing
    method on this server. Preserves the v1 PROPOSE accept-on-exact
    behavior under the new plan-based runtime: the agent gets a
    synthesis_id whose underlying step is the identity dispatch onto
    the matching method.
    """

    name = "passthrough"

    def can_fulfill(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> bool:
        names = {m.name for m in available_methods}
        return proposal.name in names

    def compose(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> Optional[SynthesisPlan]:
        target = next(
            (m for m in available_methods if m.name == proposal.name), None
        )
        if target is None:
            return None
        # Identity-mapped parameter sources for the params that exist
        # on the target. Anything in the proposal that the target
        # doesn't accept is dropped (the v1 behavior).
        target_params = set(p.name for p in target.required_params) | set(
            p.name for p in target.optional_params
        )
        params: dict = {}
        for p in proposal.required_params:
            if p.name in target_params:
                params[p.name] = ParameterSource(kind="proposal", value=p.name)
        step = CompositionStep(method_name=target.name, parameter_source=params)
        return SynthesisPlan(
            proposed_method=proposal,
            steps=[step],
            output_aggregation="last",
            description=f"identity passthrough onto {target.name}",
            policy_name=self.name,
        )


__all__ = [
    "CompositionPolicy",
    "PassthroughPolicy",
]
