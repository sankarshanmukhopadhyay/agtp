"""
The AGTP embedded method set.

Twelve methods, six cognitive plus six mechanics, registered through a
decorator. Each entry carries the metadata the catalog gate, the
manifest renderer, and the synthesis runtime consume:
single uppercase token, imperative base form, action-intent
semantic class, full semantic declaration.

Cognitive (the agent reasons about the world):
    QUERY DISCOVER DESCRIBE SUMMARIZE PLAN EXECUTE

Mechanics (the protocol does its job):
    DELEGATE ESCALATE CONFIRM SUSPEND PROPOSE NOTIFY

Each handler has the signature

    (request: AGTPRequest, server_state: ServerState, agent_doc: AgentDocument)
        -> AGTPResponse

The server dispatcher looks up a method by name in REGISTRY, runs its
capability check against the target agent's `capabilities` field, then
calls the handler. Handlers stub their cognitive content but enforce
real parameter validation, real status codes, and real response shapes.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol

from core import wire

if TYPE_CHECKING:
    from core.endpoint import SemanticBlock
from core.identity import (
    AgentDocument,
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
    DOC_TYPE_AGENT_DOCUMENT,
    HEADER_DOCUMENT_TYPE,
)
from core.render import render_html


HandlerFn = Callable[
    [wire.AGTPRequest, "ServerState", AgentDocument],
    wire.AGTPResponse,
]


class ServerState(Protocol):
    """Minimal interface a server presents to method handlers."""

    def list_ids(self) -> List[str]: ...
    def lookup(self, agent_id: str) -> Optional[AgentDocument]: ...


@dataclass
class MethodSpec:
    """
    Full declaration of a single method (embedded or custom).

    The embedded-vs-custom distinction comes from membership in
    :data:`core.methods.EMBEDDED_VERBS` (the 12 protocol primitives).
    Embedded methods carry no namespace; custom methods declare a
    non-empty ``namespace`` for disambiguation across servers.

    The ``semantic`` field carries the semantic block (intent /
    actor / outcome / capability / confidence / impact /
    is_idempotent). Both embedded and custom methods populate it;
    the composition runtime reads these fields when reasoning about
    which primitives can satisfy a recipe pattern.
    """

    name: str
    category: str                     # "cognitive" | "mechanics" | "transact" | ...
    semantic_class: str               # semantic class (e.g., "action-intent")
    idempotent: bool
    state_modifying: bool
    required_params: List[str]
    optional_params: List[str] = field(default_factory=list)
    error_codes: List[int] = field(default_factory=list)
    description: str = ""
    handler: Optional[HandlerFn] = None
    namespace: Optional[str] = None
    semantic: Optional["SemanticBlock"] = None


REGISTRY: Dict[str, MethodSpec] = {}


#: Verb-name shape per AGTP-API: uppercase ASCII letters only.
#: Length bounds (3-32) are checked separately so the error
#: messages can distinguish "wrong character set" from "wrong length".
_VERB_NAME_PATTERN = re.compile(r"^[A-Z]+$")


def _validate_spec(
    name: str,
    namespace: Optional[str],
    description: str,
    error_codes: Optional[List[int]],
) -> None:
    """Shared sanity checks for both decorator and runtime registration.

    Verb-name shape (per AGTP-API): uppercase ASCII single token,
    length 3-32. The catalog's curatorial process enforces the
    additional imperative-form / action-intent constraint; that is
    not a runtime check.

    The embedded-vs-custom distinction is determined by membership
    in :data:`core.methods.EMBEDDED_VERBS`: embedded methods (the
    12 protocol primitives) carry no namespace; custom methods
    require one.
    """
    from core.methods import EMBEDDED_VERBS

    if not name or not _VERB_NAME_PATTERN.match(name):
        raise ValueError(
            f"method name must be uppercase ASCII letters only "
            f"(regex ^[A-Z]+$); got {name!r}"
        )
    if not 3 <= len(name) <= 32:
        raise ValueError(
            f"method name length must be 3-32 characters; "
            f"got {len(name)} for {name!r}"
        )
    if name in REGISTRY:
        raise RuntimeError(f"method {name!r} already registered")
    is_embedded = name in EMBEDDED_VERBS
    if not is_embedded and not namespace:
        raise ValueError(
            f"custom method {name!r} requires a namespace"
        )
    if is_embedded and namespace is not None:
        raise ValueError(
            f"embedded method {name!r} cannot declare a namespace"
        )
    if not description:
        raise ValueError(f"method {name!r} requires a description")
    if not error_codes:
        raise ValueError(f"method {name!r} must declare at least one error code")


def _build_semantic_block(
    *,
    intent: Optional[str],
    actor: Optional[str],
    outcome: Optional[str],
    capability: Optional[str],
    confidence: Optional[float],
    impact: Optional[str],
    is_idempotent: Optional[bool],
    semantic: Optional["SemanticBlock"],
) -> Optional["SemanticBlock"]:
    """
    Resolve the semantic block for a registration.

    Two equivalent forms are accepted:

      * pre-built ``semantic=SemanticBlock(...)`` (one kwarg), or
      * the seven scalar kwargs above.

    Returns the resolved SemanticBlock or None when no semantic
    fields are supplied (preserves the v1 behavior for callers that
    haven't been migrated yet).
    """
    from core.endpoint import SemanticBlock

    if semantic is not None:
        return semantic
    if all(
        v is None
        for v in (intent, actor, outcome, capability,
                  confidence, impact, is_idempotent)
    ):
        return None
    return SemanticBlock(
        intent=intent or "",
        actor=actor or "agent",
        outcome=outcome or "",
        capability=capability,
        confidence=confidence,
        impact=impact,
        is_idempotent=is_idempotent,
    )


def method(
    *,
    name: str,
    category: str,
    semantic_class: str,
    idempotent: bool,
    state_modifying: bool,
    required_params: List[str],
    optional_params: Optional[List[str]] = None,
    error_codes: Optional[List[int]] = None,
    description: str = "",
    namespace: Optional[str] = None,
    # Semantic block — pass either ``semantic=...`` or the seven
    # scalar fields. The decorator bundles them.
    intent: Optional[str] = None,
    actor: Optional[str] = None,
    outcome: Optional[str] = None,
    capability: Optional[str] = None,
    confidence: Optional[float] = None,
    impact: Optional[str] = None,
    is_idempotent: Optional[bool] = None,
    semantic: Optional["SemanticBlock"] = None,
) -> Callable[[HandlerFn], HandlerFn]:
    """
    Decorator that registers a handler in REGISTRY.

    Names are normalized to uppercase. A second registration of the
    same name raises, since the registry is the single source of
    truth. Embedded primitives carry no namespace; custom methods
    require one — the distinction is enforced by ``_validate_spec``
    based on :data:`core.methods.EMBEDDED_VERBS` membership.
    Custom-method registration is normally done via
    :func:`register_custom`.

    The seven semantic fields (intent / actor / outcome / capability
    / confidence / impact / is_idempotent) populate the method's
    :class:`SemanticBlock`. Composition recipes, the catalog, and
    Elemen's manifest view all consume these values, so embedded
    methods are expected to declare them. The legacy form (no
    semantic fields) keeps working — the resulting MethodSpec just
    has ``semantic=None``.
    """

    sb = _build_semantic_block(
        intent=intent, actor=actor, outcome=outcome, capability=capability,
        confidence=confidence, impact=impact,
        is_idempotent=is_idempotent, semantic=semantic,
    )

    def decorator(fn: HandlerFn) -> HandlerFn:
        normalized = name.upper()
        # Phase-6 graceful degradation: if the verb isn't in the
        # current catalog, log a CatalogWarning and skip the
        # registration. The function is returned unmodified so the
        # rest of the module loads normally — the server boots, the
        # method just isn't reachable.
        from core.methods import (
            CatalogWarning, is_approved_verb,
        )
        import warnings as _warnings
        if not is_approved_verb(normalized):
            _warnings.warn(
                f"Custom method {normalized!r} references a verb not "
                f"in the current catalog. Registration skipped. The "
                f"server will boot but this method will not be "
                f"available. (Did the catalog remove it? Run "
                f"agtp-catalog-diff against your old catalog to find "
                f"out what changed.)",
                CatalogWarning,
                stacklevel=2,
            )
            return fn
        _validate_spec(normalized, namespace, description, error_codes)
        REGISTRY[normalized] = MethodSpec(
            name=normalized,
            category=category,
            semantic_class=semantic_class,
            idempotent=idempotent,
            state_modifying=state_modifying,
            required_params=list(required_params),
            optional_params=list(optional_params or []),
            error_codes=list(error_codes or []),
            description=description,
            handler=fn,
            namespace=namespace,
            semantic=sb,
        )
        return fn

    return decorator


def register_custom(
    handler: HandlerFn,
    *,
    name: str,
    namespace: str,
    category: str,
    semantic_class: str,
    idempotent: bool,
    state_modifying: bool,
    required_params: List[str],
    optional_params: Optional[List[str]] = None,
    error_codes: Optional[List[int]] = None,
    description: str = "",
    # Semantic block — same dual form as the @method decorator.
    intent: Optional[str] = None,
    actor: Optional[str] = None,
    outcome: Optional[str] = None,
    capability: Optional[str] = None,
    confidence: Optional[float] = None,
    impact: Optional[str] = None,
    is_idempotent: Optional[bool] = None,
    semantic: Optional["SemanticBlock"] = None,
) -> MethodSpec:
    """
    Register a custom method at runtime.

    A ``namespace`` is required; everything else mirrors the
    ``@method`` decorator. Servers call this to expose custom verbs
    without modifying the core registry. Returns the registered
    :class:`MethodSpec` so callers can inspect it.

    The verb name must be in the loaded method catalog. Verbs absent
    from the catalog emit a :class:`~core.methods.CatalogWarning` and
    the registration is skipped (returning ``None``). This lets a
    server upgrade its catalog and reload custom-method modules
    without crashing the boot sequence.
    """
    normalized = name.upper()
    # Catalog membership check with Phase-6 graceful skip.
    from core.methods import (
        CatalogWarning, is_approved_verb,
    )
    import warnings as _warnings
    if not is_approved_verb(normalized):
        _warnings.warn(
            f"register_custom for {normalized!r}: verb is not in "
            f"the current catalog. Registration skipped.",
            CatalogWarning,
            stacklevel=2,
        )
        return None  # type: ignore[return-value]
    _validate_spec(normalized, namespace, description, error_codes)

    sb = _build_semantic_block(
        intent=intent, actor=actor, outcome=outcome, capability=capability,
        confidence=confidence, impact=impact,
        is_idempotent=is_idempotent, semantic=semantic,
    )

    spec = MethodSpec(
        name=normalized,
        category=category,
        semantic_class=semantic_class,
        idempotent=idempotent,
        state_modifying=state_modifying,
        required_params=list(required_params),
        optional_params=list(optional_params or []),
        error_codes=list(error_codes or []),
        description=description,
        handler=handler,
        namespace=namespace,
        semantic=sb,
    )
    REGISTRY[normalized] = spec
    return spec


def unregister(name: str) -> None:
    """
    Remove a registered method. Used by tests to keep the registry clean
    across runs. Quiet no-op if the name is unknown.
    """
    REGISTRY.pop(name.upper(), None)


# ---------------------------------------------------------------------------
# Helpers shared by handlers.
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_offset_iso(seconds: int) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_token(prefix: str) -> str:
    """Generate a short opaque identifier with a human-readable prefix."""
    return f"{prefix}-{secrets.token_urlsafe(12)}"


def parse_body(request: wire.AGTPRequest) -> Dict[str, Any]:
    """
    Decode the request body as JSON. Empty body becomes an empty dict.

    Raises ValueError on malformed JSON. The dispatcher converts that
    into a 400.
    """
    if not request.body_bytes:
        return {}
    text = request.body_bytes.decode("utf-8", errors="replace")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def _content_type_for(method_name: str) -> str:
    """Per-method response media type, following the identity+json pattern."""
    return f"application/vnd.agtp.{method_name.lower()}+json"


def json_response(
    status_code: int,
    status_text: str,
    payload: Dict[str, Any],
    *,
    method_name: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> wire.AGTPResponse:
    body = json.dumps(payload, indent=2).encode("utf-8")
    content_type = (
        _content_type_for(method_name) if method_name else "application/json"
    )
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    if extra_headers:
        headers.update(extra_headers)
    return wire.AGTPResponse(
        status_code=status_code,
        status_text=status_text,
        headers=headers,
        body_bytes=body,
    )


def error_response(
    status: int,
    status_text: str,
    code: str,
    detail: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    payload: Dict[str, Any] = {"error": {"code": code, "detail": detail}}
    if extra:
        payload["error"].update(extra)
    body = json.dumps(payload).encode("utf-8")
    return wire.AGTPResponse(
        status_code=status,
        status_text=status_text,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body_bytes=body,
    )


def require_params(
    spec: MethodSpec, params: Dict[str, Any]
) -> Optional[wire.AGTPResponse]:
    """
    Return a 422 response if any required parameter is missing or empty.
    """
    # An empty list or empty dict is a valid value; only literal absence,
    # None, or an empty string counts as missing.
    missing = [
        name for name in spec.required_params
        if name not in params or params[name] is None or params[name] == ""
    ]
    if missing:
        return error_response(
            422,
            "Unprocessable Entity",
            "missing-required-params",
            f"missing required parameter(s): {', '.join(missing)}",
            extra={"missing": missing, "method": spec.name},
        )
    return None


def spec_to_dict(spec: MethodSpec) -> Dict[str, Any]:
    """
    Serialize a MethodSpec to the JSON shape used by DISCOVER /methods.

    Embedded methods omit `namespace` (always None for them); custom
    methods include it. Handler is never serialized.
    """
    payload: Dict[str, Any] = {
        "name": spec.name,
        "category": spec.category,
        "semantic_class": spec.semantic_class,
        "idempotent": spec.idempotent,
        "state_modifying": spec.state_modifying,
        "required_params": list(spec.required_params),
        "optional_params": list(spec.optional_params),
        "error_codes": list(spec.error_codes),
        "description": spec.description,
    }
    if spec.namespace is not None:
        payload["namespace"] = spec.namespace
    if spec.semantic is not None:
        payload["semantic"] = spec.semantic.to_dict()
    return payload


def spec_to_endpoint_spec(spec: MethodSpec) -> "EndpointSpec":
    """
    Convert a server-side ``MethodSpec`` (handler + protocol metadata)
    into an :class:`EndpointSpec` suitable for the synthesis runtime
    to inspect. ``required_params`` / ``optional_params`` are string
    lists on ``MethodSpec``; the endpoint spec expects ``ParamSpec``
    objects, so each name is promoted to a default ``ParamSpec`` with
    type=string and a generic description.
    """
    from core.endpoint import EndpointSpec, ParamSpec

    def _promote(name: str) -> ParamSpec:
        return ParamSpec(
            name=name,
            type="string",
            description=f"parameter '{name}' from server method spec",
        )

    error_codes = list(spec.error_codes) if spec.error_codes else [400, 422]
    if 422 not in error_codes:
        error_codes.append(422)
    return EndpointSpec(
        name=spec.name,
        category=spec.category or "custom",
        description=spec.description or f"server method {spec.name}",
        required_params=[_promote(p) for p in spec.required_params],
        optional_params=[_promote(p) for p in spec.optional_params],
        error_codes=error_codes,
        namespace=spec.namespace,
        semantic=spec.semantic,
    )


def check_capability(
    spec: MethodSpec, agent_doc: AgentDocument
) -> Optional[wire.AGTPResponse]:
    """
    Return a 405 response if the target agent does not accept the method.

    Acceptance is decided by the v2 ``requires`` declaration:

      * ``requires.wildcards == True`` accepts any method.
      * Otherwise, the method must appear in ``requires.methods``.

    Agents migrated from v1 fall through this same check (their old
    ``capabilities`` field is lifted into ``requires.methods`` at load
    time, with wildcards=False).
    """
    if agent_doc.accepts_method(spec.name):
        return None
    return error_response(
        405,
        "Method Not Allowed",
        "method-not-in-requires",
        (
            f"agent {agent_doc.agent_id[:12]}... does not declare "
            f"{spec.name} in requires.methods"
        ),
        extra={
            "method": spec.name,
            "requires": agent_doc.requires.to_dict(),
        },
    )


# ---------------------------------------------------------------------------
# Cognitive methods.
# ---------------------------------------------------------------------------


@method(
    name="QUERY",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["intent"],
    optional_params=["scope", "format", "confidence_threshold", "context"],
    error_codes=[400, 405, 422],
    description="Express an information need; semantic retrieval.",
    intent="Express a structured information need against a server.",
    actor="agent",
    outcome="Matched results are returned in the requested format.",
    capability="retrieval",
    confidence=0.80,
    impact="informational",
    is_idempotent=True,
)
def handle_query(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["QUERY"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    intent = params["intent"]
    threshold = float(params.get("confidence_threshold", 0.5))
    return json_response(
        200,
        "OK",
        {
            "method": "QUERY",
            "agent_id": agent_doc.agent_id,
            "intent": intent,
            "scope": params.get("scope"),
            "confidence_threshold": threshold,
            "results": [
                {
                    "id": _new_token("res"),
                    "content": f"stub: synthetic match for: {intent}",
                    "confidence": 0.95,
                }
            ],
            "result_count": 1,
            "issued_at": _utc_now_iso(),
        },
        method_name="QUERY",
    )


@method(
    name="DISCOVER",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["target"],
    optional_params=["filter"],
    error_codes=[400, 405, 422],
    description=(
        "Enumerate available agents, methods, APIs, or tools on this server."
    ),
    intent="Enumerate the methods or capabilities a server exposes.",
    actor="agent",
    outcome="An inventory of the requested target is returned.",
    capability="discovery",
    confidence=0.90,
    impact="informational",
    is_idempotent=True,
)
def handle_discover(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["DISCOVER"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    target = str(params["target"]).lower()

    if target == "methods":
        # Bucket by source: embedded vs custom. Each bucket is sorted
        # alphabetically by name. The browser and other consumers rely
        # on this stable ordering. Wildcard agents surface every method
        # in REGISTRY; strict agents surface only what they declare.
        from core.methods import EMBEDDED_VERBS
        embedded: List[Dict[str, Any]] = []
        custom: List[Dict[str, Any]] = []
        for verb, m in REGISTRY.items():
            if not agent_doc.accepts_method(verb):
                continue
            entry = spec_to_dict(m)
            if verb in EMBEDDED_VERBS:
                embedded.append(entry)
            else:
                custom.append(entry)
        embedded.sort(key=lambda e: e["name"])
        custom.sort(key=lambda e: e["name"])
        return json_response(
            200,
            "OK",
            {
                "method": "DISCOVER",
                "agent_id": agent_doc.agent_id,
                "target": "methods",
                "embedded": embedded,
                "custom": custom,
                "summary": {
                    "embedded_count": len(embedded),
                    "custom_count": len(custom),
                    "total": len(embedded) + len(custom),
                    "wildcards": agent_doc.requires.wildcards,
                },
                "issued_at": _utc_now_iso(),
            },
            method_name="DISCOVER",
        )

    items: List[Dict[str, Any]]

    if target == "agents":
        # v2-aware lightweight entries: skills_summary + methods_count.
        # Full per-agent details require a follow-up DESCRIBE.
        from server.manifest import _summarize_skills  # local to avoid cycle
        items = []
        for aid in server_state.list_ids():
            doc = server_state.lookup(aid)
            if doc is None:
                continue
            items.append(
                {
                    "agent_id": doc.agent_id,
                    "name": doc.name,
                    "skills_summary": _summarize_skills(doc.skills),
                    "methods_count": len(doc.requires.methods),
                }
            )
    elif target in ("tools", "apis"):
        items = []
    else:
        return error_response(
            422,
            "Unprocessable Entity",
            "unknown-discover-target",
            f"target must be one of: methods, agents, tools, apis (got {target!r})",
        )

    return json_response(
        200,
        "OK",
        {
            "method": "DISCOVER",
            "agent_id": agent_doc.agent_id,
            "target": target,
            "items": items,
            "item_count": len(items),
            "issued_at": _utc_now_iso(),
        },
        method_name="DISCOVER",
    )


@method(
    name="DESCRIBE",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=[],
    optional_params=["format"],
    error_codes=[400, 405, 406],
    description=(
        "Return a structured characterization of the target. For an agent "
        "target this is the Agent Identity Document."
    ),
    intent="Retrieve a self-describing identity or manifest document.",
    actor="agent",
    outcome="The agent or server's identity document is returned.",
    capability="discovery",
    confidence=0.95,
    impact="informational",
    is_idempotent=True,
)
def handle_describe(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    accept = wire.header(request, "Accept", default=CONTENT_TYPE_JSON).lower()

    if "text/html" in accept:
        body = render_html(agent_doc).encode("utf-8")
        content_type = CONTENT_TYPE_HTML
    elif "yaml" in accept:
        body = agent_doc.to_yaml().encode("utf-8")
        content_type = CONTENT_TYPE_YAML
    else:
        body = agent_doc.to_json(pretty=True).encode("utf-8")
        content_type = CONTENT_TYPE_JSON

    return wire.AGTPResponse(
        status_code=200,
        status_text="OK",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Agent-ID": agent_doc.agent_id,
            # Header-first dispatch: this is an Agent Document, not a
            # Server Manifest. A renderer can pick the right view
            # without first parsing the body. See core/identity.py
            # for the catalog of document-type values.
            HEADER_DOCUMENT_TYPE: DOC_TYPE_AGENT_DOCUMENT,
        },
        body_bytes=body,
    )


@method(
    name="SUMMARIZE",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["source"],
    optional_params=["max_length", "style"],
    error_codes=[400, 405, 422],
    description="Return a condensed form of source content.",
    intent="Produce a condensed restatement of a body of source material.",
    actor="agent",
    outcome="A summary of the supplied source is returned.",
    capability="analysis",
    confidence=0.70,
    impact="informational",
    is_idempotent=True,
)
def handle_summarize(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["SUMMARIZE"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    source = str(params["source"])
    max_length = int(params.get("max_length", 200))
    truncated = source[:max_length]
    if len(source) > max_length:
        truncated += "..."

    return json_response(
        200,
        "OK",
        {
            "method": "SUMMARIZE",
            "agent_id": agent_doc.agent_id,
            "source_length": len(source),
            "summary_length": len(truncated),
            "summary": f"stub-summary: {truncated}",
            "style": params.get("style", "default"),
            "issued_at": _utc_now_iso(),
        },
        method_name="SUMMARIZE",
    )


@method(
    name="PLAN",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["goal"],
    optional_params=["constraints", "max_steps"],
    error_codes=[400, 405, 422],
    description="Produce an executable strategy without executing it.",
    intent="Derive an ordered sequence of steps that achieves a stated goal.",
    actor="agent",
    outcome="An executable plan of steps and dependencies is returned.",
    capability="analysis",
    confidence=0.65,
    impact="informational",
    is_idempotent=True,
)
def handle_plan(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["PLAN"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    goal = params["goal"]
    return json_response(
        200,
        "OK",
        {
            "method": "PLAN",
            "agent_id": agent_doc.agent_id,
            "goal": goal,
            "plan_id": _new_token("plan"),
            "constraints": params.get("constraints", []),
            "steps": [
                {"step": 1, "action": f"stub: analyze {goal!s}", "method": "QUERY"},
                {"step": 2, "action": "stub: gather supporting context", "method": "DISCOVER"},
                {"step": 3, "action": "stub: synthesize result", "method": "EXECUTE"},
            ],
            "issued_at": _utc_now_iso(),
        },
        method_name="PLAN",
    )


@method(
    name="EXECUTE",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=["plan_id"],
    optional_params=["parameters", "timeout"],
    error_codes=[400, 405, 422, 409],
    description="Run a plan or registered procedure.",
    intent="Carry out a previously-prepared plan against the server.",
    actor="agent",
    outcome="The plan is executed and a result transcript is returned.",
    capability="transaction",
    confidence=0.75,
    impact="reversible",
    is_idempotent=False,
)
def handle_execute(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["EXECUTE"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    started = _utc_now_iso()
    return json_response(
        200,
        "OK",
        {
            "method": "EXECUTE",
            "agent_id": agent_doc.agent_id,
            "plan_id": params["plan_id"],
            "execution_id": _new_token("exec"),
            "status": "stub-executed",
            "parameters": params.get("parameters", {}),
            "started_at": started,
            "completed_at": started,
        },
        method_name="EXECUTE",
    )


# ---------------------------------------------------------------------------
# Mechanics methods.
# ---------------------------------------------------------------------------


@method(
    name="DELEGATE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=["task", "sub_agent"],
    optional_params=["scope", "deadline"],
    error_codes=[400, 405, 422],
    description="Transfer a task with scoped authority to a sub-agent.",
    intent="Hand a task to a sub-agent under a verifiable authority chain.",
    actor="agent",
    outcome="The sub-agent accepts and a delegation handle is returned.",
    capability="transaction",
    confidence=0.85,
    impact="reversible",
    is_idempotent=False,
)
def handle_delegate(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["DELEGATE"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    return json_response(
        200,
        "OK",
        {
            "method": "DELEGATE",
            "agent_id": agent_doc.agent_id,
            "delegated_to": params["sub_agent"],
            "task": params["task"],
            "sub_session_id": _new_token("sub"),
            "scope_granted": params.get("scope", "default"),
            "deadline": params.get("deadline"),
            "issued_at": _utc_now_iso(),
        },
        method_name="DELEGATE",
    )


@method(
    name="ESCALATE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["decision_point"],
    optional_params=["context", "target_authority"],
    error_codes=[400, 405, 422],
    description="Defer a decision to a human or higher-authority agent.",
    intent="Promote a task to higher authority for resolution.",
    actor="agent",
    outcome="The escalation is recorded; the new authority acknowledges.",
    capability="notification",
    confidence=0.85,
    impact="reversible",
    is_idempotent=True,
)
def handle_escalate(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["ESCALATE"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    return json_response(
        200,
        "OK",
        {
            "method": "ESCALATE",
            "agent_id": agent_doc.agent_id,
            "decision_point": params["decision_point"],
            "escalation_id": _new_token("esc"),
            "target_authority": params.get("target_authority", "human"),
            "context": params.get("context"),
            "status": "pending",
            "issued_at": _utc_now_iso(),
        },
        method_name="ESCALATE",
    )


@method(
    name="CONFIRM",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["attestation_target"],
    optional_params=["decision", "rationale"],
    error_codes=[400, 405, 422],
    description="Attest to a prior action; resolves an outstanding ESCALATE.",
    intent="Acknowledge and bind a previously-agreed action.",
    actor="agent",
    outcome="The acknowledgement is recorded and the action is bound.",
    capability="transaction",
    confidence=0.95,
    impact="reversible",
    is_idempotent=True,
)
def handle_confirm(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["CONFIRM"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    return json_response(
        200,
        "OK",
        {
            "method": "CONFIRM",
            "agent_id": agent_doc.agent_id,
            "attestation_target": params["attestation_target"],
            "attestation_id": _new_token("att"),
            "decision": params.get("decision", "confirmed"),
            "rationale": params.get("rationale"),
            "attested_at": _utc_now_iso(),
        },
        method_name="CONFIRM",
    )


@method(
    name="SUSPEND",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=[],
    optional_params=["reason", "session_id", "ttl_seconds", "synthesis_id"],
    error_codes=[400, 405],
    description=(
        "Pause the session and issue a resumption nonce. When "
        "synthesis_id is supplied, the named synthesis is cleared "
        "from the server's session-scoped registry."
    ),
    intent="Pause a session or release a synthesis until further notice.",
    actor="agent",
    outcome="The session or synthesis is suspended; a resumption nonce is returned.",
    capability="modification",
    confidence=0.90,
    impact="reversible",
    is_idempotent=False,
)
def handle_suspend(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    ttl = int(params.get("ttl_seconds", 3600))
    nonce = secrets.token_urlsafe(16)

    cleared_synthesis: Optional[str] = None
    syn_id = params.get("synthesis_id")
    if syn_id:
        # Prefer the runtime's expire() so both the active-plans
        # dict and the legacy SYNTHESES registry are cleared in one
        # atomic move. Fall back to the legacy registry directly if
        # no runtime is attached (older test fixtures).
        runtime = getattr(server_state, "synthesis_runtime", None)
        if runtime is not None:
            if runtime.expire(str(syn_id)):
                cleared_synthesis = str(syn_id)
        else:
            from server.negotiation import SYNTHESES
            if SYNTHESES.remove(str(syn_id)):
                cleared_synthesis = str(syn_id)

    return json_response(
        200,
        "OK",
        {
            "method": "SUSPEND",
            "agent_id": agent_doc.agent_id,
            "session_id": params.get("session_id", _new_token("sess")),
            "resumption_nonce": nonce,
            "expires_at": _utc_offset_iso(ttl),
            "reason": params.get("reason"),
            "synthesis_cleared": cleared_synthesis,
            "suspended_at": _utc_now_iso(),
        },
        method_name="SUSPEND",
    )


@method(
    name="PROPOSE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=["name"],
    optional_params=["parameters", "outcome", "description", "expect_accept"],
    error_codes=[400, 405, 422],
    description=(
        "Submit a verb proposal for negotiation. Returns 200 with a "
        "Synthesis on accept, or 422 with a refusal reason or a "
        "counter_proposal body."
    ),
    intent="Submit a new method proposal for runtime instantiation.",
    actor="agent",
    outcome="Acceptance with synthesis_id, a counter-proposal, or refusal is returned.",
    capability="transaction",
    confidence=0.70,
    impact="informational",
    is_idempotent=False,
)
def handle_propose(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    """
    PROPOSE handler — §7 of ``agtp-api``.

    Outcome status codes:

      * **400 Bad Request**        — body malformed (invalid JSON,
                                     missing required field, bad
                                     semantic block, bad schema).
      * **263 Proposal Approved**  — synthesis instantiated.
      * **463 Proposal Rejected**  — server refuses (out-of-scope,
                                     policy-refused, composition-
                                     impossible, ambiguous).
      * **261 Negotiation In Progress** — proposal queued for async
                                          evaluation (only when the
                                          server opts in via
                                          ``policies.synthesis.async_evaluation_enabled``).

    The audit log captures every outcome at the end of the handler.
    """
    # Local imports avoid module-load circular dependencies.
    from core import status as status_codes
    from core.endpoint import EndpointSpec, SemanticBlock
    from server.audit import record_propose
    from server.negotiation import find_counter_proposal
    from server.proposal_store import (
        ProposalStore, hash_proposal_body,
    )
    from server.synthesis_duration import (
        compute_expiration, parse_duration,
    )

    # ----- 1. Body well-formedness — 400. -----
    try:
        params = parse_body(request)
    except ValueError as exc:
        resp = status_codes.bad_request_for_propose(
            issue=status_codes.BAD_REQUEST_ISSUE_INVALID_JSON,
            explanation=f"PROPOSE body could not be parsed: {exc}",
        )
        record_propose(
            server_state, agent_doc=agent_doc,
            proposal_body=request.body_bytes, decision="malformed",
        )
        return resp

    # ----- 2. Required-field validation — 400. -----
    name_value = params.get("name") or ""
    if not isinstance(name_value, str) or not name_value.strip():
        resp = status_codes.bad_request_for_propose(
            issue=status_codes.BAD_REQUEST_ISSUE_MISSING_REQUIRED_FIELD,
            explanation="PROPOSE body missing required field 'name'",
            details={"missing": ["name"]},
        )
        record_propose(
            server_state, agent_doc=agent_doc,
            proposal_body=params, decision="malformed",
        )
        return resp

    # ----- 3. Semantic-block validation when present — 400. -----
    semantic_data = params.get("semantic")
    if semantic_data is not None:
        if not isinstance(semantic_data, dict):
            resp = status_codes.bad_request_for_propose(
                issue=status_codes.BAD_REQUEST_ISSUE_MALFORMED_SEMANTIC,
                explanation=(
                    "PROPOSE body 'semantic' must be a JSON object"
                ),
                details={"actual_type": type(semantic_data).__name__},
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="malformed",
            )
            return resp
        # Build the SemanticBlock to surface any structural issues
        # the dataclass can detect (most validation is value-range,
        # which the proposal_spec build will skip; this is a cheap
        # well-formedness gate).
        try:
            SemanticBlock.from_dict(semantic_data)
        except (TypeError, ValueError) as exc:
            resp = status_codes.bad_request_for_propose(
                issue=status_codes.BAD_REQUEST_ISSUE_MALFORMED_SEMANTIC,
                explanation=f"PROPOSE semantic block malformed: {exc}",
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="malformed",
            )
            return resp

    # ----- 4. JSON Schema well-formedness (input / output) — 400. -----
    for key in ("input_schema", "output_schema"):
        schema_val = params.get(key)
        if schema_val is not None and not isinstance(schema_val, dict):
            resp = status_codes.bad_request_for_propose(
                issue=status_codes.BAD_REQUEST_ISSUE_MALFORMED_SCHEMA,
                explanation=(
                    f"PROPOSE body {key!r} must be a JSON Schema object"
                ),
                details={"field": key},
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="malformed",
            )
            return resp

    # ----- 5. Synthesis-disabled gate — 463 policy-refused. -----
    server_config = getattr(server_state, "config", None)
    server_policy = (
        getattr(server_config, "policy", None) if server_config else None
    )
    if server_policy is not None and not getattr(
        server_policy, "synthesis_enabled", True
    ):
        resp = status_codes.proposal_rejected(
            reason=status_codes.PROPOSAL_REASON_POLICY_REFUSED,
            explanation=(
                "this server has disabled runtime synthesis "
                "(policies.synthesis_enabled = false); register the "
                "endpoint explicitly to invoke it"
            ),
            extra={"agent_id": agent_doc.agent_id},
        )
        record_propose(
            server_state, agent_doc=agent_doc,
            proposal_body=params, decision="rejected",
            reason=status_codes.PROPOSAL_REASON_POLICY_REFUSED,
        )
        return resp

    # Build the structured proposal. The catalog gate at the top of
    # dispatch already refused names not in the AGTP verb list; this
    # call packages the body for the synthesis runtime and the
    # counter-proposal helper.
    proposal_spec = EndpointSpec.from_proposal(params)

    # ----- 6. Persistent / duration parsing — 400 on bad duration. ----
    persistent_flag = bool(params.get("persistent") or False)
    requested_duration_raw = params.get("requested_duration")
    requested_seconds: Optional[float] = None
    if requested_duration_raw is not None:
        try:
            requested_seconds = parse_duration(str(requested_duration_raw))
        except ValueError as exc:
            resp = status_codes.bad_request_for_propose(
                issue=status_codes.BAD_REQUEST_ISSUE_MISSING_REQUIRED_FIELD,
                explanation=f"PROPOSE 'requested_duration' malformed: {exc}",
                details={"requested_duration": requested_duration_raw},
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="malformed",
            )
            return resp

    # ----- 7. Async path — 261 Negotiation In Progress. -----
    async_enabled = bool(
        getattr(server_state, "config", None)
        and getattr(server_state.config, "synthesis", None)
        and getattr(
            server_state.config.synthesis, "async_evaluation_enabled", False,
        )
    )
    store: Optional[ProposalStore] = getattr(
        server_state, "proposal_store", None
    )
    if async_enabled and store is not None:
        proposal_id = store.create(
            agent_id=agent_doc.agent_id,
            proposal_body=params,
            persistent=persistent_flag,
            requested_seconds=requested_seconds,
        )
        record_propose(
            server_state, agent_doc=agent_doc,
            proposal_body=params, decision="pending",
            proposal_id=proposal_id,
        )
        return status_codes.negotiation_in_progress(
            proposal_id=proposal_id,
            polling_path="/proposals",
            evaluation_started_at=store.evaluation_started_at(proposal_id),
            max_evaluation_duration=store.max_evaluation_duration_str(),
        )

    # ----- 8. Sync attempt — 263 on accept. -----
    runtime = getattr(server_state, "synthesis_runtime", None)
    if runtime is not None:
        available = [spec_to_endpoint_spec(s) for s in REGISTRY.values()]
        plan = runtime.attempt_synthesis(proposal_spec, available)
        if plan is not None:
            expires_at, granted_duration_str = compute_expiration(
                config=server_config,
                persistent=persistent_flag,
                requested_seconds=requested_seconds,
            )
            synthesis_id = runtime.instantiate(
                plan,
                expires_at=expires_at,
                persistent=persistent_flag,
            )
            first_step = plan.steps[0]
            param_mapping = {
                src.value: target
                for target, src in first_step.parameter_source.items()
                if src.kind == "proposal" and isinstance(src.value, str)
            }
            synthesis_detail: Dict[str, Any] = {
                "target_method": first_step.method_name,
                "parameter_mapping": param_mapping,
                "description": plan.description or "",
                "proposal_name": proposal_spec.name,
            }
            if (
                len(plan.steps) > 1
                or (plan.policy_name and plan.policy_name != "passthrough")
            ):
                synthesis_detail["plan"] = plan.to_dict()

            resp = status_codes.proposal_approved(
                synthesis_id=synthesis_id,
                endpoint=proposal_spec.to_dict(),
                persistent=persistent_flag,
                expires_at=expires_at.isoformat().replace("+00:00", "Z")
                if expires_at is not None else None,
                granted_duration=granted_duration_str,
                extra={
                    "synthesis": synthesis_detail,
                    "agent_id": agent_doc.agent_id,
                },
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="accepted",
                synthesis_id=synthesis_id,
                granted_duration=granted_duration_str,
            )
            return resp

    # ----- 9. Rejection paths — 463. -----
    counter = find_counter_proposal(proposal_spec, REGISTRY)
    if counter is not None:
        resp = status_codes.proposal_rejected(
            reason=status_codes.PROPOSAL_REASON_OUT_OF_SCOPE,
            explanation=(
                f"server cannot fulfill {proposal_spec.name!r}; see "
                f"counter_proposal for the nearest match this server "
                f"is willing to accept"
            ),
            counter_proposal=counter,
            extra={"agent_id": agent_doc.agent_id},
        )
        record_propose(
            server_state, agent_doc=agent_doc,
            proposal_body=params, decision="rejected",
            reason=status_codes.PROPOSAL_REASON_OUT_OF_SCOPE,
        )
        return resp

    resp = status_codes.proposal_rejected(
        reason=status_codes.PROPOSAL_REASON_COMPOSITION_IMPOSSIBLE,
        explanation=(
            f"server cannot compose endpoint for {proposal_spec.name!r} "
            f"from existing primitives"
        ),
        extra={"agent_id": agent_doc.agent_id},
    )
    record_propose(
        server_state, agent_doc=agent_doc,
        proposal_body=params, decision="rejected",
        reason=status_codes.PROPOSAL_REASON_COMPOSITION_IMPOSSIBLE,
    )
    return resp


@method(
    name="NOTIFY",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=["event"],
    optional_params=["recipient", "priority", "payload"],
    error_codes=[400, 405, 422],
    description="Asynchronous push of information to a recipient.",
    intent="Deliver an outbound signal for awareness, without expecting action.",
    actor="agent",
    outcome="The recipient acknowledges receipt of the signal.",
    capability="notification",
    confidence=0.85,
    impact="informational",
    is_idempotent=False,
)
def handle_notify(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["NOTIFY"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    return json_response(
        200,
        "OK",
        {
            "method": "NOTIFY",
            "agent_id": agent_doc.agent_id,
            "event": params["event"],
            "recipient": params.get("recipient", agent_doc.agent_id),
            "priority": params.get("priority", "normal"),
            "delivery_id": _new_token("ntf"),
            "delivered_at": _utc_now_iso(),
            "status": "delivered",
        },
        method_name="NOTIFY",
    )


# ---------------------------------------------------------------------------
# Phase-2 endpoint serving.
# ---------------------------------------------------------------------------
#
# When the endpoint registry resolves a (method, path) hit, control
# flows through ``_serve_endpoint``: validate the input body against
# the spec's input schema, enforce required_scopes against the
# agent's declared scopes, build an ``EndpointContext`` for the
# public handler API, call the handler, and translate the returned
# ``EndpointResponse`` / ``EndpointError`` into an ``AGTPResponse``.
#
# Handlers come back from ``handler_resolution.resolve_handler``
# already wrapped in the public-API signature
# ``(EndpointContext) -> EndpointResponse | EndpointError``. The
# wrapper here is the only place that knows about both the wire
# layer and the public layer; handler authors stay above it.


def _build_endpoint_context(
    request: wire.AGTPRequest,
    spec: "EndpointSpec",
    body: Dict[str, Any],
    agent_doc: AgentDocument,
    server_state: ServerState,
) -> Any:
    """Construct an :class:`agtp.handlers.EndpointContext` from the
    raw request + validated body."""
    from agtp.handlers import EndpointContext
    headers_lower = {
        k.lower(): v for k, v in (request.headers or {}).items()
    }
    # §10 optional request headers. Authority-Scope is parsed here
    # but validated against the agent's declared scopes earlier in
    # the dispatcher (so handlers see a pre-validated list).
    authority_scope_raw = headers_lower.get("authority-scope", "")
    authority_scope = [
        s.strip() for s in authority_scope_raw.split(",") if s.strip()
    ]
    session_id = headers_lower.get("session-id") or None
    task_id = headers_lower.get("task-id") or None
    # Phase B mTLS: when handle_connection verified a client cert,
    # it stashed a VerifiedCert on the request as the runtime
    # attribute ``verified_cert``. Surface it into the EndpointContext
    # as agent_verified + agent_cert_fingerprint so handlers can read
    # the trust signal directly.
    verified_cert = getattr(request, "verified_cert", None)
    agent_verified = verified_cert is not None
    agent_cert_fingerprint = (
        verified_cert.fingerprint if verified_cert is not None else None
    )
    return EndpointContext(
        input=body,
        agent_id=agent_doc.agent_id if agent_doc is not None else "",
        principal_id=(
            getattr(agent_doc, "principal_id", "") if agent_doc is not None else ""
        ),
        agent_scopes=list(getattr(agent_doc.requires, "scopes", []) or []),
        authority_scope=authority_scope,
        session_id=session_id,
        task_id=task_id,
        server_state=server_state,
        request_id=headers_lower.get("request-id", ""),
        method=request.method.upper(),
        path=getattr(request, "path", "/") or "/",
        headers=headers_lower,
        agent_verified=agent_verified,
        agent_cert_fingerprint=agent_cert_fingerprint,
    )


def _check_required_scopes(
    spec: "EndpointSpec",
    agent_doc: AgentDocument,
) -> Optional[wire.AGTPResponse]:
    """Compare the endpoint's ``required_scopes`` against the agent's
    declared scopes. Return a 403 ``insufficient_scope`` response
    when one or more are missing; ``None`` when authority is
    satisfied (or no scopes are required)."""
    if not spec.required_scopes:
        return None
    from core import status as _status
    declared = set(getattr(agent_doc.requires, "scopes", []) or [])
    required = set(spec.required_scopes)
    missing = sorted(required - declared)
    if missing:
        return _status.insufficient_scope(
            spec.name, spec.path or "/", missing,
        )
    return None


def _translate_endpoint_result(
    result: Any,
    spec: "EndpointSpec",
) -> wire.AGTPResponse:
    """Translate an :class:`EndpointResponse` / :class:`EndpointError`
    (or anything else the handler returned) into a wire response.

    The output validator runs against the response body so handler
    bugs surface immediately — Phase 2 keeps this on unconditionally.
    """
    from agtp.handlers import EndpointError, EndpointResponse
    from server.schema_validation import (
        OutputValidationError,
        validate_output,
    )

    if isinstance(result, EndpointResponse):
        try:
            validated = validate_output(spec, result.body or {})
        except OutputValidationError as exc:
            return error_response(
                500,
                "Internal Server Error",
                "output-validation-failed",
                f"handler response did not match the endpoint's output "
                f"schema: {exc}",
                extra={"field": exc.field, "schema_path": exc.schema_path},
            )
        wire_response = json_response(
            result.status,
            "OK" if 200 <= result.status < 300 else "Error",
            validated,
            method_name=spec.name,
            extra_headers=result.headers or None,
        )
        # Plumb the handler's attribution_extra dict to _finalize_response
        # via a private stash on the wire response. The finalizer reads
        # and removes it before serialization; nothing in the wire
        # output layer sees this attribute.
        if result.attribution_extra is not None:
            wire_response._attribution_extra = dict(result.attribution_extra)
        return wire_response
    # Built-in handlers (DISCOVER /methods, QUERY /proposals, ...)
    # occasionally need to return non-output-schema responses
    # (404 not-found, 261 in-progress). Pass-through a raw
    # ``wire.AGTPResponse`` without running output validation.
    if isinstance(result, wire.AGTPResponse):
        return result
    if isinstance(result, EndpointError):
        # Refuse codes the spec didn't declare — handler bugs.
        if result.code not in (spec.errors or []):
            return error_response(
                500,
                "Internal Server Error",
                "undeclared-error-code",
                f"handler returned undeclared error code "
                f"{result.code!r}; declared: "
                f"{', '.join(spec.errors) or '(none)'}",
            )
        body: Dict[str, Any] = {
            "error": {
                "code": result.code,
                "method": spec.name,
                "path": spec.path or "/",
                "message": result.message,
            }
        }
        if result.details is not None:
            body["error"]["details"] = result.details
        from core import status as _status
        return _status._build(_status.UNPROCESSABLE, body=body)

    # Anything else is a handler bug; surface it as a 500.
    return error_response(
        500,
        "Internal Server Error",
        "bad-handler-return-type",
        f"handler returned {type(result).__name__}; expected "
        f"EndpointResponse or EndpointError",
    )


def _serve_endpoint(
    spec: "EndpointSpec",
    handler: Optional[Any],
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    """The Phase-2 endpoint hot path. Runs every gate (body parse,
    input validation, authority) before the handler, and translates
    the handler's return value (or exception) into a wire response
    afterwards.
    """
    from server.schema_validation import (
        InputValidationError,
        validate_input,
    )

    # Parse the body; reuse the existing helper so JSON / empty /
    # malformed shapes all behave consistently with the older
    # method-only path.
    try:
        body = parse_body(request)
    except ValueError as exc:
        return error_response(
            400, "Bad Request", "invalid-body", str(exc),
        )

    # Merge query-string parameters into the input. Query parameters
    # ride alongside body parameters and validate against the same
    # input schema. **Body wins on key conflicts** — the documented
    # contract is that authoritative input lives in the body; the
    # query string is a convenience for callers that want path-style
    # URLs (e.g., ``SCHEDULE /meeting?date=050526``).
    query_params = dict(getattr(request, "query", {}) or {})
    if query_params:
        merged = dict(query_params)
        if isinstance(body, dict):
            merged.update(body)
        body = merged

    try:
        validated = validate_input(spec, body)
    except InputValidationError as exc:
        return error_response(
            422, "Unprocessable",
            "input-validation-failed",
            str(exc),
            extra={
                "field": exc.field,
                "schema_path": exc.schema_path,
                "method": spec.name,
                "path": spec.path or "/",
            },
        )

    auth_err = _check_required_scopes(spec, agent_doc)
    if auth_err is not None:
        return auth_err

    if handler is None:
        # Endpoint registered without a resolved handler — Phase-1
        # registries can store a None handler. Surface as 500 rather
        # than a confusing 405.
        return error_response(
            500,
            "Internal Server Error",
            "no-handler-resolved",
            f"endpoint ({spec.name}, {spec.path}) has no resolved "
            f"handler bound to it",
        )

    ctx = _build_endpoint_context(
        request, spec, validated, agent_doc, server_state,
    )

    # Operational-module dispatch hooks (M9). Hooks may short-circuit
    # the handler — used by mod_cache to serve cached responses. Hooks
    # run only when the server has a registered HookRegistry; the bare
    # tests-and-fixtures path skips this entirely.
    hooks = getattr(server_state, "hook_registry", None)
    short_circuit_result = None
    if hooks is not None:
        short_circuit_result = hooks.run_before(spec, ctx, server_state)

    if short_circuit_result is not None:
        result = short_circuit_result
    else:
        try:
            result = handler(ctx)
        except Exception as exc:  # noqa: BLE001
            # Expected error conditions ride EndpointError; bare
            # exceptions are bugs. Log them and return 500 — Phase 2's
            # logging hook is just stderr for now.
            import sys as _sys
            import traceback as _tb
            print(
                f"[server] handler for ({spec.name}, {spec.path}) raised "
                f"{type(exc).__name__}: {exc}",
                file=_sys.stderr,
            )
            _tb.print_exc(file=_sys.stderr)
            return error_response(
                500,
                "Internal Server Error",
                "handler-exception",
                f"{type(exc).__name__}: {exc}",
            )

    if hooks is not None and result is not None:
        hooks.run_after(spec, ctx, result, server_state)

    return _translate_endpoint_result(result, spec)


# ---------------------------------------------------------------------------
# Public dispatch helper used by the server.
# ---------------------------------------------------------------------------


def _stamp_deprecation_header(
    response: wire.AGTPResponse,
    method_name: str,
) -> wire.AGTPResponse:
    """Phase-6 hook: when ``method_name`` is a deprecated catalog
    verb, stamp the ``AGTP-Catalog-Warning`` advisory header on
    ``response`` so clients can surface a migration prompt to the
    user. The request itself processes normally — deprecation is
    advisory, not a refusal.

    Header shape:

        AGTP-Catalog-Warning: deprecated; successor=AUDIT; removed_in=2.0.0

    Fields after ``deprecated`` are omitted when the catalog
    doesn't declare them.
    """
    from core.methods import deprecation_metadata, is_deprecated
    if not is_deprecated(method_name):
        return response
    meta = deprecation_metadata(method_name) or {}
    parts: List[str] = ["deprecated"]
    if meta.get("successor"):
        parts.append(f"successor={meta['successor']}")
    if meta.get("removed_in"):
        parts.append(f"removed_in={meta['removed_in']}")
    response.headers = dict(response.headers or {})
    response.headers["AGTP-Catalog-Warning"] = "; ".join(parts)
    return response


def _stamp_endpoint_deprecation_header(
    response: wire.AGTPResponse,
    spec: "EndpointSpec",
) -> wire.AGTPResponse:
    """Stamp the ``AGTP-Endpoint-Warning`` advisory header on
    ``response`` when the endpoint carries deprecation metadata.

    Parallel to :func:`_stamp_deprecation_header` but for
    endpoint-level deprecation. An endpoint can be deprecated even
    when its method+path verbs remain in the catalog — operators
    deprecate endpoints when they migrate callers to a different
    ``(method, path)``.

    Header shape:

        AGTP-Endpoint-Warning: deprecated;
          successor=RESERVE /rooms; removed_in=3.0.0

    The ``successor`` field is rendered as ``METHOD /path`` when both
    are present; just one of the two is emitted bare. Fields are
    omitted when the spec doesn't declare them.
    """
    if spec is None or spec.deprecated is None:
        return response
    dep = spec.deprecated
    parts: List[str] = ["deprecated"]
    if dep.successor_method or dep.successor_path:
        if dep.successor_method and dep.successor_path:
            successor = f"{dep.successor_method} {dep.successor_path}"
        else:
            successor = dep.successor_method or dep.successor_path or ""
        parts.append(f"successor={successor}")
    if dep.removed_in:
        parts.append(f"removed_in={dep.removed_in}")
    response.headers = dict(response.headers or {})
    response.headers["AGTP-Endpoint-Warning"] = "; ".join(parts)
    return response


def dispatch(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
    *,
    config: Optional[Any] = None,
) -> wire.AGTPResponse:
    """Public dispatch entry point. Calls :func:`_dispatch_inner`
    and stamps two advisory headers on the result:

      * ``AGTP-Catalog-Warning`` when the method is a deprecated
        catalog verb (Phase 6).
      * ``AGTP-Endpoint-Warning`` when the resolved endpoint
        carries endpoint-level deprecation metadata.

    Both headers are advisory; the request still processes
    normally.
    """
    method_name = request.method.upper()
    request_path = getattr(request, "path", "/") or "/"
    response = _dispatch_inner(
        request, server_state, agent_doc, config=config,
    )
    response = _stamp_deprecation_header(response, method_name)
    # Endpoint-level deprecation: look up the spec from the
    # registry post-dispatch. This fires whether the request
    # succeeded, failed validation, or refused on authority —
    # advisory headers ride every response shape.
    endpoint_registry = getattr(server_state, "endpoint_registry", None)
    if endpoint_registry is not None:
        hit = endpoint_registry.lookup(method_name, request_path)
        if hit is not None:
            spec, _ = hit
            response = _stamp_endpoint_deprecation_header(response, spec)
    return response


def _dispatch_inner(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
    *,
    config: Optional[Any] = None,
) -> wire.AGTPResponse:
    """
    Validate the request against the AGTP verb catalog and path
    grammar, apply the server's per-method policy, then dispatch to
    the registered handler.

    Resolution order:

      1. **Synthesis-Id** — if the header is present and names an
         active synthesis, the runtime walks its plan and returns;
         an unrecognized id returns 404 ``synthesis-not-found``.
      2. **459 Method Violation** — if the method name is
         not in the canonical AGTP method list (``core/methods.json``),
         refuse with close-match suggestions in the body.
      3. **460 Endpoint Violation** — if the request path
         is malformed or contains a verb token, refuse with the
         offending segment in the body.
      4. **405 Method Not Allowed (per policy)** — when the server's
         ``policies.methods`` block disallows the method, refuse
         before reaching the registry.
      5. **Redirect rewrite** — ``policies.methods.redirects``
         rewrites the (method, path) pair before dispatch.
      6. **REGISTRY lookup** — unchanged from prior revisions: the
         registry resolves the method to a handler and runs the
         capability check. Methods absent from the registry but
         present in the catalog (e.g., recipe-based syntheses)
         continue to flow through the Method-Grammar invitation
         path during the transition.
    """
    method_name = request.method.upper()
    request_path = getattr(request, "path", "/") or "/"

    # Synthesis-Id execution pathway. Runs ahead of every other
    # check so a synthesis_id whose plan executes a method that
    # wouldn't otherwise be admissible (e.g. soft-denied) still
    # works — the original PROPOSE was the authorization. Inner
    # steps still go through dispatch() and fire capability /
    # scope checks.
    syn_id = wire.header(request, "Synthesis-Id")
    if syn_id:
        runtime = getattr(server_state, "synthesis_runtime", None)
        if runtime is not None and runtime.get(syn_id) is not None:
            return runtime.execute(syn_id, request, server_state, agent_doc)
        # Fall through: an unrecognized Synthesis-Id with no runtime
        # match returns the same not-found shape as a registry miss.
        if runtime is not None:
            return error_response(
                404, "Not Found",
                "synthesis-not-found",
                f"synthesis {syn_id!r} is not active on this server",
                extra={"synthesis_id": syn_id},
            )

    # 459 Method Violation. Local imports keep the methods
    # module free of core-side imports at module load time.
    from core import status as _status
    from core.methods import (
        ALL_PROTOCOL_VERBS,
        find_close_matches,
        is_approved_verb,
    )
    from core.path_grammar import PathGrammarError, validate_path

    # §10 Authority-Scope claim validation. The agent may declare a
    # subset of its scopes for this specific request via the
    # ``Authority-Scope`` header. Every claimed scope must appear in
    # the agent's declared scope set; otherwise refuse with 262
    # ``scope-claim-invalid``. The check is dispatcher-side so handlers
    # see only validated claims.
    auth_scope_raw = wire.header(request, "Authority-Scope")
    if auth_scope_raw and agent_doc is not None:
        claimed = [s.strip() for s in auth_scope_raw.split(",") if s.strip()]
        declared = set(getattr(agent_doc.requires, "scopes", []) or [])
        invalid = [s for s in claimed if s not in declared]
        if invalid:
            return _status.authorization_required(
                type=_status.AUTH_TYPE_SCOPE_REQUIRED,
                explanation=(
                    f"Authority-Scope header claims scopes not declared "
                    f"by the agent's document: {', '.join(invalid)}"
                ),
                details={
                    "code": "scope-claim-invalid",
                    "claimed": claimed,
                    "invalid": invalid,
                    "declared": sorted(declared),
                },
            )
    # Embedded-method names always pass the verb gate even if the
    # name was somehow stripped from the catalog — protocol
    # primitives must be answerable. This also covers the Synthesis-Id
    # rewrite path that hands an inner-step method through dispatch.
    catalog_admits = is_approved_verb(method_name) or method_name in REGISTRY

    # Honor the per-server method policy: a server that opts into
    # legacy verbs via ``policies.methods.legacy`` wants ``GET`` to
    # bypass the catalog refusal too.
    policy = getattr(server_state, "methods_policy", None)
    if not catalog_admits and policy is not None and method_name in policy.legacy:
        catalog_admits = True

    if not catalog_admits:
        suggestions = find_close_matches(method_name)
        return _status.method_grammar_violation(
            method_name, suggestions=suggestions,
        )

    # 460 Endpoint Violation.
    try:
        validate_path(request_path)
    except PathGrammarError as exc:
        return _status.endpoint_grammar_violation(
            request_path, exc.message, segment=exc.segment,
        )

    # 405 (per-server policy). Embedded methods bypass this gate so
    # the protocol primitives are always reachable, regardless of a
    # mis-authored policies.methods block.
    from core.methods import EMBEDDED_VERBS as _EMBEDDED
    if (
        policy is not None
        and method_name not in _EMBEDDED
        and not policy.is_method_allowed(method_name)
    ):
        return error_response(
            405,
            "Method Not Allowed",
            "method-not-allowed-by-policy",
            f"{method_name} is not allowed by this server's policies.methods",
            extra={"method": method_name},
        )

    # Redirects rewrite (method, path) before the registry lookup.
    if policy is not None:
        rewrite = policy.resolve_redirect(method_name, request_path)
        if rewrite is not None:
            method_name, rewritten_path = rewrite
            # The redirect's optional path component flows through too
            # so a ``Redirect: BOOK /room -> RESERVE /room`` rewrites
            # the path along with the method.
            if rewritten_path:
                request_path = rewritten_path

    # Phase-2 endpoint registry lookup. The registry binds
    # (method, path) pairs to handlers with full input/output
    # contracts. The hit / miss / wrong-method shapes:
    #
    #   * (method, path) hit         → validate, authorize, call.
    #   * path exists, method doesn't → 405 with allowed_methods.
    #   * path != "/" with no entry  → 404 endpoint-not-found.
    #   * path == "/" with no entry  → fall through to the method-only
    #                                  REGISTRY so embedded primitives
    #                                  keep working without TOML
    #                                  declarations.
    endpoint_registry = getattr(server_state, "endpoint_registry", None)
    if endpoint_registry is not None and endpoint_registry.count() > 0:
        hit = endpoint_registry.lookup(method_name, request_path)
        if hit is not None:
            ep_spec, ep_handler = hit
            return _serve_endpoint(
                ep_spec, ep_handler, request, server_state, agent_doc,
            )
        if request_path != "/":
            if endpoint_registry.has_path(request_path):
                allowed = endpoint_registry.methods_for_path(request_path)
                return _status.method_not_allowed(
                    method_name, request_path,
                    allowed_methods_for_path=sorted(allowed),
                )
            return _status.not_found(method_name, request_path)
        # path == "/" with no endpoint hit: fall through to method-only.

    spec = REGISTRY.get(method_name)
    if spec is None:
        # Method is in the AGTP catalog but no handler is registered
        # on this server. Return 405 — the caller's next move is
        # PROPOSE to negotiate instantiation.
        return error_response(
            405,
            "Method Not Allowed",
            "method-not-implemented",
            f"{method_name} is in the AGTP catalog but no handler is "
            f"registered on this server",
            extra={
                "method": method_name,
                "known_methods": sorted(REGISTRY.keys()),
            },
        )

    cap_err = check_capability(spec, agent_doc)
    if cap_err is not None:
        return cap_err

    if spec.handler is None:
        return error_response(
            500,
            "Internal Server Error",
            "no-handler-registered",
            f"{method_name} is registered without a handler",
        )
    return spec.handler(request, server_state, agent_doc)


# Verb admission is handled at the top of dispatch via the catalog
# lookup against core/methods.json. There is no separate probe header.


__all__ = [
    "MethodSpec",
    "REGISTRY",
    "ServerState",
    "method",
    "register_custom",
    "unregister",
    "dispatch",
    "parse_body",
    "json_response",
    "error_response",
    "require_params",
    "check_capability",
    "spec_to_dict",
    "spec_to_endpoint_spec",
]
