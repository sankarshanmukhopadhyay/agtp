"""
Endpoint primitives.

The semantic-block dataclass lives in ``core`` because it gets
consumed by every layer of the protocol — dispatcher, manifest,
synthesis runtime, Compose drawer.

A ``SemanticBlock`` is the structured declaration an endpoint
makes about *what it is* (intent / actor / outcome), *what it
costs* (impact / confidence / is_idempotent), and *what it
classifies as* (capability). It is per-method metadata, not
per-request data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional


# ---------------------------------------------------------------------------
# Vocabulary constants. Kept in core so a Compose-drawer or CLI surface
# can validate dropdown choices without pulling in server-side code.
# ---------------------------------------------------------------------------


#: Suggested vocabulary for the ``actor`` field. The validator does
#: **not** enforce this set — ``actor`` is a free-form identifier of
#: the intended invoker class (``agent``, ``human``, ``system``,
#: ``customer``, ``staff``, ``admin``, or a domain-specific tag like
#: ``merchant`` / ``auditor`` / ``practitioner``). Treated as
#: documentation, not as a permission gate; scopes do permission
#: work. This constant is exposed so authoring surfaces (Compose
#: drawer, CLI ``--propose``) can offer it as a dropdown without
#: blocking values outside the list.
SUGGESTED_ACTORS: FrozenSet[str] = frozenset({
    "agent", "human", "system", "customer", "staff", "admin",
})


#: Recognized values for the ``capability`` field. These are the
#: catalog's top-level categories — one taxonomy serves both methods
#: and endpoints, so a new capability lands in the verb catalog
#: (``core/methods.json``) and this constant together.
ALL_CAPABILITIES: FrozenSet[str] = frozenset({
    "discovery",
    "retrieval",
    "analysis",
    "transaction",
    "modification",
    "creation",
    "notification",
    "mechanics",
    "domain_spanning",
})


#: Recognized values for the ``impact`` field. Three tiers describing
#: the reversibility of an endpoint's effect:
#:
#:   * ``informational`` — pure read; no state change anywhere.
#:   * ``reversible``    — state change that can be undone.
#:   * ``irreversible``  — state change that cannot be undone.
ALL_IMPACTS: FrozenSet[str] = frozenset({
    "informational",
    "reversible",
    "irreversible",
})


#: Confidence floor recommended for ``impact='irreversible'``
#: endpoints. Authoring surfaces (Compose drawer, CLI) surface this
#: as a soft warning when an author declares a destructive endpoint
#: with low confidence.
IRREVERSIBLE_CONFIDENCE_FLOOR: float = 0.85


# ---------------------------------------------------------------------------
# SemanticBlock.
# ---------------------------------------------------------------------------


@dataclass
class SemanticBlock:
    """
    Semantic declaration for a single endpoint.

    Per ``agtp-api §6``, a published endpoint declares all seven
    fields. The registry validator enforces that contract at
    insertion time; the dataclass keeps ``Optional`` defaults so
    programmatic construction can build a block field-by-field
    before handing it to the registry.

    Field summary:

      * intent      single-sentence agent-goal voice ("Reconcile...")
      * actor       free-form identifier of the intended invoker
                    class (``agent``, ``human``, ``system``,
                    ``customer``, ``staff``, ``admin``, or a
                    domain-specific tag). Suggested values in
                    :data:`SUGGESTED_ACTORS`; not enforced.
      * outcome     single-sentence post-condition ("...returns a
                    structured assessment")
      * capability  one of :data:`ALL_CAPABILITIES`, drawn from the
                    verb catalog's category taxonomy
      * confidence  float in ``[0.0, 1.0]``; non-normative guidance
                    for agent caution level. See ``agtp-api §6`` for
                    the suggested interpretation bands.
      * impact      one of :data:`ALL_IMPACTS`: ``informational``,
                    ``reversible``, or ``irreversible``
      * is_idempotent author's declaration; cross-checked against the
                      protocol-level ``idempotent`` flag
    """

    intent: str
    actor: str
    outcome: str
    capability: Optional[str] = None
    confidence: Optional[float] = None
    impact: Optional[str] = None
    is_idempotent: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "intent": self.intent,
            "actor": self.actor,
            "outcome": self.outcome,
        }
        if self.capability is not None:
            out["capability"] = self.capability
        if self.confidence is not None:
            out["confidence"] = float(self.confidence)
        if self.impact is not None:
            out["impact"] = self.impact
        if self.is_idempotent is not None:
            out["is_idempotent"] = bool(self.is_idempotent)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SemanticBlock":
        # Back-compat: pre-§4-rename docs and persisted manifests used
        # ``confidence_guidance`` / ``impact_tier``. Accept either key
        # so older wire payloads still deserialize.
        confidence_raw = data.get("confidence")
        if confidence_raw is None:
            confidence_raw = data.get("confidence_guidance")
        impact_raw = data.get("impact")
        if impact_raw is None:
            impact_raw = data.get("impact_tier")
        return cls(
            intent=str(data.get("intent", "")),
            actor=str(data.get("actor", "")),
            outcome=str(data.get("outcome", "")),
            capability=data.get("capability"),
            confidence=(
                float(confidence_raw) if confidence_raw is not None else None
            ),
            impact=impact_raw,
            is_idempotent=(
                bool(data["is_idempotent"])
                if data.get("is_idempotent") is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# EndpointSpec.
# ---------------------------------------------------------------------------


#: Recognized primitive types for ``ParamSpec.type``. The endpoint
#: registry refuses any spec whose fields name a type not in this set.
ALL_PARAM_TYPES: FrozenSet[str] = frozenset({
    "string", "integer", "number", "boolean", "object", "array",
})


#: Recognized handler-binding kinds. ``registered_function`` resolves
#: to a callable in the server's import path; ``composition`` resolves
#: to a recipe in the synthesis runtime; ``external_service`` resolves
#: to an upstream URL the server proxies to. Phase 1 only validates
#: the binding shape; resolution lands in Phase 2+.
ALL_HANDLER_TYPES: FrozenSet[str] = frozenset({
    "registered_function", "composition", "external_service",
})


@dataclass
class ParamSpec:
    """
    A single field declaration on an endpoint (input parameter or
    output value).

    Field summary:

      * name         lowercase snake_case identifier
      * type         one of the recognized primitives in
                     :data:`ALL_PARAM_TYPES`
      * description  non-empty prose
      * schema       full JSON Schema, used when ``type`` is
                     ``object`` or ``array`` and the inner shape
                     matters for validation
      * enum         optional list of constrained values (the field
                     value must match one of them)
      * format       optional named format hint (``date``,
                     ``date-time``, ``email``, ``uuid``, ...). The
                     registry treats this as documentation; downstream
                     consumers may enforce it.

    Names: ``ParamSpec`` is the historical name (the synthesis runtime
    and negotiation code call it that); ``FieldSpec`` is an alias for
    new code that wants the more general term.
    """

    name: str
    type: str
    description: str
    schema: Optional[Dict[str, Any]] = None
    enum: Optional[List[Any]] = None
    format: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }
        if self.schema is not None:
            out["schema"] = dict(self.schema)
        if self.enum is not None:
            out["enum"] = list(self.enum)
        if self.format is not None:
            out["format"] = self.format
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParamSpec":
        return cls(
            name=str(data.get("name", "")),
            type=str(data.get("type", "string")),
            description=str(data.get("description", "")),
            schema=data.get("schema"),
            enum=list(data["enum"]) if data.get("enum") is not None else None,
            format=str(data["format"]) if data.get("format") else None,
        )

    @classmethod
    def from_bare_name(cls, name: str) -> "ParamSpec":
        """Promote a bare parameter name to a ``ParamSpec``."""
        return cls(
            name=str(name),
            type="string",
            description=f"parameter '{name}'",
        )


# Alias for new code that prefers the general term over the historical
# ``ParamSpec`` name. Both refer to the same dataclass.
FieldSpec = ParamSpec


#: Default upstream-call timeout for ``external_service`` bindings,
#: in seconds. Bindings may override per-endpoint via
#: ``timeout_seconds`` in the TOML; without an override, the
#: dispatcher refuses to wait longer than this for an upstream
#: response.
DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS: float = 30.0


@dataclass
class HandlerBinding:
    """
    Declaration of how an endpoint's handler is resolved.

    Three binding kinds are recognized, each with its own
    type-specific reference field (per ``agtp-api §9``):

      * ``registered_function`` — :attr:`function` is a Python dotted
        path (``staybeta.handlers.book_room``) the server imports and
        calls. The function follows the public :class:`agtp.handlers`
        signature: a single :class:`~agtp.handlers.EndpointContext`
        argument, returning :class:`~agtp.handlers.EndpointResponse`
        or :class:`~agtp.handlers.EndpointError`.
      * ``composition`` — :attr:`recipe` is the name of a recipe in
        the synthesis runtime. The dispatcher routes the call to the
        runtime which threads parameters through the recipe's plan.
        Recipe steps walk through the same dispatcher external
        invocations go through, so authority is preserved.
      * ``external_service`` — :attr:`url` is an HTTPS URL the server
        proxies to. The binding additionally carries the upstream
        HTTP method, headers, input/output transforms, an
        HTTP-status-code → AGTP-error-code map, and a timeout.

    Pre-§9 callers used a generic ``reference`` field and
    ``input_map`` / ``output_map`` transform names. The dataclass
    accepts those legacy kwargs and routes them in ``__post_init__``;
    the read-only :attr:`reference`, :attr:`input_map`, and
    :attr:`output_map` properties surface the type-specific values
    for any straggler reads.

    The ``external_service`` extras (method, headers, transforms,
    error_map, timeout_seconds) are ignored for non-external bindings
    and stay empty in their serialized shape so existing TOML files
    continue to round-trip cleanly.
    """

    type: str
    #: ``registered_function`` only: Python dotted path the server
    #: imports and calls.
    function: Optional[str] = None
    #: ``composition`` only: recipe name in the synthesis runtime.
    recipe: Optional[str] = None
    #: ``external_service`` only: HTTPS URL the server proxies to.
    url: Optional[str] = None
    # external_service-only extras. Defaults: empty maps mean
    # "pass-through with original names", empty headers mean "no
    # extra request headers".
    method: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    input_transform: Dict[str, str] = field(default_factory=dict)
    output_transform: Dict[str, str] = field(default_factory=dict)
    error_map: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS
    # Pre-§9 back-compat init kwargs. Callers that still pass these
    # get routed to the new fields in __post_init__; they never
    # surface in to_dict or asdict round-trips.
    reference: Optional[str] = None
    input_map: Optional[Dict[str, str]] = None
    output_map: Optional[Dict[str, str]] = None

    def __post_init__(self) -> None:
        # Route legacy init kwargs (reference, input_map, output_map)
        # into the §9 type-specific fields. New names always win when
        # both are supplied; the legacy fields are then cleared so
        # they don't shadow read access via :meth:`reference_value`
        # or leak into serialization.
        if self.reference:
            if self.type == "registered_function" and not self.function:
                self.function = self.reference
            elif self.type == "composition" and not self.recipe:
                self.recipe = self.reference
            elif self.type == "external_service" and not self.url:
                self.url = self.reference
            self.reference = None
        if self.input_map is not None and not self.input_transform:
            self.input_transform = dict(self.input_map)
        self.input_map = None
        if self.output_map is not None and not self.output_transform:
            self.output_transform = dict(self.output_map)
        self.output_map = None

    @property
    def reference_value(self) -> str:
        """The type-specific reference value (function / recipe /
        url) as a single string. Returns ``""`` when none is set.

        Use this when a caller needs to read the binding's reference
        without switching on :attr:`type` — e.g., logging or error
        messages. Code that takes type-specific action should read
        :attr:`function`, :attr:`recipe`, or :attr:`url` directly.
        """
        if self.type == "registered_function":
            return self.function or ""
        if self.type == "composition":
            return self.recipe or ""
        if self.type == "external_service":
            return self.url or ""
        return ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type}
        # Emit the type-specific reference field name. Pre-§9
        # consumers that scanned for ``reference`` need to look up
        # the new name (``function`` / ``recipe`` / ``url``).
        if self.type == "registered_function" and self.function:
            out["function"] = self.function
        elif self.type == "composition" and self.recipe:
            out["recipe"] = self.recipe
        elif self.type == "external_service" and self.url:
            out["url"] = self.url
        if self.type == "external_service":
            if self.method is not None:
                out["method"] = self.method
            if self.headers:
                out["headers"] = dict(self.headers)
            if self.input_transform:
                out["input_transform"] = dict(self.input_transform)
            if self.output_transform:
                out["output_transform"] = dict(self.output_transform)
            if self.error_map:
                # Stringify keys for stable JSON output regardless of
                # whether the loader read them as ints or strings.
                out["error_map"] = {str(k): v for k, v in self.error_map.items()}
            if self.timeout_seconds != DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS:
                out["timeout_seconds"] = float(self.timeout_seconds)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HandlerBinding":
        """Construct from the wire / TOML shape.

        Accepts both the new §9 field names (``function`` /
        ``recipe`` / ``url`` / ``input_transform`` /
        ``output_transform``) and the pre-§9 names (``reference`` /
        ``input_map`` / ``output_map``). New names win on conflict.
        """
        type_ = str(data.get("type", ""))
        # Type-specific reference field. Prefer the new name; fall
        # back to legacy ``reference``.
        function = data.get("function")
        recipe = data.get("recipe")
        url = data.get("url")
        legacy_reference = data.get("reference")
        if legacy_reference:
            if type_ == "registered_function" and not function:
                function = legacy_reference
            elif type_ == "composition" and not recipe:
                recipe = legacy_reference
            elif type_ == "external_service" and not url:
                url = legacy_reference
        # Transforms — prefer §9 names.
        input_transform_raw = (
            data.get("input_transform") or data.get("input_map") or {}
        )
        output_transform_raw = (
            data.get("output_transform") or data.get("output_map") or {}
        )
        timeout = data.get("timeout_seconds")
        return cls(
            type=type_,
            function=str(function) if function else None,
            recipe=str(recipe) if recipe else None,
            url=str(url) if url else None,
            method=(
                str(data["method"]).upper()
                if data.get("method") else None
            ),
            headers={
                str(k): str(v) for k, v in (data.get("headers") or {}).items()
            },
            input_transform={
                str(k): str(v) for k, v in input_transform_raw.items()
            },
            output_transform={
                str(k): str(v) for k, v in output_transform_raw.items()
            },
            error_map={
                str(k): str(v)
                for k, v in (data.get("error_map") or {}).items()
            },
            timeout_seconds=(
                float(timeout)
                if timeout is not None
                else DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS
            ),
        )


@dataclass
class EndpointDeprecation:
    """
    Per-endpoint deprecation metadata.

    Mirrors the catalog-level ``deprecated_in`` / ``removed_in`` /
    ``successor`` pattern but at the endpoint level. An endpoint
    can be deprecated even when its method and path remain in the
    catalog (e.g., a server is migrating ``BOOK /room`` to
    ``RESERVE /room``; the verbs are both still in the catalog,
    but the operator wants callers off the old endpoint).

    Fields:

      * ``deprecated_in``  semver string the endpoint was flagged
                           as deprecated.
      * ``removed_in``     optional semver the endpoint is
                           scheduled for removal. ``None`` means
                           "deprecated indefinitely".
      * ``successor_method`` optional AGTP verb the agent should
                           call instead.
      * ``successor_path``  optional path the agent should call
                           instead. Together with ``successor_method``
                           identifies the replacement endpoint.

    The dispatcher stamps an ``AGTP-Endpoint-Warning`` advisory
    header on responses for deprecated-endpoint invocations so
    callers can surface the migration prompt without parsing the
    manifest.
    """

    deprecated_in: str
    removed_in: Optional[str] = None
    successor_method: Optional[str] = None
    successor_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"deprecated_in": self.deprecated_in}
        if self.removed_in:
            out["removed_in"] = self.removed_in
        if self.successor_method or self.successor_path:
            successor: Dict[str, str] = {}
            if self.successor_method:
                successor["method"] = self.successor_method.upper()
            if self.successor_path:
                successor["path"] = self.successor_path
            out["successor"] = successor
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EndpointDeprecation":
        successor = data.get("successor") or {}
        if not isinstance(successor, dict):
            successor = {}
        return cls(
            deprecated_in=str(data.get("deprecated_in", "")),
            removed_in=(
                str(data["removed_in"]) if data.get("removed_in") else None
            ),
            successor_method=(
                str(successor["method"]).upper()
                if successor.get("method") else None
            ),
            successor_path=(
                str(successor["path"]) if successor.get("path") else None
            ),
        )


@dataclass
class EndpointSpec:
    """
    A complete endpoint declaration: (method, path) pair, parameters,
    semantic block, output contract, declared error conditions, and
    a handler binding.

    Validation reduces to two cheap checks against
    ``core.methods``: the verb name is in the curated catalog (or
    a custom verb under the server's namespace), and any path the
    endpoint serves passes ``core.path_grammar.validate_path``.
    A structural validator runs at registry insertion time.

    Field summary:

      * name             the AGTP verb. Stored under ``name`` for
                         backward compat with the synthesis runtime;
                         exposed under ``method`` in the wire /
                         manifest renderings.
      * path             the URI path the endpoint serves at. Optional
                         for runtime proposals (where the path
                         registry isn't engaged); required when an
                         endpoint is loaded into
                         :class:`~server.endpoint_registry.EndpointRegistry`.
      * description      short prose description.
      * namespace        optional grouping for organizing endpoints.
      * semantic         the semantic block. Required by the
                         registry validator.
      * required_params  input fields the caller must supply.
      * optional_params  input fields the caller may supply.
      * output           output fields the response carries.
      * errors           named error condition strings declared by
                         the endpoint (e.g.
                         ``["room_unavailable", "invalid_dates"]``).
                         Distinct from ``error_codes`` (HTTP / AGTP
                         status numbers).
      * handler          :class:`HandlerBinding` declaration.
      * category         legacy hint used by the registry's lookup
                         helpers; defaults to ``"custom"``.
      * error_codes      list of status codes the endpoint may
                         return. Distinct from ``errors`` above.
    """

    name: str
    path: Optional[str] = None
    description: str = ""
    required_params: List[ParamSpec] = field(default_factory=list)
    optional_params: List[ParamSpec] = field(default_factory=list)
    output: List[ParamSpec] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    semantic: Optional[SemanticBlock] = None
    handler: Optional[HandlerBinding] = None
    required_scopes: List[str] = field(default_factory=list)
    deprecated: Optional["EndpointDeprecation"] = None
    namespace: Optional[str] = None
    category: str = "custom"
    error_codes: List[int] = field(default_factory=lambda: [400, 422])

    @property
    def method(self) -> str:
        """Alias for :attr:`name`. The (method, path) pair is the
        canonical registry key; ``method`` is the term Phase-1 docs
        use, while ``name`` is what the synthesis runtime accesses."""
        return self.name

    @property
    def input_required(self) -> List[ParamSpec]:
        """Phase-1 alias for :attr:`required_params`."""
        return self.required_params

    @property
    def input_optional(self) -> List[ParamSpec]:
        """Phase-1 alias for :attr:`optional_params`."""
        return self.optional_params

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the canonical wire shape.

        The wire shape mirrors the Phase-1 contract: ``method`` and
        ``path`` at the top, the semantic block under ``semantic``,
        inputs under ``input.required`` / ``input.optional``,
        outputs under ``output``, declared errors under ``errors``,
        and the handler binding under ``handler``. The historical
        ``name`` / ``required_params`` / ``optional_params`` keys
        are also emitted so existing readers keep working.
        """
        required_out = [
            p.to_dict() if hasattr(p, "to_dict") else dict(p)
            for p in self.required_params
        ]
        optional_out = [
            p.to_dict() if hasattr(p, "to_dict") else dict(p)
            for p in self.optional_params
        ]
        output_out = [
            p.to_dict() if hasattr(p, "to_dict") else dict(p)
            for p in self.output
        ]
        out: Dict[str, Any] = {
            # Phase-1 canonical keys.
            "method": self.name,
            "input": {"required": required_out, "optional": optional_out},
            "output": output_out,
            "errors": list(self.errors),
            # Historical keys (synthesis runtime + negotiation read these).
            "name": self.name,
            "description": self.description,
            "required_params": required_out,
            "optional_params": optional_out,
            "category": self.category,
            "error_codes": list(self.error_codes),
        }
        if self.path is not None:
            out["path"] = self.path
        if self.namespace:
            out["namespace"] = self.namespace
        if self.semantic is not None:
            out["semantic"] = self.semantic.to_dict()
        if self.handler is not None:
            out["handler"] = self.handler.to_dict()
        if self.required_scopes:
            out["required_scopes"] = list(self.required_scopes)
        if self.deprecated is not None:
            out["deprecated"] = self.deprecated.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EndpointSpec":
        """Construct from the canonical wire shape produced by
        :meth:`to_dict`. Accepts either the new ``method`` /
        ``input.required`` keys or the historical
        ``name`` / ``required_params`` keys (the latter is what
        runtime PROPOSE bodies and persisted manifests carry)."""
        name = str(data.get("method") or data.get("name", "")).upper()

        # Inputs: prefer the new ``input.required`` / ``input.optional``
        # nested shape; fall back to the historical flat shape.
        input_block = data.get("input")
        if isinstance(input_block, dict):
            req_raw = input_block.get("required") or []
            opt_raw = input_block.get("optional") or []
        else:
            req_raw = data.get("required_params") or []
            opt_raw = data.get("optional_params") or []
        required = [
            ParamSpec.from_dict(p) if isinstance(p, dict)
            else ParamSpec.from_bare_name(str(p))
            for p in req_raw
        ]
        optional = [
            ParamSpec.from_dict(p) if isinstance(p, dict)
            else ParamSpec.from_bare_name(str(p))
            for p in opt_raw
        ]
        output_raw = data.get("output") or []
        output = [
            ParamSpec.from_dict(p) if isinstance(p, dict)
            else ParamSpec.from_bare_name(str(p))
            for p in output_raw
        ]

        semantic_data = data.get("semantic")
        semantic = (
            SemanticBlock.from_dict(semantic_data)
            if isinstance(semantic_data, dict)
            else None
        )

        handler_data = data.get("handler")
        handler = (
            HandlerBinding.from_dict(handler_data)
            if isinstance(handler_data, dict)
            else None
        )

        deprecated_data = data.get("deprecated")
        deprecation = (
            EndpointDeprecation.from_dict(deprecated_data)
            if isinstance(deprecated_data, dict) and deprecated_data.get("deprecated_in")
            else None
        )

        return cls(
            name=name,
            path=(str(data["path"]) if data.get("path") else None),
            description=str(data.get("description") or ""),
            required_params=required,
            optional_params=optional,
            output=output,
            errors=[str(e) for e in (data.get("errors") or [])],
            semantic=semantic,
            handler=handler,
            required_scopes=[
                str(s) for s in (data.get("required_scopes") or [])
            ],
            deprecated=deprecation,
            namespace=(
                str(data["namespace"]) if data.get("namespace") else None
            ),
            category=str(data.get("category", "custom")),
            error_codes=list(data.get("error_codes") or [400, 422]),
        )

    @classmethod
    def from_proposal(cls, proposal: Dict[str, Any]) -> "EndpointSpec":
        """
        Build an ``EndpointSpec`` from a runtime PROPOSE body.

        The body shape is intentionally permissive — proposals from
        agents at the wire are sometimes minimal (just ``name`` plus
        ``parameters``). Missing fields take conservative defaults so
        the dispatcher's catalog lookup is the one true gate.
        """
        name = str(proposal.get("name", "")).upper()

        raw_params = proposal.get("parameters") or {}
        required: list = []
        if isinstance(raw_params, dict):
            for pname, ptype in raw_params.items():
                type_str = ptype if isinstance(ptype, str) else "string"
                required.append(ParamSpec(
                    name=str(pname),
                    type=type_str,
                    description=f"parameter '{pname}' from proposal",
                ))
        elif isinstance(raw_params, list):
            for entry in raw_params:
                if isinstance(entry, dict):
                    required.append(ParamSpec.from_dict(entry))
                elif isinstance(entry, str):
                    required.append(ParamSpec.from_bare_name(entry))

        semantic_data = proposal.get("semantic")
        semantic = (
            SemanticBlock.from_dict(semantic_data)
            if isinstance(semantic_data, dict)
            else None
        )

        return cls(
            name=name,
            path=(str(proposal["path"]) if proposal.get("path") else None),
            description=str(
                proposal.get("description")
                or f"runtime proposal: {name or '(unnamed)'}"
            ),
            required_params=required,
            optional_params=[],
            semantic=semantic,
            namespace=str(proposal.get("namespace") or "proposal"),
            category=str(proposal.get("category", "custom")),
            error_codes=list(proposal.get("error_codes") or [400, 422]),
        )


__all__ = [
    "ALL_CAPABILITIES",
    "ALL_HANDLER_TYPES",
    "ALL_IMPACTS",
    "ALL_PARAM_TYPES",
    "DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS",
    "EndpointDeprecation",
    "EndpointSpec",
    "FieldSpec",
    "HandlerBinding",
    "IRREVERSIBLE_CONFIDENCE_FLOOR",
    "ParamSpec",
    "SemanticBlock",
    "SUGGESTED_ACTORS",
]
