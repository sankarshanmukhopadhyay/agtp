"""
AMG grammar specification.

Two halves:

  1. ``AMGMethodSpec`` and friends. The dataclass mirror of the wire
     format a method declaration takes when published to a server,
     a catalog, or a PROPOSE body. AMG validates against this shape;
     callers convert from looser internal forms (e.g. the runtime
     ``MethodSpec`` in agtp.methods) before validation.

  2. The recognized semantic classes. Three are admitted; one is
     embedded-only.

Nothing in this module side-effects. Construction does basic type
checks; full validation lives in ``agtp.amg.validator``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


AMG_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Semantic classes.
# ---------------------------------------------------------------------------


# Canonical agent-method class. Any verb that expresses what the agent
# wants to happen on the server side.
SEMANTIC_ACTION_INTENT = "action-intent"

# Read-only perception verbs. Subset of action-intent that is
# guaranteed not to mutate state. Reserved for cognitive perception.
SEMANTIC_QUERY_INTENT = "query-intent"

# Internal protocol verbs (DELEGATE / ESCALATE / CONFIRM / SUSPEND /
# PROPOSE / NOTIFY). Embedded methods only; user-defined methods that
# declare this class are rejected at Pass 3.
SEMANTIC_PROTOCOL_MECHANIC = "protocol-mechanic"

ALL_SEMANTIC_CLASSES = frozenset({
    SEMANTIC_ACTION_INTENT,
    SEMANTIC_QUERY_INTENT,
    SEMANTIC_PROTOCOL_MECHANIC,
})

# Classes that user-defined (source=amg/1.0) methods are allowed to
# declare. ``protocol-mechanic`` is excluded by design.
USER_SEMANTIC_CLASSES = frozenset({
    SEMANTIC_ACTION_INTENT,
    SEMANTIC_QUERY_INTENT,
})


# ---------------------------------------------------------------------------
# Recognized parameter types.
# ---------------------------------------------------------------------------


PARAM_TYPES = frozenset({
    "string", "integer", "number", "boolean", "object", "array",
})

# Types that must carry a JSON Schema in their ParamSpec.schema field.
PARAM_TYPES_REQUIRING_SCHEMA = frozenset({"object", "array"})


# ---------------------------------------------------------------------------
# Source and namespace conventions.
# ---------------------------------------------------------------------------


SOURCE_AGTP = "agtp/1.0"
SOURCE_AMG = "amg/1.0"
ALL_SOURCES = frozenset({SOURCE_AGTP, SOURCE_AMG})


# ---------------------------------------------------------------------------
# Dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class ParamSpec:
    """
    A single parameter declaration. Names are lowercase snake_case;
    types come from PARAM_TYPES; descriptions are non-empty. Object
    and array types must carry a JSON Schema in ``schema``.
    """

    name: str
    type: str
    description: str
    schema: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }
        if self.schema is not None:
            out["schema"] = dict(self.schema)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParamSpec":
        return cls(
            name=str(data.get("name", "")),
            type=str(data.get("type", "string")),
            description=str(data.get("description", "")),
            schema=data.get("schema"),
        )

    @classmethod
    def from_bare_name(cls, name: str) -> "ParamSpec":
        """Promote a bare parameter name (legacy List[str] form) to
        a ParamSpec with safe defaults. Used by integration shims."""
        return cls(
            name=str(name),
            type="string",
            description=f"parameter '{name}'",
        )


@dataclass
class SubstitutionHint:
    """
    Declares that this method may stand in for ``target_method`` in
    appropriate contexts. ``conditions`` is free-form prose and lets
    the substitution surface its applicability constraints.
    """

    target_method: str
    conditions: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"target_method": self.target_method}
        if self.conditions:
            out["conditions"] = self.conditions
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubstitutionHint":
        return cls(
            target_method=str(data.get("target_method", "")),
            conditions=data.get("conditions"),
        )


@dataclass
class AMGMethodSpec:
    """
    The wire form of a method declaration that AMG validates.

    Fields mirror the published catalog shape so a CI step or a
    server's ``register_custom`` path can pass the spec straight into
    ``validate()``.
    """

    name: str
    semantic_class: str
    category: str
    description: str
    idempotent: bool
    state_modifying: bool
    required_params: List[ParamSpec]
    optional_params: List[ParamSpec]
    error_codes: List[int]
    source: str
    namespace: Optional[str] = None
    substitutes_for: List[SubstitutionHint] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "semantic_class": self.semantic_class,
            "category": self.category,
            "description": self.description,
            "idempotent": bool(self.idempotent),
            "state_modifying": bool(self.state_modifying),
            "required_params": [p.to_dict() for p in self.required_params],
            "optional_params": [p.to_dict() for p in self.optional_params],
            "error_codes": list(self.error_codes),
            "source": self.source,
        }
        if self.namespace:
            out["namespace"] = self.namespace
        if self.substitutes_for:
            out["substitutes_for"] = [
                s.to_dict() for s in self.substitutes_for
            ]
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AMGMethodSpec":
        """
        Construct from a JSON dict. Tolerates two parameter shapes:

          * Rich:  list of ``{name, type, description, schema?}`` objects
          * Legacy: list of bare names (strings); each promoted to a
                    ParamSpec with safe defaults
        """
        def _coerce_params(raw) -> List[ParamSpec]:
            specs: List[ParamSpec] = []
            for entry in (raw or []):
                if isinstance(entry, str):
                    specs.append(ParamSpec.from_bare_name(entry))
                elif isinstance(entry, dict):
                    specs.append(ParamSpec.from_dict(entry))
                else:
                    raise TypeError(
                        f"parameter entry must be str or dict, got "
                        f"{type(entry).__name__}: {entry!r}"
                    )
            return specs

        substitutes = [
            SubstitutionHint.from_dict(s) if isinstance(s, dict)
            else SubstitutionHint(target_method=str(s))
            for s in (data.get("substitutes_for") or [])
        ]

        return cls(
            name=str(data.get("name", "")),
            semantic_class=str(data.get("semantic_class", SEMANTIC_ACTION_INTENT)),
            category=str(data.get("category", "")),
            description=str(data.get("description", "")),
            idempotent=bool(data.get("idempotent", False)),
            state_modifying=bool(data.get("state_modifying", False)),
            required_params=_coerce_params(data.get("required_params")),
            optional_params=_coerce_params(data.get("optional_params")),
            error_codes=list(data.get("error_codes", [])),
            source=str(data.get("source", SOURCE_AMG)),
            namespace=data.get("namespace"),
            substitutes_for=substitutes,
        )

    @classmethod
    def from_proposal(cls, proposal: Dict[str, Any]) -> "AMGMethodSpec":
        """
        Build a spec from a runtime PROPOSE body.

        Proposals carry less metadata than published catalog entries:
        clients typically only ship ``name``, ``parameters``, and
        ``outcome``. The validator still gets a complete spec so the
        relevant passes (lexical, reserved, stoplist, semantic class)
        run; absent fields take conservative defaults.
        """
        name = str(proposal.get("name", "")).upper()
        raw_params = proposal.get("parameters") or {}
        required: List[ParamSpec] = []
        if isinstance(raw_params, dict):
            for pname, ptype in raw_params.items():
                type_str = ptype if isinstance(ptype, str) else "string"
                if type_str not in PARAM_TYPES:
                    type_str = "string"
                required.append(
                    ParamSpec(
                        name=str(pname),
                        type=type_str,
                        description=f"parameter '{pname}' from proposal",
                    )
                )
        elif isinstance(raw_params, list):
            for entry in raw_params:
                if isinstance(entry, dict):
                    required.append(ParamSpec.from_dict(entry))
                elif isinstance(entry, str):
                    required.append(ParamSpec.from_bare_name(entry))

        description = str(
            proposal.get("description")
            or f"runtime proposal: {name or '(unnamed)'}"
        )

        return cls(
            name=name,
            semantic_class=SEMANTIC_ACTION_INTENT,
            category=str(proposal.get("category", "custom")),
            description=description,
            idempotent=bool(proposal.get("idempotent", False)),
            state_modifying=bool(proposal.get("state_modifying", True)),
            required_params=required,
            optional_params=[],
            # 422 is the validator's required-field error; proposals
            # always declare it so Pass 5 passes when the rest is sound.
            error_codes=list(proposal.get("error_codes") or [400, 422]),
            source=SOURCE_AMG,
            namespace=str(proposal.get("namespace") or "proposal"),
        )


__all__ = [
    "AMG_VERSION",
    "ALL_SEMANTIC_CLASSES",
    "ALL_SOURCES",
    "AMGMethodSpec",
    "PARAM_TYPES",
    "PARAM_TYPES_REQUIRING_SCHEMA",
    "ParamSpec",
    "SEMANTIC_ACTION_INTENT",
    "SEMANTIC_PROTOCOL_MECHANIC",
    "SEMANTIC_QUERY_INTENT",
    "SOURCE_AGTP",
    "SOURCE_AMG",
    "SubstitutionHint",
    "USER_SEMANTIC_CLASSES",
]
