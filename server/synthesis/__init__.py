"""
Server synthesis runtime: composition policies and execution.

PROPOSE flow at a glance:

  1. ``handle_propose`` validates the proposal under AMG.
  2. The :class:`SynthesisRuntime` walks its policies in order,
     looking for one that can compose the proposal from existing
     server methods.
  3. If a policy returns a :class:`SynthesisPlan`, the runtime
     instantiates it (assigns a synthesis_id, registers in
     :attr:`SynthesisRuntime.active`, mirrors a backward-compat
     :class:`Synthesis` into :data:`SYNTHESES`) and returns the
     synthesis_id in the 200 response.
  4. Subsequent invocations carrying ``Synthesis-Id`` are routed to
     :meth:`SynthesisRuntime.execute`, which walks the plan's
     :class:`CompositionStep` entries through the same dispatcher
     every external invocation goes through. Authority is preserved.

Default policies (see :mod:`server.synthesis.policies`,
:mod:`server.synthesis.recipes`):

  * :class:`RecipeBasedPolicy` — hand-authored recipes loaded from
    ``agtp-recipes.toml`` (configured via ``[synthesis]`` in
    ``agtp-server.toml``).
  * :class:`PassthroughPolicy` — appended automatically as the final
    fallback so a proposal naming an existing method always becomes
    a one-step identity plan.
"""

from __future__ import annotations

from server.synthesis.errors import SynthesisError
from server.synthesis.plan import (
    CompositionStep,
    ParameterSource,
    SynthesisPlan,
)
from server.synthesis.policies import (
    CompositionPolicy,
    PassthroughPolicy,
)
from server.synthesis.recipes import (
    Recipe,
    RecipeBasedPolicy,
    RecipeFileError,
    RecipePattern,
    load_recipes,
)
from server.synthesis.runtime import (
    SYNTHESES,
    StepDispatcher,
    Synthesis,
    SynthesisRegistry,
    SynthesisRuntime,
    new_synthesis_id,
)


__all__ = [
    "CompositionPolicy",
    "CompositionStep",
    "ParameterSource",
    "PassthroughPolicy",
    "Recipe",
    "RecipeBasedPolicy",
    "RecipeFileError",
    "RecipePattern",
    "StepDispatcher",
    "SYNTHESES",
    "Synthesis",
    "SynthesisError",
    "SynthesisPlan",
    "SynthesisRegistry",
    "SynthesisRuntime",
    "load_recipes",
    "new_synthesis_id",
]
