"""
The AGTP embedded method set.

Twelve methods, six cognitive plus six mechanics, registered through a
decorator. Each entry carries enough metadata that an Agent Method
Grammar (AMG) validator can later iterate this registry and verify
conformance: single uppercase token, imperative base form, action-intent
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
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from core import wire
from core.identity import (
    AgentDocument,
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
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

    The fields here are the AMG conformance surface. A grammar validator
    iterates REGISTRY and checks each entry: name shape, semantic class,
    parameter declarations, error codes, idempotency, etc.

    The `source` field distinguishes baseline AGTP/1.0 verbs from
    server-defined custom methods declared under AMG. Embedded methods
    carry source="agtp/1.0" and no namespace. Custom methods carry
    source="amg/1.0" and a non-empty namespace for disambiguation.
    """

    name: str
    category: str                     # "cognitive" | "mechanics" | "transact" | ...
    semantic_class: str               # AMG semantic class (e.g., "action-intent")
    idempotent: bool
    state_modifying: bool
    required_params: List[str]
    optional_params: List[str] = field(default_factory=list)
    error_codes: List[int] = field(default_factory=list)
    description: str = ""
    handler: Optional[HandlerFn] = None
    source: str = "agtp/1.0"
    namespace: Optional[str] = None


REGISTRY: Dict[str, MethodSpec] = {}


# Recognized source values. Anything else fails registration. v1 admits
# only these two; future revisions may add e.g. "experimental/0.1".
ALLOWED_SOURCES = {"agtp/1.0", "amg/1.0"}


def _validate_spec(
    name: str,
    source: str,
    namespace: Optional[str],
    description: str,
    error_codes: Optional[List[int]],
) -> None:
    """Shared sanity checks for both decorator and runtime registration."""
    if not name or not name.isupper() or " " in name:
        raise ValueError(
            f"method name must be a single uppercase token (got {name!r})"
        )
    if name in REGISTRY:
        raise RuntimeError(f"method {name!r} already registered")
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(ALLOWED_SOURCES)} (got {source!r})"
        )
    if source == "amg/1.0" and not namespace:
        raise ValueError(
            f"custom method {name!r} (source=amg/1.0) requires a namespace"
        )
    if source == "agtp/1.0" and namespace is not None:
        raise ValueError(
            f"embedded method {name!r} (source=agtp/1.0) cannot declare a namespace"
        )
    if not description:
        raise ValueError(f"method {name!r} requires a description")
    if not error_codes:
        raise ValueError(f"method {name!r} must declare at least one error code")


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
    source: str = "agtp/1.0",
    namespace: Optional[str] = None,
) -> Callable[[HandlerFn], HandlerFn]:
    """
    Decorator that registers a handler in REGISTRY.

    Names are normalized to uppercase. A second registration of the same
    name raises, since the registry is the single source of truth. The
    `source` and `namespace` fields default to embedded-method values;
    custom-method registration is normally done via register_custom().
    """

    def decorator(fn: HandlerFn) -> HandlerFn:
        normalized = name.upper()
        _validate_spec(normalized, source, namespace, description, error_codes)
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
            source=source,
            namespace=namespace,
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
    skip_amg_validation: bool = False,
) -> MethodSpec:
    """
    Register an AMG-flagged custom method at runtime.

    Source is forced to "amg/1.0" and namespace is required; everything
    else mirrors the @method decorator. Servers call this to expose
    custom verbs without modifying the core registry. Returns the
    registered MethodSpec so callers can inspect it.

    AMG validation runs by default. ``skip_amg_validation=True`` is
    available for tests that intentionally register specs which would
    otherwise fail (e.g. for substitution-table fixtures); production
    code should leave the gate on so registrations match the catalog
    grammar contract.
    """
    normalized = name.upper()
    _validate_spec(normalized, "amg/1.0", namespace, description, error_codes)

    # AMG validation: build the rich grammar form (promoting bare-string
    # parameter names to ParamSpec with safe defaults) and run all
    # nine passes. InvalidMethodError extends ValueError so existing
    # callers that ``except ValueError`` keep catching refusals.
    if not skip_amg_validation:
        from server.amg import (
            AMGMethodSpec,
            InvalidMethodError,
            ParamSpec as AMGParamSpec,
            validate as amg_validate,
        )
        amg_spec = AMGMethodSpec(
            name=normalized,
            semantic_class=semantic_class,
            category=category,
            description=description,
            idempotent=idempotent,
            state_modifying=state_modifying,
            required_params=[
                AMGParamSpec.from_bare_name(n) for n in (required_params or [])
            ],
            optional_params=[
                AMGParamSpec.from_bare_name(n) for n in (optional_params or [])
            ],
            error_codes=list(error_codes or []),
            source="amg/1.0",
            namespace=namespace,
        )
        amg_result = amg_validate(amg_spec, known_methods=set(REGISTRY.keys()))
        if not amg_result.valid:
            err = amg_result.error
            raise InvalidMethodError(
                f"AMG validation refused {normalized} at pass "
                f"'{err.pass_name}' [{err.code}]: {err.message}",
                result=amg_result,
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
        source="amg/1.0",
        namespace=namespace,
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
        "source": spec.source,
    }
    if spec.namespace is not None:
        payload["namespace"] = spec.namespace
    return payload


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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
        embedded: List[Dict[str, Any]] = []
        custom: List[Dict[str, Any]] = []
        for verb, m in REGISTRY.items():
            if not agent_doc.accepts_method(verb):
                continue
            entry = spec_to_dict(m)
            if m.source == "agtp/1.0":
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
    source="agtp/1.0",
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
            "Server-Agent-ID": agent_doc.agent_id,
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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
    source="agtp/1.0",
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
        # Local import avoids the negotiation -> methods import cycle.
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
    error_codes=[400, 405, 422, 460, 461],
    description=(
        "Submit a verb proposal for negotiation. Returns 200 with a "
        "Synthesis on accept, 460 with a refusal reason, or 461 with "
        "a counter-proposal."
    ),
    source="agtp/1.0",
)
def handle_propose(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["PROPOSE"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    # Local imports keep the methods module free of negotiation-side
    # imports at module load time. The policy module pulls in REGISTRY,
    # which would otherwise create a circular dependency.
    from core import status as status_codes
    from server.amg import AMGMethodSpec, validate as amg_validate
    from server.negotiation import (
        BasicNegotiationPolicy,
        SYNTHESES,
    )

    # AMG gate: malformed proposals are rejected with 460/ambiguous
    # before the negotiation policy ever sees them. This frees the
    # policy from having to re-implement structural / lexical /
    # reserved-name checks on every evaluation.
    amg_spec = AMGMethodSpec.from_proposal(params)
    # Allow proposals to name an embedded method as their own (the
    # accept-with-synthesis path); the AMG embedded-name check would
    # otherwise refuse it. We run validation against the universe of
    # known methods so substitution targets resolve.
    known = set(REGISTRY.keys())
    amg_result = amg_validate(amg_spec, known_methods=known)
    if not amg_result.valid:
        err_obj = amg_result.error
        # The reserved-embedded-method case is benign here: the
        # proposer named a verb the server already exposes, which is
        # exactly the accept-with-synthesis case. Let it through.
        if err_obj.code != "reserved-embedded-method":
            return status_codes.negotiation_refused(
                reason=status_codes.REFUSAL_AMBIGUOUS,
                explanation=err_obj.message,
                extra={"agent_id": agent_doc.agent_id, "amg_code": err_obj.code},
            )

    policy = BasicNegotiationPolicy()
    decision = policy.evaluate(params, REGISTRY)

    if decision.outcome == "accept":
        assert decision.synthesis is not None
        SYNTHESES.add(decision.synthesis)
        return json_response(
            200,
            "OK",
            {
                "method": "PROPOSE",
                "agent_id": agent_doc.agent_id,
                "outcome": "accept",
                "synthesis": decision.synthesis.to_dict(),
                "issued_at": _utc_now_iso(),
            },
            method_name="PROPOSE",
        )

    if decision.outcome == "counter":
        assert decision.counter_proposal is not None
        return status_codes.counter_proposal(decision.counter_proposal)

    assert decision.outcome == "refuse"
    return status_codes.negotiation_refused(
        decision.refusal_reason or status_codes.REFUSAL_OUT_OF_SCOPE,
        decision.refusal_explanation or "negotiation refused",
        extra={"agent_id": agent_doc.agent_id},
    )


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
    source="agtp/1.0",
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
# Public dispatch helper used by the server.
# ---------------------------------------------------------------------------


def dispatch(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    """
    Look up the requested method in REGISTRY and invoke its handler.

    Returns 501 for unknown methods. Returns 405 when the method exists
    but the target agent does not declare it. DESCRIBE has no required
    parameters and no body, so the capability check runs unconditionally
    for every method, including DESCRIBE.
    """
    method_name = request.method.upper()
    spec = REGISTRY.get(method_name)
    if spec is None:
        return error_response(
            501,
            "Not Implemented",
            "method-not-implemented",
            f"{method_name} is not part of the AGTP embedded method set",
            extra={"method": method_name, "known_methods": sorted(REGISTRY.keys())},
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


__all__ = [
    "ALLOWED_SOURCES",
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
]
