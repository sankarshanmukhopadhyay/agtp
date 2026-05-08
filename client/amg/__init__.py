"""
AMG (Agent Method Grammar) — the validation + composition layer for
AGTP method declarations.

Two halves:

  * Validator (``validate``) — runs nine passes against any
    AMGMethodSpec and returns a structured ValidationResult. Wired
    into ``server.methods.register_custom`` and
    ``server.methods.handle_propose``.

  * Composer (``compose_method``, ``MethodBuilder``,
    ``compose_from_*``) — helps method authors build well-formed
    specs in the first place. Always calls the validator before
    returning, so anything you receive from the composer has passed
    the gate.

Public API::

    from client.amg import (
        # data shapes
        AMGMethodSpec, ParamSpec, SemanticBlock, SubstitutionHint,
        # validator
        validate, ValidationResult, ValidationError,
        # synthesis
        SynthesisContract, validate_synthesis,
        # substitution catalog
        find_substitutes, EquivalenceClass, DEFAULT_SUBSTITUTIONS,
        # reserved-name data
        EMBEDDED_METHODS, HTTP_METHODS, STOPLIST, is_reserved,
        # composer
        compose_method, MethodBuilder,
        compose_from_dict, compose_from_yaml, compose_from_json,
        CompositionError, suggest_fix,
        # error types
        InvalidMethodError,
        # version
        AMG_VERSION,
    )

The validator runs nine passes (lexical, reserved, semantic-class,
stoplist, required-fields, description, parameters, schemas,
substitution) and aborts on the first failure. See
``client.amg.validator`` for the full pass contract.
"""

from __future__ import annotations

from client.amg.grammar import (
    AMG_VERSION,
    ALL_ACTORS,
    ALL_CAPABILITIES,
    ALL_IMPACT_TIERS,
    ALL_SEMANTIC_CLASSES,
    ALL_SOURCES,
    AMGMethodSpec,
    IRREVERSIBLE_CONFIDENCE_FLOOR,
    PARAM_TYPES,
    PARAM_TYPES_REQUIRING_SCHEMA,
    ParamSpec,
    SEMANTIC_ACTION_INTENT,
    SEMANTIC_PROTOCOL_MECHANIC,
    SEMANTIC_QUERY_INTENT,
    SOURCE_AGTP,
    SOURCE_AMG,
    SemanticBlock,
    SubstitutionHint,
    USER_SEMANTIC_CLASSES,
)
from client.amg.reserved import (
    EMBEDDED_METHODS,
    HTTP_METHODS,
    STOPLIST,
    is_reserved,
    stoplist_suggestion,
)
from client.amg.substitution import (
    DEFAULT_SUBSTITUTIONS,
    EquivalenceClass,
    conditions_for,
    find_substitutes,
    index_by_member,
)
from client.amg.synthesis import SynthesisContract, validate_synthesis
from client.amg.validator import (
    PassResult,
    ValidationError,
    ValidationResult,
    validate,
)
from client.amg.composer import (
    CompositionError,
    MethodBuilder,
    compose_from_dict,
    compose_from_json,
    compose_from_yaml,
    compose_method,
    suggest_fix,
    validate_partial,
)


class InvalidMethodError(ValueError):
    """
    Raised when a method registration or proposal fails AMG validation.

    Inherits from ValueError so existing call sites that ``except
    ValueError`` continue to catch refused registrations. The
    ``result`` attribute exposes the full ValidationResult for
    callers that want to inspect every pass.
    """

    def __init__(
        self,
        message: str,
        *,
        result: ValidationResult,
    ) -> None:
        super().__init__(message)
        self.result = result


__all__ = [
    "AMG_VERSION",
    "ALL_ACTORS",
    "ALL_CAPABILITIES",
    "ALL_IMPACT_TIERS",
    "ALL_SEMANTIC_CLASSES",
    "ALL_SOURCES",
    "AMGMethodSpec",
    "CompositionError",
    "DEFAULT_SUBSTITUTIONS",
    "EMBEDDED_METHODS",
    "EquivalenceClass",
    "HTTP_METHODS",
    "InvalidMethodError",
    "IRREVERSIBLE_CONFIDENCE_FLOOR",
    "MethodBuilder",
    "PARAM_TYPES",
    "PARAM_TYPES_REQUIRING_SCHEMA",
    "ParamSpec",
    "PassResult",
    "SEMANTIC_ACTION_INTENT",
    "SEMANTIC_PROTOCOL_MECHANIC",
    "SEMANTIC_QUERY_INTENT",
    "SOURCE_AGTP",
    "SOURCE_AMG",
    "STOPLIST",
    "SemanticBlock",
    "SubstitutionHint",
    "SynthesisContract",
    "USER_SEMANTIC_CLASSES",
    "ValidationError",
    "ValidationResult",
    "compose_from_dict",
    "compose_from_json",
    "compose_from_yaml",
    "compose_method",
    "conditions_for",
    "find_substitutes",
    "index_by_member",
    "is_reserved",
    "stoplist_suggestion",
    "suggest_fix",
    "validate",
    "validate_partial",
    "validate_synthesis",
]
