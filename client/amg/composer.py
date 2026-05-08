"""
AMG composer.

Helps method authors construct well-formed ``AMGMethodSpec`` instances
before submitting them. The validator (``client.amg.validator``) is
the gatekeeper; the composer is the assembly tool that puts a spec
together and runs the gate before handing the spec back.

Three modes converge on the same output:

  * Function-style composition  -> ``compose_method(...)``
  * Builder pattern             -> ``MethodBuilder(name)...build()``
  * Document-form composition   -> ``compose_from_dict / _yaml / _json``

All three end with a ``validate()`` call, so any spec you receive
from the composer has passed the nine-pass grammar check. Author-side
problems (contradictory idempotency, irreversible methods with low
confidence guidance, missing semantic block on amg/1.0 methods) are
caught by the composer's own coherence layer before validation runs.
On any failure, ``CompositionError`` is raised with the underlying
``ValidationResult`` and a list of human-readable suggestions.

The composer must not duplicate validation rules. Anything the
validator can already check (lexical, reserved, stoplist, semantic
class, required fields, description quality, parameters, schemas,
substitution) is left to ``validate()``. The composer adds layers
the validator does not own:

  * coherence between the AGIS semantic block and the protocol-level
    ``idempotent`` / ``state_modifying`` flags
  * confidence-floor warnings for irreversible methods
  * AGIS field shape (actor / capability / impact_tier vocabulary)
  * "you forgot the semantic block" enforcement for source=amg/1.0
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from client.amg.grammar import (
    ALL_ACTORS,
    ALL_CAPABILITIES,
    ALL_IMPACT_TIERS,
    AMGMethodSpec,
    IRREVERSIBLE_CONFIDENCE_FLOOR,
    PARAM_TYPES,
    ParamSpec,
    SEMANTIC_ACTION_INTENT,
    SOURCE_AGTP,
    SOURCE_AMG,
    SemanticBlock,
    SubstitutionHint,
)
from client.amg.reserved import (
    HTTP_METHODS,
    STOPLIST,
    is_reserved,
    stoplist_suggestion,
)
from client.amg.substitution import (
    DEFAULT_SUBSTITUTIONS,
    find_substitutes,
)
from client.amg.validator import (
    ValidationError,
    ValidationResult,
    validate,
)


# ---------------------------------------------------------------------------
# Errors and warnings.
# ---------------------------------------------------------------------------


class CompositionError(ValueError):
    """
    Raised when composition produces an invalid spec.

    Attributes:
      * ``validation_result``  the ValidationResult from the validator
                               (when validation ran). May be None when
                               composition failed before validation.
      * ``suggestions``        list of human-readable hints for fixing
                               the composition; combines validator
                               suggestions with composer-side hints
                               from ``suggest_fix``.

    Inherits from ValueError so callers using ``except ValueError``
    keep catching it.
    """

    def __init__(
        self,
        message: str,
        *,
        validation_result: Optional[ValidationResult] = None,
        suggestions: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.validation_result = validation_result
        self.suggestions = list(suggestions or [])


# ---------------------------------------------------------------------------
# Suggestion engine.
# ---------------------------------------------------------------------------


def suggest_fix(
    validation_result: Optional[ValidationResult],
    attempted_name: str,
) -> List[str]:
    """
    Produce human-readable suggestions for fixing a failed composition.

    Looks at the failed pass and the offending name to recommend a
    concrete next step: a renamed verb, an expanded description, an
    action verb pulled from the substitution catalog. Returns an
    empty list when no specific suggestion applies; callers can fall
    back to ``validation_result.error.suggestion`` for the validator's
    own hint.
    """
    out: List[str] = []
    if validation_result is None or validation_result.valid:
        return out
    err = validation_result.error
    if err is None:
        return out
    code = err.code
    upper = (attempted_name or "").upper()

    if code == "malformed-name":
        looks_lower = attempted_name and any(c.islower() for c in attempted_name)
        if looks_lower:
            out.append(
                f"Method names must be uppercase ASCII. "
                f"Try {attempted_name.upper()!r} instead of {attempted_name!r}."
            )
        else:
            out.append(
                "Method names are 3-32 uppercase ASCII letters. "
                "No digits, hyphens, underscores, or unicode."
            )

    elif code == "reserved-http-method":
        # GET / POST / etc. Map them to action-verb suggestions.
        http_to_verb = {
            "GET":     ["FETCH", "RETRIEVE", "QUERY"],
            "POST":    ["CREATE", "SUBMIT", "PROPOSE"],
            "PUT":     ["UPDATE", "REPLACE", "STORE"],
            "DELETE":  ["REMOVE", "REVOKE", "DESTROY"],
            "PATCH":   ["UPDATE", "MODIFY", "ADJUST"],
            "HEAD":    ["DESCRIBE"],
            "OPTIONS": ["DISCOVER"],
        }
        candidates = http_to_verb.get(upper, [])
        if candidates:
            out.append(
                f"AGTP does not reuse HTTP method names. "
                f"For {upper}-style intent, consider: {', '.join(candidates)}."
            )
        else:
            out.append(
                f"{upper} is reserved as an HTTP method; choose a "
                f"non-HTTP verb."
            )

    elif code == "reserved-embedded-method":
        out.append(
            f"{upper} is one of the 12 AGTP embedded methods. "
            f"User-defined methods cannot register this name; pick a "
            f"different verb."
        )

    elif code == "non-action-intent":
        # Stoplist hit. Pull the curated hint plus any catalog matches.
        hint = stoplist_suggestion(upper)
        if hint:
            out.append(hint)
        # Catalog lookup: does any equivalence class contain the
        # offending name's neighborhood? We sweep DEFAULT_SUBSTITUTIONS
        # and offer any verb whose name shares a prefix or whose class
        # matches the implied intent.
        catalog_hits = sorted({
            m
            for ec in DEFAULT_SUBSTITUTIONS
            for m in ec.members
            if m.startswith(upper[:3]) and m not in STOPLIST
        })
        if catalog_hits:
            out.append(
                f"Catalog candidates that share intent: "
                f"{', '.join(catalog_hits)}."
            )

    elif code == "description-too-short":
        out.append(
            "Expand the description to at least 20 characters. A good "
            "description names what the method does and what calling it "
            "produces, in one sentence."
        )

    elif code == "stub-description":
        out.append(
            "Replace the placeholder description with a real one before "
            "publishing. The validator detects 'TODO', 'stub', "
            "'placeholder', and similar markers."
        )

    elif code == "missing-required-field":
        out.append(
            "Fill the missing field(s) before composing. "
            "The composer requires intent, actor, outcome at minimum."
        )

    elif code == "missing-namespace":
        out.append(
            "User-defined methods (source=amg/1.0) require a namespace "
            "such as 'acme-finance'."
        )

    elif code == "namespace-on-embedded":
        out.append(
            "Embedded methods (source=agtp/1.0) cannot declare a "
            "namespace. Drop the namespace or change source to amg/1.0."
        )

    elif code == "error-codes-missing-422":
        out.append(
            "Add 422 to error_codes. AMG-validated methods always emit "
            "422 for missing or malformed parameters."
        )

    elif code == "malformed-param-name":
        out.append(
            "Parameter names must be lowercase snake_case "
            "(/^[a-z][a-z0-9_]*$/). Rename camelCase or kebab-case "
            "params to snake_case."
        )

    elif code == "missing-param-schema":
        out.append(
            "Object and array parameters must declare a JSON Schema "
            "describing their shape."
        )

    elif code == "unknown-substitution-target":
        out.append(
            "Pass --known-methods (CLI) or known_methods= (programmatic) "
            "to extend the validator's universe of recognized methods."
        )

    return out


# ---------------------------------------------------------------------------
# Coherence checks (composer-only; the validator does not own these).
# ---------------------------------------------------------------------------


@dataclass
class _CoherenceReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _check_coherence(spec: AMGMethodSpec) -> _CoherenceReport:
    """
    Inspect cross-field invariants the validator does not own.

    Returns a report with any hard errors and any soft warnings. The
    composer raises CompositionError on hard errors; warnings are
    surfaced through the ``suggestions`` field on a successful spec
    (printed by the CLI, exposed programmatically).
    """
    report = _CoherenceReport()

    # 1. Custom (amg/1.0) methods must declare a semantic block.
    if spec.source == SOURCE_AMG and spec.semantic is None:
        report.errors.append(
            "user-defined methods (source=amg/1.0) must declare a "
            "semantic block; provide intent, actor, and outcome at minimum"
        )
        return report  # later checks need .semantic

    sb = spec.semantic
    if sb is None:
        return report  # embedded method without semantic; nothing to coherence-check

    # 2. Required AGIS fields (intent / actor / outcome). When all
    # three are empty, treat that as "no semantic block at all" so
    # the message matches the author's mental model rather than
    # listing three separate per-field errors.
    intent_empty = not (sb.intent or "").strip()
    actor_empty = not (sb.actor or "").strip()
    outcome_empty = not (sb.outcome or "").strip()
    if (
        intent_empty and actor_empty and outcome_empty
        and spec.source == SOURCE_AMG
    ):
        report.errors.append(
            "user-defined methods (source=amg/1.0) must declare a "
            "semantic block; provide intent, actor, and outcome at minimum"
        )
        return report

    if intent_empty:
        report.errors.append("semantic.intent is required and must be non-empty")
    if actor_empty:
        report.errors.append("semantic.actor is required and must be non-empty")
    elif sb.actor not in ALL_ACTORS:
        report.errors.append(
            f"semantic.actor must be one of {sorted(ALL_ACTORS)} "
            f"(got {sb.actor!r})"
        )
    if outcome_empty:
        report.errors.append(
            "semantic.outcome is required and must be non-empty"
        )

    # 3. Optional AGIS field shape.
    if sb.capability is not None and sb.capability not in ALL_CAPABILITIES:
        report.errors.append(
            f"semantic.capability must be one of {sorted(ALL_CAPABILITIES)} "
            f"(got {sb.capability!r})"
        )
    if sb.impact_tier is not None and sb.impact_tier not in ALL_IMPACT_TIERS:
        report.errors.append(
            f"semantic.impact_tier must be one of {sorted(ALL_IMPACT_TIERS)} "
            f"(got {sb.impact_tier!r})"
        )
    if sb.confidence_guidance is not None:
        if not (0.0 <= sb.confidence_guidance <= 1.0):
            report.errors.append(
                f"semantic.confidence_guidance must be in [0.0, 1.0] "
                f"(got {sb.confidence_guidance!r})"
            )

    # 4. Cross-field consistency.
    # 4a. is_idempotent + state_modifying contradiction.
    if sb.is_idempotent is True and spec.state_modifying:
        report.errors.append(
            "is_idempotent=true contradicts state_modifying=true; "
            "an idempotent method must not modify state"
        )
    # 4b. is_idempotent vs spec.idempotent.
    if sb.is_idempotent is not None and bool(sb.is_idempotent) != bool(spec.idempotent):
        report.errors.append(
            f"semantic.is_idempotent ({sb.is_idempotent!r}) disagrees "
            f"with the protocol-level idempotent flag ({spec.idempotent!r})"
        )
    # 4c. irreversible methods should declare high confidence guidance.
    if (
        sb.impact_tier == "irreversible"
        and sb.confidence_guidance is not None
        and sb.confidence_guidance < IRREVERSIBLE_CONFIDENCE_FLOOR
    ):
        report.warnings.append(
            f"impact_tier=irreversible methods typically declare "
            f"confidence_guidance >= {IRREVERSIBLE_CONFIDENCE_FLOOR}; "
            f"got {sb.confidence_guidance}"
        )

    # 5. Description / intent overlap warning.
    if (
        spec.description
        and sb.intent
        and spec.description.strip().lower() == sb.intent.strip().lower()
    ):
        report.warnings.append(
            "description matches intent verbatim; the description is "
            "expected to read slightly more technical than the intent "
            "statement (what + how vs. agent-goal voice)"
        )

    return report


# ---------------------------------------------------------------------------
# Mode 1: function-style composition.
# ---------------------------------------------------------------------------


_DEFAULT_ERROR_CODES: List[int] = [400, 405, 422]


def _coerce_param_list(
    raw: Optional[List[Union[ParamSpec, dict, str]]],
) -> List[ParamSpec]:
    out: List[ParamSpec] = []
    for entry in (raw or []):
        if isinstance(entry, ParamSpec):
            out.append(entry)
        elif isinstance(entry, dict):
            out.append(ParamSpec.from_dict(entry))
        elif isinstance(entry, str):
            out.append(ParamSpec.from_bare_name(entry))
        else:
            raise CompositionError(
                f"parameter entries must be ParamSpec | dict | str (got "
                f"{type(entry).__name__})"
            )
    return out


def compose_method(
    name: str,
    *,
    intent: str,
    actor: str,
    outcome: str,
    capability: Optional[str] = None,
    confidence_guidance: Optional[float] = None,
    impact_tier: Optional[str] = None,
    is_idempotent: Optional[bool] = None,
    state_transition: Optional[Dict[str, str]] = None,
    description: Optional[str] = None,
    category: str = "transact",
    semantic_class: str = SEMANTIC_ACTION_INTENT,
    required_params: Optional[List[Union[ParamSpec, dict, str]]] = None,
    optional_params: Optional[List[Union[ParamSpec, dict, str]]] = None,
    error_codes: Optional[List[int]] = None,
    source: str = SOURCE_AMG,
    namespace: Optional[str] = None,
    substitutes_for: Optional[List[Union[SubstitutionHint, dict]]] = None,
    state_modifying: Optional[bool] = None,
    idempotent: Optional[bool] = None,
    known_methods: Optional[Set[str]] = None,
) -> AMGMethodSpec:
    """
    Compose a well-formed AMG method specification.

    Builds an AMGMethodSpec from the provided fields, runs full AMG
    validation, and returns the spec on success. Raises
    CompositionError with field-level details on validation failure.

    Defaults applied here when the caller doesn't supply them:

      * ``description``   defaults to ``intent`` (intent voice and
                          description voice usually say the same
                          thing)
      * ``error_codes``   defaults to ``[400, 405, 422]``
      * ``idempotent``    defaults to the AGIS ``is_idempotent`` value
                          when supplied, else False
      * ``state_modifying`` defaults to the inverse of ``is_idempotent``
                          when supplied, else False; an idempotent
                          method is presumed not to mutate state
    """
    # Coerce params first so downstream validation sees ParamSpecs.
    req_specs = _coerce_param_list(required_params)
    opt_specs = _coerce_param_list(optional_params)

    # Coerce substitutions.
    subs: List[SubstitutionHint] = []
    for s in (substitutes_for or []):
        if isinstance(s, SubstitutionHint):
            subs.append(s)
        elif isinstance(s, dict):
            subs.append(SubstitutionHint.from_dict(s))
        else:
            raise CompositionError(
                f"substitutes_for entries must be SubstitutionHint | dict "
                f"(got {type(s).__name__})"
            )

    # Defaults: description follows intent if absent.
    final_description = (description if description is not None else intent) or ""

    # Defaults: error_codes baseline.
    final_error_codes = list(error_codes) if error_codes else list(_DEFAULT_ERROR_CODES)

    # Idempotency / state-modifying defaults derive from is_idempotent
    # when available, so authors only have to think about one notion.
    if idempotent is None:
        idempotent_final = bool(is_idempotent) if is_idempotent is not None else False
    else:
        idempotent_final = bool(idempotent)
    if state_modifying is None:
        if is_idempotent is True:
            state_modifying_final = False
        elif is_idempotent is False:
            state_modifying_final = True
        else:
            state_modifying_final = False
    else:
        state_modifying_final = bool(state_modifying)

    semantic = SemanticBlock(
        intent=intent or "",
        actor=actor or "",
        outcome=outcome or "",
        capability=capability,
        confidence_guidance=confidence_guidance,
        impact_tier=impact_tier,
        is_idempotent=is_idempotent,
        state_transition=state_transition,
    )

    spec = AMGMethodSpec(
        # Pass the name through as-is. Auto-uppercasing would silently
        # accept ``compose_method("reconcile", ...)`` when the user
        # really wanted a Pass 1 lexical refusal that the suggestion
        # engine could turn into an actionable hint.
        name=name or "",
        semantic_class=semantic_class,
        category=category,
        description=final_description,
        idempotent=idempotent_final,
        state_modifying=state_modifying_final,
        required_params=req_specs,
        optional_params=opt_specs,
        error_codes=final_error_codes,
        source=source,
        namespace=namespace,
        substitutes_for=subs,
        semantic=semantic,
    )

    # Composer's own coherence pass first. It owns rules the validator
    # does not (semantic-block consistency, idempotency contradictions).
    report = _check_coherence(spec)
    if report.errors:
        raise CompositionError(
            f"composer rejected {spec.name!r}: " + "; ".join(report.errors),
            validation_result=None,
            suggestions=report.warnings,
        )

    # Then the validator's nine-pass grammar check.
    result = validate(spec, known_methods=known_methods)
    if not result.valid:
        suggestions = suggest_fix(result, spec.name) + report.warnings
        if result.error and result.error.suggestion:
            suggestions.insert(0, result.error.suggestion)
        raise CompositionError(
            f"AMG validation refused {spec.name!r} at pass "
            f"'{result.error.pass_name}' [{result.error.code}]: "
            f"{result.error.message}",
            validation_result=result,
            suggestions=suggestions,
        )

    # Stash any composer warnings on the spec for callers to surface.
    # We do this via a private attribute so the dataclass shape stays
    # untouched. Callers ignore the attribute when they don't care.
    if report.warnings:
        spec.__dict__["_composer_warnings"] = list(report.warnings)
    return spec


# ---------------------------------------------------------------------------
# Mode 2: builder pattern.
# ---------------------------------------------------------------------------


class MethodBuilder:
    """
    Fluent builder for AMG method specs.

    Each ``with_*`` call returns ``self`` so chains read naturally.
    ``build()`` runs the composer's coherence checks plus the full
    nine-pass validator and returns an ``AMGMethodSpec`` (or raises
    ``CompositionError``). ``preview()`` returns the in-progress spec
    without running validation; useful for incremental UIs.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._intent: Optional[str] = None
        self._actor: Optional[str] = None
        self._outcome: Optional[str] = None
        self._capability: Optional[str] = None
        self._confidence_guidance: Optional[float] = None
        self._impact_tier: Optional[str] = None
        self._is_idempotent: Optional[bool] = None
        self._state_transition: Optional[Dict[str, str]] = None
        self._description: Optional[str] = None
        self._category: str = "transact"
        self._semantic_class: str = SEMANTIC_ACTION_INTENT
        self._required_params: List[ParamSpec] = []
        self._optional_params: List[ParamSpec] = []
        self._error_codes: List[int] = []
        self._source: str = SOURCE_AMG
        self._namespace: Optional[str] = None
        self._substitutions: List[SubstitutionHint] = []
        self._idempotent_override: Optional[bool] = None
        self._state_modifying_override: Optional[bool] = None

    # ---- AGIS semantic block ----

    def with_intent(self, intent: str) -> "MethodBuilder":
        self._intent = intent
        return self

    def with_actor(self, actor: str) -> "MethodBuilder":
        self._actor = actor
        return self

    def with_outcome(self, outcome: str) -> "MethodBuilder":
        self._outcome = outcome
        return self

    def with_capability(self, capability: str) -> "MethodBuilder":
        self._capability = capability
        return self

    def with_confidence_guidance(self, value: float) -> "MethodBuilder":
        self._confidence_guidance = float(value)
        return self

    def with_impact_tier(self, tier: str) -> "MethodBuilder":
        self._impact_tier = tier
        return self

    def with_idempotent(self, value: bool) -> "MethodBuilder":
        self._is_idempotent = bool(value)
        return self

    def with_state_transition(self, mapping: Dict[str, str]) -> "MethodBuilder":
        self._state_transition = dict(mapping)
        return self

    # ---- protocol-level fields ----

    def with_description(self, description: str) -> "MethodBuilder":
        self._description = description
        return self

    def with_category(self, category: str) -> "MethodBuilder":
        self._category = category
        return self

    def with_semantic_class(self, semantic_class: str) -> "MethodBuilder":
        self._semantic_class = semantic_class
        return self

    def with_source(self, source: str) -> "MethodBuilder":
        self._source = source
        return self

    def with_namespace(self, namespace: str) -> "MethodBuilder":
        self._namespace = namespace
        return self

    def with_required_param(
        self,
        name: str,
        type_: str,
        description: str,
        *,
        schema: Optional[dict] = None,
    ) -> "MethodBuilder":
        self._required_params.append(
            ParamSpec(name=name, type=type_, description=description, schema=schema)
        )
        return self

    def with_optional_param(
        self,
        name: str,
        type_: str,
        description: str,
        *,
        schema: Optional[dict] = None,
    ) -> "MethodBuilder":
        self._optional_params.append(
            ParamSpec(name=name, type=type_, description=description, schema=schema)
        )
        return self

    def with_error_code(self, code: int) -> "MethodBuilder":
        if code not in self._error_codes:
            self._error_codes.append(int(code))
        return self

    def with_substitution(
        self,
        target: str,
        conditions: Optional[str] = None,
    ) -> "MethodBuilder":
        self._substitutions.append(
            SubstitutionHint(target_method=target, conditions=conditions)
        )
        return self

    # ---- escape hatches for protocol-level overrides ----

    def with_protocol_idempotent(self, value: bool) -> "MethodBuilder":
        """Override the protocol-level ``idempotent`` flag explicitly.
        Most authors should set this via ``with_idempotent`` (the AGIS
        semantic field) and let the composer derive the protocol flag."""
        self._idempotent_override = bool(value)
        return self

    def with_protocol_state_modifying(self, value: bool) -> "MethodBuilder":
        """Override the protocol-level ``state_modifying`` flag."""
        self._state_modifying_override = bool(value)
        return self

    # ---- terminal operations ----

    def build(
        self,
        *,
        known_methods: Optional[Set[str]] = None,
    ) -> AMGMethodSpec:
        """Run validation and return an AMGMethodSpec, or raise."""
        return compose_method(
            self._name,
            intent=self._intent or "",
            actor=self._actor or "",
            outcome=self._outcome or "",
            capability=self._capability,
            confidence_guidance=self._confidence_guidance,
            impact_tier=self._impact_tier,
            is_idempotent=self._is_idempotent,
            state_transition=self._state_transition,
            description=self._description,
            category=self._category,
            semantic_class=self._semantic_class,
            required_params=list(self._required_params),
            optional_params=list(self._optional_params),
            error_codes=list(self._error_codes) if self._error_codes else None,
            source=self._source,
            namespace=self._namespace,
            substitutes_for=list(self._substitutions),
            state_modifying=self._state_modifying_override,
            idempotent=self._idempotent_override,
            known_methods=known_methods,
        )

    def preview(self) -> AMGMethodSpec:
        """
        Return the in-progress spec WITHOUT running validation.

        Useful for incremental UIs that want to render a draft as the
        user fills it in. The returned spec may not validate; do not
        publish it without calling ``build()``.
        """
        sb = SemanticBlock(
            intent=self._intent or "",
            actor=self._actor or "",
            outcome=self._outcome or "",
            capability=self._capability,
            confidence_guidance=self._confidence_guidance,
            impact_tier=self._impact_tier,
            is_idempotent=self._is_idempotent,
            state_transition=self._state_transition,
        )
        idempotent = (
            self._idempotent_override
            if self._idempotent_override is not None
            else bool(self._is_idempotent) if self._is_idempotent is not None
            else False
        )
        state_modifying = (
            self._state_modifying_override
            if self._state_modifying_override is not None
            else (not bool(self._is_idempotent)) if self._is_idempotent is not None
            else False
        )
        return AMGMethodSpec(
            name=self._name,
            semantic_class=self._semantic_class,
            category=self._category,
            description=self._description or self._intent or "",
            idempotent=idempotent,
            state_modifying=state_modifying,
            required_params=list(self._required_params),
            optional_params=list(self._optional_params),
            error_codes=list(self._error_codes) if self._error_codes else list(_DEFAULT_ERROR_CODES),
            source=self._source,
            namespace=self._namespace,
            substitutes_for=list(self._substitutions),
            semantic=sb,
        )


# ---------------------------------------------------------------------------
# Mode 3: document-form composition.
# ---------------------------------------------------------------------------


def compose_from_dict(
    data: Dict[str, Any],
    *,
    known_methods: Optional[Set[str]] = None,
) -> AMGMethodSpec:
    """
    Compose a spec from a parsed dict (typically loaded from YAML or
    JSON). Top-level keys mirror ``AMGMethodSpec`` fields; the AGIS
    semantic block lives under ``semantic``.
    """
    if not isinstance(data, dict):
        raise CompositionError(
            f"compose_from_dict expected a mapping, got {type(data).__name__}"
        )
    sem = data.get("semantic") or {}
    if not isinstance(sem, dict):
        raise CompositionError(
            f"'semantic' must be a mapping, got {type(sem).__name__}"
        )

    # Substitution shapes: list of {target, conditions?} or {target_method, conditions?}.
    raw_subs = data.get("substitutes_for") or []
    subs: List[SubstitutionHint] = []
    for s in raw_subs:
        if isinstance(s, dict):
            target = s.get("target_method") or s.get("target") or ""
            subs.append(SubstitutionHint(
                target_method=str(target),
                conditions=s.get("conditions"),
            ))
        elif isinstance(s, str):
            subs.append(SubstitutionHint(target_method=s))
        else:
            raise CompositionError(
                f"substitutes_for entries must be dict | str (got "
                f"{type(s).__name__})"
            )

    return compose_method(
        str(data.get("name", "")),
        intent=str(sem.get("intent", "")),
        actor=str(sem.get("actor", "")),
        outcome=str(sem.get("outcome", "")),
        capability=sem.get("capability"),
        confidence_guidance=(
            float(sem["confidence_guidance"])
            if sem.get("confidence_guidance") is not None else None
        ),
        impact_tier=sem.get("impact_tier"),
        is_idempotent=(
            bool(sem["is_idempotent"])
            if sem.get("is_idempotent") is not None else None
        ),
        state_transition=sem.get("state_transition"),
        description=data.get("description"),
        category=str(data.get("category", "transact")),
        semantic_class=str(data.get("semantic_class", SEMANTIC_ACTION_INTENT)),
        required_params=data.get("required_params"),
        optional_params=data.get("optional_params"),
        error_codes=data.get("error_codes"),
        source=str(data.get("source", SOURCE_AMG)),
        namespace=data.get("namespace"),
        substitutes_for=subs,
        state_modifying=data.get("state_modifying"),
        idempotent=data.get("idempotent"),
        known_methods=known_methods,
    )


def compose_from_json(
    path: Union[str, Path],
    *,
    known_methods: Optional[Set[str]] = None,
) -> AMGMethodSpec:
    """Load a ``*.method.json`` file and compose the spec."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompositionError(f"could not read {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CompositionError(f"{p}: invalid JSON: {exc}") from exc
    return compose_from_dict(data, known_methods=known_methods)


def compose_from_yaml(
    path: Union[str, Path],
    *,
    known_methods: Optional[Set[str]] = None,
) -> AMGMethodSpec:
    """
    Load a ``*.method.yaml`` file and compose the spec.

    Requires the optional ``pyyaml`` dependency. Install with::

        pip install -e ".[yaml]"

    or simply ``pip install pyyaml``. Without PyYAML this function
    raises ImportError with the install hint.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "compose_from_yaml requires PyYAML. Install with "
            "'pip install pyyaml' or 'pip install -e \".[yaml]\"'"
        ) from exc
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompositionError(f"could not read {p}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CompositionError(f"{p}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CompositionError(
            f"{p}: top-level YAML value must be a mapping"
        )
    return compose_from_dict(data, known_methods=known_methods)


# ---------------------------------------------------------------------------
# Partial validation (UI-friendly).
#
# ``validate_partial`` is the entry point used by the Elemen Compose
# drawer (and any other UI that wants per-field feedback while the
# author is still typing). Unlike ``compose_method``, it does not
# require a complete spec — fields that are absent or empty are
# skipped, not rejected, so the UI can call it on every keystroke
# without spamming the user with "this field is required" errors
# they have not yet had a chance to fill.
#
# The output is keyed by field path so the UI can render feedback
# directly under the relevant input. Field paths use dot notation:
#
#     name                          -> the method name input
#     description                   -> the description textarea
#     semantic.intent               -> the intent textarea
#     semantic.actor                -> the actor radios
#     semantic.outcome              -> the outcome textarea
#     semantic.capability           -> the capability dropdown
#     semantic.confidence_guidance  -> the confidence slider
#     semantic.impact_tier          -> the impact-tier toggle
#     semantic.is_idempotent        -> the idempotent checkbox
#     required_params[i].{name,type,description,schema}
#     optional_params[i].{...}
#     namespace                     -> the namespace input
#     error_codes                   -> the multi-select chips
#     substitutes_for[i].{target,conditions}
# ---------------------------------------------------------------------------


# Section -> ordered list of field paths that belong to it. Used by
# the completion summary and by the UI to scroll-to-field on warning
# clicks. Paths under an indexed list (e.g. required_params[0].name)
# match the prefix entries here.
_PARTIAL_SECTIONS: Dict[str, List[str]] = {
    "identity": ["name", "description"],
    "semantic": [
        "semantic.intent",
        "semantic.actor",
        "semantic.outcome",
        "semantic.capability",
        "semantic.confidence_guidance",
        "semantic.impact_tier",
        "semantic.is_idempotent",
    ],
    "parameters": ["required_params", "optional_params"],
    "authority": ["source", "namespace", "error_codes"],
    "substitution": ["substitutes_for"],
}


def _present(value: Any) -> bool:
    """Return True when a draft field has user-supplied content."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _check_name_partial(name: str) -> Optional[str]:
    """Run lexical / reserved / stoplist checks against a name draft."""
    import re
    if not re.match(r"^[A-Z]{3,32}$", name):
        if name and name.isalpha():
            return (
                f"{name!r} is not valid; method names must be 3-32 "
                f"uppercase ASCII letters (try {name.upper()!r})"
            )
        return (
            f"{name!r} is not valid; use 3-32 uppercase ASCII letters "
            f"with no digits, hyphens, underscores, or unicode"
        )
    if name in HTTP_METHODS:
        return f"{name!r} is reserved as an HTTP method"
    from client.amg.reserved import EMBEDDED_METHODS
    if name in EMBEDDED_METHODS:
        return (
            f"{name!r} is one of the 12 embedded AGTP methods and "
            f"cannot be redefined"
        )
    if name in STOPLIST:
        return (
            f"{name!r} is in the AMG stoplist (describes a state, "
            f"not an action)"
        )
    return None


def validate_partial(draft: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate a partial composition draft with per-field feedback.

    Unlike :func:`compose_method`, missing fields do not raise — they
    simply do not contribute errors. This is the contract the UI
    relies on while the author is still typing.

    Returns a dict with the shape::

        {
          "valid": bool,
          "errors": {field_path: error_message, ...},
          "warnings": {field_path: warning_message, ...},
          "completion": {section_name: status, ...},
        }

    where ``status`` is one of ``"untouched"``, ``"partial"``,
    ``"complete"``. ``valid`` is True when every populated field
    passes its individual checks AND the cross-field coherence layer
    raises no errors (warnings do not flip the flag).
    """
    if not isinstance(draft, dict):
        return {
            "valid": False,
            "errors": {"_root": "draft must be an object"},
            "warnings": {},
            "completion": {s: "untouched" for s in _PARTIAL_SECTIONS},
        }

    errors: Dict[str, str] = {}
    warnings: Dict[str, str] = {}

    name = (draft.get("name") or "").strip()
    if _present(name):
        msg = _check_name_partial(name)
        if msg:
            errors["name"] = msg

    description = (draft.get("description") or "").strip()
    if _present(description) and len(description) < 20:
        errors["description"] = (
            f"description is {len(description)} chars; aim for at "
            f"least 20"
        )

    sb = draft.get("semantic") or {}
    if not isinstance(sb, dict):
        errors["semantic"] = "semantic block must be an object"
        sb = {}

    intent = (sb.get("intent") or "").strip()
    if _present(intent) and len(intent) < 20:
        errors["semantic.intent"] = (
            f"intent is {len(intent)} chars; aim for at least 20"
        )

    actor = (sb.get("actor") or "").strip()
    if _present(actor) and actor not in ALL_ACTORS:
        errors["semantic.actor"] = (
            f"actor must be one of {sorted(ALL_ACTORS)} (got {actor!r})"
        )

    outcome = (sb.get("outcome") or "").strip()
    if _present(outcome) and len(outcome) < 20:
        errors["semantic.outcome"] = (
            f"outcome is {len(outcome)} chars; aim for at least 20"
        )

    capability = (sb.get("capability") or "").strip() or None
    if capability is not None and capability not in ALL_CAPABILITIES:
        errors["semantic.capability"] = (
            f"capability must be one of {sorted(ALL_CAPABILITIES)} "
            f"(got {capability!r})"
        )

    impact_tier = (sb.get("impact_tier") or "").strip() or None
    if impact_tier is not None and impact_tier not in ALL_IMPACT_TIERS:
        errors["semantic.impact_tier"] = (
            f"impact_tier must be one of {sorted(ALL_IMPACT_TIERS)} "
            f"(got {impact_tier!r})"
        )

    confidence = sb.get("confidence_guidance")
    if confidence is not None and confidence != "":
        try:
            cg = float(confidence)
        except (TypeError, ValueError):
            errors["semantic.confidence_guidance"] = (
                f"confidence_guidance must be a number (got {confidence!r})"
            )
            cg = None
        else:
            if not (0.0 <= cg <= 1.0):
                errors["semantic.confidence_guidance"] = (
                    f"confidence_guidance must be in [0.0, 1.0] (got {cg})"
                )
        if (
            cg is not None
            and impact_tier == "irreversible"
            and cg < IRREVERSIBLE_CONFIDENCE_FLOOR
        ):
            warnings["semantic.confidence_guidance"] = (
                f"impact_tier=irreversible recommends "
                f"confidence_guidance >= {IRREVERSIBLE_CONFIDENCE_FLOOR}; "
                f"got {cg}"
            )

    # Description / intent overlap warning, mirroring the composer's
    # coherence layer.
    if (
        description
        and intent
        and description.lower() == intent.lower()
    ):
        warnings["description"] = (
            "description matches intent verbatim; describe the "
            "implementation (what + how) rather than the goal"
        )

    # Parameters: validate each populated row. Empty rows are skipped.
    for key in ("required_params", "optional_params"):
        rows = draft.get(key) or []
        if not isinstance(rows, list):
            errors[key] = f"{key} must be a list"
            continue
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                errors[f"{key}[{i}]"] = "parameter row must be an object"
                continue
            pname = (row.get("name") or "").strip()
            ptype = (row.get("type") or "").strip()
            pdesc = (row.get("description") or "").strip()
            if not _present(pname) and not _present(ptype) and not _present(pdesc):
                continue  # blank row; nothing to flag yet
            if pname:
                import re
                if not re.match(r"^[a-z][a-z0-9_]*$", pname):
                    errors[f"{key}[{i}].name"] = (
                        f"parameter name {pname!r} must be lowercase "
                        f"snake_case"
                    )
            else:
                errors[f"{key}[{i}].name"] = "parameter name is required"
            if ptype:
                if ptype not in PARAM_TYPES:
                    errors[f"{key}[{i}].type"] = (
                        f"parameter type must be one of "
                        f"{sorted(PARAM_TYPES)} (got {ptype!r})"
                    )
                elif ptype in ("object", "array") and not row.get("schema"):
                    errors[f"{key}[{i}].schema"] = (
                        f"{ptype!r} parameters must declare a JSON schema"
                    )
            if not pdesc:
                errors[f"{key}[{i}].description"] = (
                    "parameter description is required"
                )

    # Authority section.
    namespace = (draft.get("namespace") or "").strip()
    source = (draft.get("source") or "amg/1.0").strip()
    if source == SOURCE_AMG and _present(namespace):
        # Lower-snake guidance is a soft check (warning, not error).
        import re
        if not re.match(r"^[a-z][a-z0-9-]*$", namespace):
            warnings["namespace"] = (
                "namespace is conventionally lowercase with hyphens "
                "(e.g., acme-finance)"
            )
    elif source == SOURCE_AMG and not _present(namespace):
        # Required by the validator, but only flag once the user has
        # started filling the form — namespace alone shouldn't error
        # on a brand-new draft.
        if any(_present(draft.get(k)) for k in ("name", "semantic")):
            errors["namespace"] = (
                "amg/1.0 methods must declare a namespace"
            )

    error_codes = draft.get("error_codes")
    if _present(error_codes):
        if not isinstance(error_codes, list):
            errors["error_codes"] = "error_codes must be a list of integers"
        else:
            for i, code in enumerate(error_codes):
                try:
                    int(code)
                except (TypeError, ValueError):
                    errors[f"error_codes[{i}]"] = (
                        f"error code {code!r} must be an integer"
                    )
            if 422 not in [int(c) for c in error_codes if _is_int_like(c)]:
                warnings["error_codes"] = (
                    "error_codes should include 422 (validation refused)"
                )

    # Substitutes-for: each entry should at least name a target.
    subs = draft.get("substitutes_for") or []
    if isinstance(subs, list):
        for i, entry in enumerate(subs):
            if not isinstance(entry, dict):
                errors[f"substitutes_for[{i}]"] = (
                    "substitution entry must be an object"
                )
                continue
            target = (entry.get("target") or entry.get("target_method") or "").strip()
            if not _present(target):
                errors[f"substitutes_for[{i}].target"] = (
                    "substitution target is required"
                )

    completion = _completion_summary(draft, errors)
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "completion": completion,
    }


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _completion_summary(
    draft: Dict[str, Any],
    errors: Dict[str, str],
) -> Dict[str, str]:
    """Per-section completion: untouched / partial / complete."""
    result: Dict[str, str] = {}
    for section, fields in _PARTIAL_SECTIONS.items():
        present = 0
        total = len(fields)
        for path in fields:
            value = _walk_path(draft, path)
            if _present(value):
                present += 1
        section_has_error = any(
            err_path == section
            or err_path.startswith(f"{section}.")
            or any(err_path == p or err_path.startswith(f"{p}.") or err_path.startswith(f"{p}[")
                   for p in fields)
            for err_path in errors
        )
        if present == 0:
            result[section] = "untouched"
        elif section_has_error or present < total:
            result[section] = "partial"
        else:
            result[section] = "complete"
    return result


def _walk_path(obj: Any, path: str) -> Any:
    """Resolve a dotted field path against the draft dict."""
    cur: Any = obj
    for segment in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(segment)
    return cur


__all__ = [
    "CompositionError",
    "MethodBuilder",
    "compose_from_dict",
    "compose_from_json",
    "compose_from_yaml",
    "compose_method",
    "suggest_fix",
    "validate_partial",
]
