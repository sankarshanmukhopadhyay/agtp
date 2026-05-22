"""
Recipe-based composition policy.

Server operators hand-author synthesis recipes in TOML and the
:class:`RecipeBasedPolicy` runs through them at PROPOSE time. Each
recipe declares a :class:`RecipePattern` (matching criteria) and a
plan template (the steps to execute). When a recipe matches, the
template is materialized against the actual proposal and returned to
the runtime.

The TOML format mirrors the dataclass shape closely so the recipe
file reads almost like the Python dataclasses themselves. See
``server/agtp-recipes.toml`` for the starter set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.endpoint import EndpointSpec
from server.synthesis.plan import (
    CompositionStep,
    ParameterSource,
    SynthesisPlan,
)


# ---------------------------------------------------------------------------
# Recipe + RecipePattern dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class RecipePattern:
    """
    Matching criteria for a recipe.

    All declared fields must match for the recipe to apply (logical
    AND). Fields left at None are unconstrained.

    RCNS-2 adds ``path_exact`` and ``path_regex`` for endpoint-keyed
    matching. Method-only recipes leave both unset and continue to
    match any path; endpoint-keyed recipes constrain on path so a
    single verb can route to different plans depending on the URI.
    """

    name_exact: Optional[str] = None
    name_regex: Optional[str] = None
    category: Optional[str] = None
    has_parameters: Optional[List[str]] = None
    path_exact: Optional[str] = None
    path_regex: Optional[str] = None

    def matches(self, proposal: EndpointSpec) -> bool:
        if self.name_exact is not None and proposal.name != self.name_exact:
            return False
        if self.name_regex is not None:
            if not re.match(self.name_regex, proposal.name):
                return False
        if self.category is not None and proposal.category != self.category:
            return False
        if self.has_parameters is not None:
            proposal_param_names = {p.name for p in proposal.required_params} | {
                p.name for p in proposal.optional_params
            }
            for required in self.has_parameters:
                if required not in proposal_param_names:
                    return False
        # RCNS-2: path constraint. A pattern without a path filter
        # matches any path (the legacy method-only behavior). A
        # pattern with ``path_exact`` requires byte-exact match; a
        # pattern with ``path_regex`` requires the regex to match
        # the proposal's path. Proposals without a path are treated
        # as path ``"/"`` for comparison purposes.
        if self.path_exact is not None or self.path_regex is not None:
            proposal_path = proposal.path or "/"
            if self.path_exact is not None and proposal_path != self.path_exact:
                return False
            if self.path_regex is not None and not re.match(
                self.path_regex, proposal_path
            ):
                return False
        return True


@dataclass
class Recipe:
    """A hand-authored synthesis recipe.

    The ``version`` field (RCNS-2, default ``"1"``) snapshots into the
    :class:`SynthesisPlan` at composition time. Pattern edits bump the
    version; existing contracts continue to execute against the
    captured version until expiry. This prevents an operator from
    accidentally changing the behavior of a running contract by
    editing its source recipe — the contract holds a frozen reference.
    """

    name: str
    description: str
    pattern: RecipePattern
    steps: List[CompositionStep] = field(default_factory=list)
    output_aggregation: str = "last"
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("recipe.name is required")
        if not self.steps:
            raise ValueError(f"recipe {self.name!r} must declare at least one step")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError(
                f"recipe {self.name!r} version must be a non-empty string"
            )


# ---------------------------------------------------------------------------
# RecipeBasedPolicy.
# ---------------------------------------------------------------------------


class RecipeBasedPolicy:
    """
    Composition policy backed by a list of hand-authored recipes.

    Recipes are checked in declaration order. The first recipe whose
    pattern matches the proposal AND whose underlying methods are
    all available wins; its plan template is materialized and
    returned.
    """

    name = "recipes"

    def __init__(self, recipes: List[Recipe]):
        self.recipes = list(recipes)
        # Detect duplicate names early so misconfiguration is caught
        # at construction rather than at request time.
        seen: set = set()
        for r in self.recipes:
            if r.name in seen:
                raise ValueError(
                    f"duplicate recipe name {r.name!r} in policy"
                )
            seen.add(r.name)

    def can_fulfill(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> bool:
        return any(r.pattern.matches(proposal) for r in self.recipes)

    def compose(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> Optional[SynthesisPlan]:
        available_names = {m.name for m in available_methods}
        for recipe in self.recipes:
            if not recipe.pattern.matches(proposal):
                continue
            # Every method the recipe references must exist on the server.
            referenced = [s.method_name for s in recipe.steps]
            if not all(m in available_names for m in referenced):
                continue
            # Materialize the plan. RCNS-2 snapshots the recipe's name
            # and version so an operator editing the recipe later
            # doesn't silently change the behavior of already-bound
            # contracts — they carry the captured version forward.
            return SynthesisPlan(
                proposed_method=proposal,
                steps=[_clone_step(s) for s in recipe.steps],
                output_aggregation=recipe.output_aggregation,
                description=recipe.description,
                policy_name=self.name,
                recipe_name=recipe.name,
                recipe_version=recipe.version,
            )
        return None


def _clone_step(step: CompositionStep) -> CompositionStep:
    """Defensive copy: recipes are templates and should not be mutated."""
    return CompositionStep(
        method_name=step.method_name,
        parameter_source={
            k: ParameterSource(kind=v.kind, value=v.value)
            for k, v in step.parameter_source.items()
        },
        capture_output_as=step.capture_output_as,
    )


# ---------------------------------------------------------------------------
# TOML loader.
# ---------------------------------------------------------------------------


class RecipeFileError(ValueError):
    """Raised on malformed recipe TOML so callers see a clean error."""


def load_recipes(path: Path) -> List[Recipe]:
    """
    Read ``path`` (TOML) and return the list of :class:`Recipe`
    objects. Raises :class:`RecipeFileError` on malformed input,
    with a message that names the offending recipe / field where
    possible.

    File format (illustrated for one recipe; multiple ``[[recipe]]``
    blocks are appended to the list):

    .. code-block:: toml

       [[recipe]]
       name = "evaluate-via-analyze-and-validate"
       description = "Compose EVALUATE from ANALYZE + VALIDATE."

       [recipe.pattern]
       name_exact = "EVALUATE"
       has_parameters = ["input", "ruleset"]

       [[recipe.steps]]
       method = "ANALYZE"
       capture_as = "analysis"

         [recipe.steps.parameters.input]
         kind = "proposal"
         value = "input"

       [[recipe.steps]]
       method = "VALIDATE"

         [recipe.steps.parameters.ruleset]
         kind = "proposal"
         value = "ruleset"

       [recipe.aggregation]
       mode = "last"
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Py < 3.11 fallback
        import tomli as tomllib  # type: ignore[no-redef]

    p = Path(path)
    if not p.exists():
        raise RecipeFileError(f"recipe file not found: {p}")

    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # tomllib raises TOMLDecodeError
        raise RecipeFileError(f"{p}: invalid TOML: {exc}") from exc

    raw_recipes = data.get("recipe", [])
    if not isinstance(raw_recipes, list):
        raise RecipeFileError(
            f"{p}: top-level 'recipe' must be an array of tables ([[recipe]])"
        )

    out: List[Recipe] = []
    for i, raw in enumerate(raw_recipes):
        try:
            out.append(_recipe_from_dict(raw))
        except Exception as exc:
            name = raw.get("name") if isinstance(raw, dict) else f"#{i + 1}"
            raise RecipeFileError(
                f"{p}: recipe {name!r}: {exc}"
            ) from exc
    return out


def _recipe_from_dict(raw: Dict[str, Any]) -> Recipe:
    if not isinstance(raw, dict):
        raise ValueError("entry must be a table")

    name = raw.get("name", "")
    if not isinstance(name, str) or not name:
        raise ValueError("missing or empty 'name'")

    description = str(raw.get("description", ""))

    pattern_block = raw.get("pattern", {})
    if not isinstance(pattern_block, dict):
        raise ValueError("'pattern' must be a table")
    pattern = RecipePattern(
        name_exact=pattern_block.get("name_exact"),
        name_regex=pattern_block.get("name_regex"),
        category=pattern_block.get("category"),
        has_parameters=list(pattern_block.get("has_parameters") or [])
        or None,
        path_exact=pattern_block.get("path_exact"),
        path_regex=pattern_block.get("path_regex"),
    )

    raw_steps = raw.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("'steps' must be a non-empty array of tables")
    steps: List[CompositionStep] = []
    for j, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError(f"step #{j + 1} must be a table")
        method = raw_step.get("method")
        if not method or not isinstance(method, str):
            raise ValueError(f"step #{j + 1} missing 'method'")
        param_block = raw_step.get("parameters", {})
        if not isinstance(param_block, dict):
            raise ValueError(
                f"step #{j + 1} ({method}): 'parameters' must be a table"
            )
        params: Dict[str, ParameterSource] = {}
        for target, src in param_block.items():
            if not isinstance(src, dict):
                raise ValueError(
                    f"step #{j + 1} ({method}): parameter {target!r} "
                    f"must be a table with 'kind' and 'value'"
                )
            kind = src.get("kind")
            value = src.get("value")
            if kind not in ("proposal", "constant", "previous_step"):
                raise ValueError(
                    f"step #{j + 1} ({method}): parameter {target!r} "
                    f"has invalid kind {kind!r} (expected proposal / "
                    f"constant / previous_step)"
                )
            params[target] = ParameterSource(kind=kind, value=value)
        capture_as = raw_step.get("capture_as")
        steps.append(CompositionStep(
            method_name=method.upper(),
            parameter_source=params,
            capture_output_as=capture_as,
        ))

    agg_block = raw.get("aggregation", {})
    if isinstance(agg_block, dict):
        mode = agg_block.get("mode", "last")
    else:
        mode = "last"
    if mode not in ("last", "merge", "list"):
        raise ValueError(
            f"aggregation.mode must be one of (last, merge, list); got {mode!r}"
        )

    # RCNS-2: ``version`` defaults to "1" when the recipe omits it so
    # legacy recipes (pre-versioning) load cleanly. Coerce to string so
    # numeric YAML-style values ("1", 1, "v3") survive consistently.
    raw_version = raw.get("version", "1")
    version = str(raw_version) if raw_version not in ("", None) else "1"

    return Recipe(
        name=name,
        description=description,
        pattern=pattern,
        steps=steps,
        output_aggregation=mode,
        version=version,
    )


__all__ = [
    "Recipe",
    "RecipeBasedPolicy",
    "RecipeFileError",
    "RecipePattern",
    "load_recipes",
]
