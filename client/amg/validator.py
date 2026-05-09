"""
The nine-pass AMG validator.

Each pass checks one property. Passes run in declared order; the
first failure aborts and the result reports which pass refused the
spec. This is deliberate: the order makes debugging easier (lexical
problems surface before semantic ones) and lets future passes be
inserted without renumbering callers.

Public entry point::

    result = validate(spec, known_methods=...)
    if not result.valid:
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

from client.amg.grammar import (
    ALL_SEMANTIC_CLASSES,
    ALL_SOURCES,
    AMGMethodSpec,
    PARAM_TYPES,
    PARAM_TYPES_REQUIRING_SCHEMA,
    ParamSpec,
    SEMANTIC_ACTION_INTENT,
    SEMANTIC_PROTOCOL_MECHANIC,
    SOURCE_AGTP,
    SOURCE_AMG,
    USER_SEMANTIC_CLASSES,
)
from client.amg.reserved import (
    EMBEDDED_METHODS,
    HTTP_METHODS,
    STOPLIST,
    is_reserved,
    stoplist_suggestion,
)


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass
class PassResult:
    name: str                           # e.g. "lexical", "reserved"
    passed: bool
    detail: Optional[str] = None        # explanation; populated for both


@dataclass
class ValidationError:
    pass_name: str                      # which pass refused
    code: str                           # machine-readable
    message: str                        # human-readable
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    valid: bool
    method_name: str
    passes: List[PassResult] = field(default_factory=list)
    error: Optional[ValidationError] = None
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pass implementations.
#
# Each pass returns either a populated PassResult (passed=True) or a
# ValidationError. A None return for the error field means the pass
# is satisfied; the validator then proceeds to the next pass.
# ---------------------------------------------------------------------------


_LEXICAL_RE = re.compile(r"^[A-Z]{3,32}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_STUB_PATTERNS = (
    "todo",
    "fixme",
    "stub",
    "placeholder",
    "example",
    "tbd",
    "lorem ipsum",
)

_DESCRIPTION_MIN_LENGTH = 20


def _pass_lexical(spec: AMGMethodSpec) -> Optional[ValidationError]:
    name = spec.name
    if not name:
        return ValidationError(
            pass_name="lexical",
            code="empty-name",
            message="method name is empty",
        )
    if not _LEXICAL_RE.match(name):
        return ValidationError(
            pass_name="lexical",
            code="malformed-name",
            message=(
                f"method name {name!r} does not conform to /^[A-Z]{{3,32}}$/: "
                f"must be 3-32 uppercase ASCII letters with no digits, "
                f"hyphens, underscores, or unicode"
            ),
            suggestion="Use a single uppercase verb like RECONCILE.",
        )
    return None


def _pass_reserved(spec: AMGMethodSpec) -> Optional[ValidationError]:
    upper = spec.name.upper()
    if upper in HTTP_METHODS:
        return ValidationError(
            pass_name="reserved",
            code="reserved-http-method",
            message=(
                f"{upper} conflicts with the HTTP method namespace; "
                f"AGTP and HTTP method semantics differ and must not "
                f"be conflated"
            ),
            suggestion="Choose a non-HTTP verb (e.g. FETCH, RETRIEVE).",
        )
    # source=amg/1.0 cannot register an embedded name; embedded
    # methods (source=agtp/1.0) are how the names got there.
    if spec.source == SOURCE_AMG and upper in EMBEDDED_METHODS:
        return ValidationError(
            pass_name="reserved",
            code="reserved-embedded-method",
            message=(
                f"{upper} is an AGTP embedded method; user-defined "
                f"methods (source=amg/1.0) cannot register this name"
            ),
            suggestion=f"Pick a different verb; the embedded {upper} already covers this surface.",
        )
    return None


def _pass_semantic_class(spec: AMGMethodSpec) -> Optional[ValidationError]:
    cls = spec.semantic_class
    if cls not in ALL_SEMANTIC_CLASSES:
        return ValidationError(
            pass_name="semantic-class",
            code="unknown-semantic-class",
            message=(
                f"semantic_class {cls!r} is not recognized; must be one of "
                f"{sorted(ALL_SEMANTIC_CLASSES)}"
            ),
        )
    if (
        spec.source == SOURCE_AMG
        and cls == SEMANTIC_PROTOCOL_MECHANIC
    ):
        return ValidationError(
            pass_name="semantic-class",
            code="protocol-mechanic-not-allowed",
            message=(
                "semantic_class 'protocol-mechanic' is reserved for "
                "embedded methods; user-defined methods cannot declare it"
            ),
            suggestion=(
                f"Use one of {sorted(USER_SEMANTIC_CLASSES)}."
            ),
        )
    return None


def _pass_stoplist(spec: AMGMethodSpec) -> Optional[ValidationError]:
    upper = spec.name.upper()
    if upper in STOPLIST:
        return ValidationError(
            pass_name="stoplist",
            code="non-action-intent",
            message=(
                f"{upper} appears to be a noun, adjective, or static state "
                f"rather than an action-intent verb"
            ),
            suggestion=stoplist_suggestion(upper),
        )
    return None


def _pass_required_fields(spec: AMGMethodSpec) -> Optional[ValidationError]:
    missing: List[str] = []
    if not spec.name:
        missing.append("name")
    if not spec.semantic_class:
        missing.append("semantic_class")
    if not spec.category:
        missing.append("category")
    if not spec.description:
        missing.append("description")
    if not spec.source:
        missing.append("source")

    if spec.required_params is None:
        missing.append("required_params")
    if not spec.error_codes:
        missing.append("error_codes")

    if missing:
        return ValidationError(
            pass_name="required-fields",
            code="missing-required-field",
            message=f"missing required field(s): {', '.join(sorted(set(missing)))}",
        )

    if spec.source not in ALL_SOURCES:
        return ValidationError(
            pass_name="required-fields",
            code="unknown-source",
            message=(
                f"source {spec.source!r} is not recognized; must be one of "
                f"{sorted(ALL_SOURCES)}"
            ),
        )

    if spec.source == SOURCE_AMG and not spec.namespace:
        return ValidationError(
            pass_name="required-fields",
            code="missing-namespace",
            message="user-defined methods (source=amg/1.0) require a namespace",
            suggestion="Add a namespace such as 'acme-finance'.",
        )

    if spec.source == SOURCE_AGTP and spec.namespace:
        return ValidationError(
            pass_name="required-fields",
            code="namespace-on-embedded",
            message="embedded methods (source=agtp/1.0) cannot declare a namespace",
        )

    if 422 not in spec.error_codes:
        return ValidationError(
            pass_name="required-fields",
            code="error-codes-missing-422",
            message=(
                "error_codes must include 422 (Unprocessable Entity); "
                "AMG-validated methods always emit 422 for missing or "
                "malformed parameters"
            ),
            suggestion="Add 422 to error_codes.",
        )

    return None


def _pass_description(spec: AMGMethodSpec) -> Optional[ValidationError]:
    text = (spec.description or "").strip()
    if not text:
        return ValidationError(
            pass_name="description",
            code="empty-description",
            message="description is empty",
        )
    lower = text.lower()
    for stub in _STUB_PATTERNS:
        if stub in lower:
            return ValidationError(
                pass_name="description",
                code="stub-description",
                message=(
                    f"description appears to be a stub (matched pattern "
                    f"{stub!r}): {text!r}"
                ),
                suggestion="Replace with a real one-sentence description of the method's intent.",
            )
    if len(text) < _DESCRIPTION_MIN_LENGTH:
        return ValidationError(
            pass_name="description",
            code="description-too-short",
            message=(
                f"description is {len(text)} chars; AMG requires at least "
                f"{_DESCRIPTION_MIN_LENGTH}"
            ),
            suggestion="Expand the description so a downstream agent can decide whether to invoke.",
        )
    return None


def _pass_parameters(spec: AMGMethodSpec) -> Optional[ValidationError]:
    seen: Set[str] = set()

    def _check(p: ParamSpec, role: str) -> Optional[ValidationError]:
        if not p.name:
            return ValidationError(
                pass_name="parameters",
                code="empty-param-name",
                message=f"{role} parameter has an empty name",
            )
        if not _PARAM_NAME_RE.match(p.name):
            return ValidationError(
                pass_name="parameters",
                code="malformed-param-name",
                message=(
                    f"{role} parameter name {p.name!r} must be lowercase "
                    f"snake_case (regex /^[a-z][a-z0-9_]*$/)"
                ),
            )
        if p.type not in PARAM_TYPES:
            return ValidationError(
                pass_name="parameters",
                code="unknown-param-type",
                message=(
                    f"parameter {p.name!r} has unknown type {p.type!r}; "
                    f"must be one of {sorted(PARAM_TYPES)}"
                ),
            )
        if not (p.description or "").strip():
            return ValidationError(
                pass_name="parameters",
                code="empty-param-description",
                message=f"parameter {p.name!r} has an empty description",
            )
        if p.type in PARAM_TYPES_REQUIRING_SCHEMA and p.schema is None:
            return ValidationError(
                pass_name="parameters",
                code="missing-param-schema",
                message=(
                    f"parameter {p.name!r} has type {p.type!r} but no schema; "
                    f"object/array params must declare a JSON Schema"
                ),
                suggestion=(
                    "Add a 'schema' field describing the expected shape."
                ),
            )
        if p.name in seen:
            return ValidationError(
                pass_name="parameters",
                code="duplicate-param-name",
                message=(
                    f"parameter {p.name!r} appears in both required and "
                    f"optional lists (or twice in the same list)"
                ),
            )
        seen.add(p.name)
        return None

    for p in spec.required_params or []:
        err = _check(p, "required")
        if err is not None:
            return err
    for p in spec.optional_params or []:
        err = _check(p, "optional")
        if err is not None:
            return err
    return None


def _pass_schemas(spec: AMGMethodSpec) -> Optional[ValidationError]:
    """
    Lightweight JSON Schema check.

    When the optional ``jsonschema`` library is installed we use its
    Draft7 metaschema validator. Without it, we fall back to a
    structural sanity check: each schema must be a dict that names a
    JSON Schema "type". Catalog-level CI is the place for fully
    rigorous checks.
    """
    try:
        import jsonschema  # type: ignore
        from jsonschema import Draft7Validator  # type: ignore
        rich = True
    except Exception:  # pragma: no cover - import-time only
        rich = False

    def _validate_one(schema, owner_name: str) -> Optional[ValidationError]:
        if rich:
            try:
                Draft7Validator.check_schema(schema)
            except Exception as exc:
                return ValidationError(
                    pass_name="schemas",
                    code="invalid-json-schema",
                    message=(
                        f"parameter {owner_name!r} declares a malformed JSON "
                        f"Schema: {exc}"
                    ),
                )
            return None
        if not isinstance(schema, dict):
            return ValidationError(
                pass_name="schemas",
                code="schema-not-object",
                message=(
                    f"parameter {owner_name!r} schema is not a JSON object"
                ),
            )
        if "type" not in schema and "properties" not in schema and "items" not in schema:
            return ValidationError(
                pass_name="schemas",
                code="schema-missing-type",
                message=(
                    f"parameter {owner_name!r} schema lacks a 'type', "
                    f"'properties', or 'items' field"
                ),
            )
        return None

    for p in (spec.required_params or []) + (spec.optional_params or []):
        if p.schema is None:
            continue
        err = _validate_one(p.schema, p.name)
        if err is not None:
            return err
    return None


def _pass_substitution(
    spec: AMGMethodSpec, *, known_methods: Set[str]
) -> Optional[ValidationError]:
    if not spec.substitutes_for:
        return None
    seen_targets: Set[str] = set()
    for hint in spec.substitutes_for:
        target = (hint.target_method or "").upper()
        if not target:
            return ValidationError(
                pass_name="substitution",
                code="empty-substitution-target",
                message="substitution hint has an empty target_method",
            )
        if target == spec.name.upper():
            return ValidationError(
                pass_name="substitution",
                code="self-substitution",
                message=(
                    f"{spec.name} cannot declare itself as a substitution target"
                ),
            )
        if target in seen_targets:
            return ValidationError(
                pass_name="substitution",
                code="duplicate-substitution-target",
                message=(
                    f"substitution target {target!r} is declared more than once"
                ),
            )
        seen_targets.add(target)
        if known_methods and target not in known_methods:
            return ValidationError(
                pass_name="substitution",
                code="unknown-substitution-target",
                message=(
                    f"substitution target {target!r} is not a known method"
                ),
                suggestion=(
                    "Pass --known-methods or known_methods= to extend the "
                    "validator's universe."
                ),
            )
    return None


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


# (pass_name, callable). Order matters; first failure aborts.
def _ordered_passes() -> list:
    return [
        ("lexical",         _pass_lexical),
        ("reserved",        _pass_reserved),
        ("semantic-class",  _pass_semantic_class),
        ("stoplist",        _pass_stoplist),
        ("required-fields", _pass_required_fields),
        ("description",     _pass_description),
        ("parameters",      _pass_parameters),
        ("schemas",         _pass_schemas),
        ("substitution",    None),     # special-cased to thread known_methods
    ]


def validate(
    spec: AMGMethodSpec,
    *,
    known_methods: Optional[Set[str]] = None,
) -> ValidationResult:
    """
    Run all nine passes against ``spec``.

    ``known_methods`` is the universe consulted by Pass 9
    (substitution-coherence). Defaults to the embedded methods alone;
    servers that admit custom methods should pass their full set.
    """
    if known_methods is None:
        known_methods = set(EMBEDDED_METHODS)
    else:
        known_methods = {m.upper() for m in known_methods} | set(EMBEDDED_METHODS)

    result = ValidationResult(valid=True, method_name=spec.name)

    for name, pass_fn in _ordered_passes():
        if name == "substitution":
            err = _pass_substitution(spec, known_methods=known_methods)
        else:
            err = pass_fn(spec)
        if err is None:
            result.passes.append(PassResult(
                name=name, passed=True, detail=_pass_detail_ok(name, spec),
            ))
            continue
        # First failure aborts. The result records the partial pass list
        # so callers and CLIs can show progress up to the failure.
        result.passes.append(PassResult(
            name=name, passed=False, detail=err.message,
        ))
        result.valid = False
        result.error = err
        return result

    return result


def _pass_detail_ok(name: str, spec: AMGMethodSpec) -> str:
    """Tiny human-readable summary used when a pass succeeds."""
    if name == "lexical":
        return f"name {spec.name!r} conforms to /^[A-Z]{{3,32}}$/"
    if name == "reserved":
        return "not in HTTP_METHODS or EMBEDDED_METHODS (or grandfathered embedded)"
    if name == "semantic-class":
        return spec.semantic_class
    if name == "stoplist":
        return "not on the noun/state stoplist"
    if name == "required-fields":
        return "all required fields present and well-formed"
    if name == "description":
        return f"{len(spec.description)} chars, non-stub"
    if name == "parameters":
        return (
            f"{len(spec.required_params)} required, "
            f"{len(spec.optional_params)} optional, all well-formed"
        )
    if name == "schemas":
        n = sum(
            1 for p in (spec.required_params + spec.optional_params)
            if p.schema is not None
        )
        return f"{n} schema(s) checked"
    if name == "substitution":
        return f"{len(spec.substitutes_for)} substitution hint(s)"
    return "ok"


def validate_name_only(name: str) -> Optional[ValidationError]:
    """
    Run only the name-targeted AMG passes (lexical, reserved, stoplist).

    The Method-Grammar header pathway in the server dispatcher needs
    to decide whether a method name is AMG-conformant *before* a full
    spec exists — the inbound request carries only the verb. This
    helper constructs a minimal stub spec, runs Pass 1 (lexical),
    Pass 2 (reserved), and Pass 4 (stoplist), and returns ``None``
    when all three accept the name. The first failure is returned as
    a :class:`ValidationError` so callers can map it onto a 459
    Grammar Violation response.

    Pass 3 (semantic-class) is skipped because it requires a
    declared ``semantic_class`` field that the wire request cannot
    carry. Passes 5-9 are skipped because they target body / schema
    / substitution shape that does not apply pre-dispatch.
    """
    stub = AMGMethodSpec(
        name=name,
        semantic_class=SEMANTIC_ACTION_INTENT,
        category="custom",
        description="Method-Grammar runtime validation stub",
        idempotent=False,
        state_modifying=False,
        required_params=[],
        optional_params=[],
        error_codes=[400, 422],
        source=SOURCE_AMG,
        namespace="method-grammar-stub",
    )
    for pass_fn in (_pass_lexical, _pass_reserved, _pass_stoplist):
        err = pass_fn(stub)
        if err is not None:
            return err
    return None


__all__ = [
    "PassResult",
    "ValidationError",
    "ValidationResult",
    "validate",
    "validate_name_only",
]
