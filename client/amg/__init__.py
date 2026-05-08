"""
AMG (Agent Method Grammar) — the validation layer that makes runtime
method synthesis safe.

Public API:

    from client.amg import (
        AMGMethodSpec, ParamSpec, SubstitutionHint,
        validate, ValidationResult, ValidationError,
        SynthesisContract, validate_synthesis,
        find_substitutes, EquivalenceClass, DEFAULT_SUBSTITUTIONS,
        EMBEDDED_METHODS, HTTP_METHODS, STOPLIST, is_reserved,
        InvalidMethodError,
        AMG_VERSION,
    )

The validator runs nine passes (lexical, reserved, semantic-class,
stoplist, required-fields, description, parameters, schemas,
substitution) and aborts on the first failure. See
``agtp.amg.validator`` for the full pass contract.
"""

from __future__ import annotations

from client.amg.grammar import (
    AMG_VERSION,
    ALL_SEMANTIC_CLASSES,
    ALL_SOURCES,
    AMGMethodSpec,
    PARAM_TYPES,
    PARAM_TYPES_REQUIRING_SCHEMA,
    ParamSpec,
    SEMANTIC_ACTION_INTENT,
    SEMANTIC_PROTOCOL_MECHANIC,
    SEMANTIC_QUERY_INTENT,
    SOURCE_AGTP,
    SOURCE_AMG,
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
    "ALL_SEMANTIC_CLASSES",
    "ALL_SOURCES",
    "AMGMethodSpec",
    "DEFAULT_SUBSTITUTIONS",
    "EMBEDDED_METHODS",
    "EquivalenceClass",
    "HTTP_METHODS",
    "InvalidMethodError",
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
    "SubstitutionHint",
    "SynthesisContract",
    "USER_SEMANTIC_CLASSES",
    "ValidationError",
    "ValidationResult",
    "conditions_for",
    "find_substitutes",
    "index_by_member",
    "is_reserved",
    "stoplist_suggestion",
    "validate",
    "validate_synthesis",
]
