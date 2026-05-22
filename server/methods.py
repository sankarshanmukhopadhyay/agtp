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
from pathlib import Path
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


#: T4.1: protocol-reserved DISCOVER paths. The daemon implements
#: each identically across deployments; operator-registered custom
#: paths (``DISCOVER /products``, etc.) live in the endpoint
#: registry and MUST NOT collide with these (per the path-grammar
#: ``validate_discover_path`` check).
_DISCOVER_PATH_TO_TARGET: Dict[str, str] = {
    "/methods": "methods",
    "/agents":  "agents",
    "/tools":   "tools",
    "/apis":    "apis",
    "/genesis": "genesis",
}


_DISCOVER_LEGACY_WARNED: set = set()


def _warn_legacy_discover_target(agent_id: str) -> None:
    """One-shot stderr warning per agent_id when a caller uses the
    legacy ``target=`` body parameter form. Logged once so a chatty
    legacy client doesn't drown the server log."""
    if agent_id in _DISCOVER_LEGACY_WARNED:
        return
    _DISCOVER_LEGACY_WARNED.add(agent_id)
    import sys as _sys
    _sys.stderr.write(
        f"[server] DISCOVER from {agent_id[:12]}... used the legacy "
        f"body-`target` form. Migrate to path-keyed form "
        f"(DISCOVER /methods, /agents, /tools, /apis, /genesis) — "
        f"the body form is accepted during transition but will be "
        f"removed in a future revision.\n"
    )


@method(
    name="DISCOVER",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=[],
    optional_params=["target", "filter"],
    error_codes=[400, 404, 405, 422, 460],
    description=(
        "Enumerate available agents, methods, APIs, tools, or other "
        "operator-defined collections. T4.1 makes DISCOVER path-keyed: "
        "DISCOVER /methods, DISCOVER /agents, DISCOVER /tools, "
        "DISCOVER /apis, DISCOVER /genesis are protocol-reserved. The "
        "body-target form is accepted during transition."
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

    # T4.1 path-keyed dispatch. ``request.path`` is already grammar-
    # validated by the dispatcher; we just map it onto a target.
    request_path = (getattr(request, "path", "/") or "/").lower()
    path_target = _DISCOVER_PATH_TO_TARGET.get(request_path)

    # Back-compat: accept the legacy body-target form on requests
    # whose path is the default "/". When path is non-default the
    # path wins outright.
    body_target = ""
    if isinstance(params, dict):
        raw = params.get("target")
        if isinstance(raw, str) and raw:
            body_target = raw.lower()

    if path_target is not None:
        target = path_target
        if body_target and body_target != path_target:
            return error_response(
                400, "Bad Request",
                "discover-target-conflict",
                (
                    f"DISCOVER request path {request_path!r} maps to "
                    f"target {path_target!r}, but the body specifies "
                    f"target={body_target!r}. Remove the body "
                    f"`target` and rely on the path."
                ),
            )
    elif request_path == "/":
        if body_target:
            target = body_target
            _warn_legacy_discover_target(agent_doc.agent_id)
        else:
            # T4.1: no target supplied. Return the directory of
            # protocol-reserved DISCOVER endpoints so the caller
            # can navigate without prior knowledge of the spec.
            return _discover_index(agent_doc)
    else:
        # Path doesn't match a reserved root and isn't the default.
        # In a future revision the endpoint registry will accept
        # operator-defined custom paths here; until then any
        # non-reserved path returns 460 (the same 460 the path-
        # grammar would raise for collision).
        return error_response(
            460, "Endpoint Violation",
            "discover-unknown-path",
            (
                f"DISCOVER path {request_path!r} is not a "
                f"protocol-reserved root and no custom DISCOVER "
                f"endpoint is registered for it. Reserved roots: "
                f"{sorted(_DISCOVER_PATH_TO_TARGET)}."
            ),
            extra={"path": request_path},
        )

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
        # v2-aware lightweight entries: skills_summary + methods_count
        # plus Phase 5 trust posture (trust_tier, verification_path,
        # trust_warning, owner_id). The trust block answers "should I
        # trust this agent?" without requiring a follow-up DESCRIBE.
        from server.manifest import _summarize_skills  # local to avoid cycle
        items = []
        for aid in server_state.list_ids():
            doc = server_state.lookup(aid)
            if doc is None:
                continue
            entry = {
                "agent_id": doc.agent_id,
                "name": doc.name,
                "skills_summary": _summarize_skills(doc.skills),
                "methods_count": len(doc.requires.methods),
                "trust_tier": doc.trust_tier,
                "verification_path": doc.verification_path,
            }
            # Optional fields: only emitted when present so clients
            # can branch on "field present" cleanly.
            if doc.trust_warning:
                entry["trust_warning"] = doc.trust_warning
            if doc.owner_id:
                entry["owner_id"] = doc.owner_id
            items.append(entry)
    elif target in ("tools", "apis"):
        items = []
    elif target == "genesis":
        # Phase 4: serve the agent's signed Agent Genesis document.
        # Verifiers (chain inspector, registrar verifiers, governance
        # tooling) fetch via this endpoint to confirm that the cert
        # presented during mTLS is bound to a Genesis whose hash equals
        # subject-agent-id and whose signature verifies against a
        # trusted issuer key.
        genesis = None
        lookup = getattr(server_state, "lookup_genesis", None)
        if lookup is not None:
            genesis = lookup(agent_doc.agent_id)
        if genesis is None:
            return error_response(
                404,
                "Not Found",
                "genesis-not-found",
                (
                    f"agent {agent_doc.agent_id} has no Agent Genesis "
                    f"loaded on this server (transport-only identity)"
                ),
            )
        return json_response(
            200,
            "OK",
            genesis.to_dict(),
            method_name="DISCOVER",
        )
    else:
        return error_response(
            422,
            "Unprocessable Entity",
            "unknown-discover-target",
            f"target must be one of: methods, agents, tools, apis, genesis (got {target!r})",
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


def _discover_index(agent_doc: AgentDocument) -> wire.AGTPResponse:
    """Return the directory of DISCOVER endpoints available on this
    server. Used when a caller invokes ``DISCOVER`` on the bare
    URI without a target — gives them a self-describing way to
    learn what they can DISCOVER without prior catalog knowledge.

    Each entry carries a ``tier`` field per the Tier A/B/C taxonomy
    (see ``docs/endpoint-tiers.md``). The bare-`DISCOVER /` index
    surfaces only Tier A reserved roots — operator-registered
    (Tier B) DISCOVER endpoints surface via ``DISCOVER /methods``
    and the full manifest. RCNS-3 may surface negotiable (Tier C)
    patterns separately via ``DISCOVER /patterns``.
    """
    endpoints = []
    for path, target in sorted(_DISCOVER_PATH_TO_TARGET.items()):
        endpoints.append({
            "path": path,
            "target": target,
            "reserved": True,
            "tier": "A",
        })
    return json_response(
        200, "OK",
        {
            "method": "DISCOVER",
            "agent_id": agent_doc.agent_id,
            "target": "index",
            "endpoints": endpoints,
            "endpoint_count": len(endpoints),
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
    name="INSPECT",
    category="cognitive",
    semantic_class="action-intent",
    idempotent=True,
    state_modifying=False,
    required_params=["target"],
    optional_params=["audit_id", "agent_id"],
    error_codes=[400, 404, 422],
    description=(
        "Read a record from this server's audit store. target=audit "
        "returns the JWS for a given audit_id; target=chain_head "
        "returns the latest audit_id for a given agent_id; "
        "target=lifecycle returns the agent's full lifecycle event "
        "stream (ACTIVATE/DEACTIVATE/REVOKE history)."
    ),
    intent="Fetch a signed Attribution-Record by its identifier.",
    actor="agent",
    outcome="The signed JWS for the requested audit record is returned.",
    capability="discovery",
    confidence=0.95,
    impact="informational",
    is_idempotent=True,
)
def handle_inspect(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    """Phase-6 audit read surface. Two shapes:

      * ``{"target": "audit", "audit_id": "<64-hex>"}`` returns the
        JWS (compact-form) plus the decoded payload so the inspector
        can verify and walk in one round-trip.
      * ``{"target": "chain_head", "agent_id": "<64-hex>"}`` returns
        the latest known audit_id for that agent.

    Access control honors ``[audit].read_acl``:

      * ``public`` (default) — anyone can read.
      * ``agent_only`` — only the agent that emitted the record
        (caller's Agent-ID / mTLS cert must match).
      * ``operator_only`` — caller's verified cert public-key
        fingerprint must appear in
        ``[audit].read_acl_operator_keys``.
    """
    spec = REGISTRY["INSPECT"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    target = str(params["target"]).lower()

    # Identify the target this INSPECT is querying so the ACL check
    # can compare against the calling agent. For target=audit we
    # parse the JWS later (after the store lookup) to extract the
    # owner agent_id; for target=chain_head / target=lifecycle the
    # agent_id is in the request body directly.
    target_agent_id = ""
    if target in ("chain_head", "lifecycle"):
        target_agent_id = str(params.get("agent_id") or "").strip().lower()

    # Pre-check for chain_head / lifecycle (we know the target agent
    # before fetching). target=audit checks after JWS parse below.
    if target in ("chain_head", "lifecycle"):
        denial = _inspect_acl_check(
            request, server_state, agent_doc,
            target_agent_id=target_agent_id,
        )
        if denial is not None:
            return denial

    if target == "audit":
        audit_id = str(params.get("audit_id") or "").strip().lower()
        if not audit_id:
            return error_response(
                400, "Bad Request",
                "missing-audit-id",
                "target=audit requires an audit_id parameter",
            )
        # Read from the configured records root. Falls back to the
        # platform default when the operator didn't set one.
        record_store = _resolve_record_store(server_state)
        jws = record_store.read(audit_id) if record_store else None
        if jws is None:
            return error_response(
                404, "Not Found",
                "audit-record-not-found",
                f"no audit record stored under {audit_id!r}",
                extra={"audit_id": audit_id},
            )
        # Parse the JWS so callers get the payload alongside the
        # opaque form. The JWS itself is the verifiable artifact;
        # the payload is convenience.
        from server.signing import (
            AttributionRecordError as _ARErr,
            parse_attribution_record as _parse,
        )
        try:
            header, payload, _sig = _parse(jws)
        except _ARErr as exc:
            return error_response(
                500, "Internal Server Error",
                "stored-record-corrupt",
                f"stored JWS for {audit_id!r} did not parse: {exc}",
            )
        # Now that we know whose record this is, enforce the ACL.
        # operator_only also reaches this path (it doesn't depend on
        # the target agent, only on the caller's cert key), but
        # checking here keeps a single ACL gate per request.
        record_owner = str(payload.get("agent_id") or "").lower()
        denial = _inspect_acl_check(
            request, server_state, agent_doc,
            target_agent_id=record_owner,
        )
        if denial is not None:
            return denial
        return json_response(
            200, "OK",
            {
                "method": "INSPECT",
                "target": "audit",
                "audit_id": audit_id,
                "jws": jws,
                "header": header,
                "payload": payload,
                "issued_at": _utc_now_iso(),
            },
            method_name="INSPECT",
        )

    if target == "chain_head":
        agent_id_param = str(params.get("agent_id") or "").strip().lower()
        if not agent_id_param:
            return error_response(
                400, "Bad Request",
                "missing-agent-id",
                "target=chain_head requires an agent_id parameter",
            )
        chain_store = _resolve_chain_store(server_state)
        head = chain_store.head(agent_id_param) if chain_store else None
        if head is None:
            return error_response(
                404, "Not Found",
                "chain-head-not-found",
                f"no chain head recorded for agent {agent_id_param!r}",
                extra={"agent_id": agent_id_param},
            )
        return json_response(
            200, "OK",
            {
                "method": "INSPECT",
                "target": "chain_head",
                "agent_id": agent_id_param,
                "audit_id": head.audit_id,
                "last_at": head.at_iso,
                "issued_at": _utc_now_iso(),
            },
            method_name="INSPECT",
        )

    if target == "lifecycle":
        agent_id_param = str(params.get("agent_id") or "").strip().lower()
        if not agent_id_param:
            return error_response(
                400, "Bad Request",
                "missing-agent-id",
                "target=lifecycle requires an agent_id parameter",
            )
        lifecycle_store = _resolve_lifecycle_store(server_state)
        events = lifecycle_store.read_all(agent_id_param) if lifecycle_store else []

        # T4.2: each stored line is one of:
        #   * a JWS Compact (three dot-separated base64url segments) —
        #     mode=jws records, default form
        #   * "cose:<base64url(COSE_Sign1 bytes)>" — mode=scitt
        #     records
        # The reader sniffs the prefix per line so a mode flip
        # mid-stream stays readable. Bad lines surface as
        # {"parse_error": True} entries — better to expose a corrupt
        # entry than silently drop it from the historical record.
        from server.signing import (
            AttributionRecordError as _ARErr,
            parse_attribution_record as _parse,
        )
        decoded: List[Dict[str, Any]] = []
        for line in events:
            if line.startswith("cose:"):
                import base64 as _b64
                from server.cose import (
                    CoseError as _CErr,
                    parse_cose_payload as _parse_cose,
                )
                try:
                    raw = _b64.urlsafe_b64decode(
                        line[len("cose:"):] + "=" * (
                            -len(line[len("cose:"):]) % 4
                        )
                    )
                    parsed = _parse_cose(raw)
                except (ValueError, _CErr):
                    decoded.append({
                        "format": "cose",
                        "line": line,
                        "parse_error": True,
                    })
                    continue
                decoded.append({
                    "format": "cose",
                    "line": line,
                    "header": parsed["header"],
                    "payload": parsed["payload"],
                })
                continue
            # JWS Compact (default form).
            try:
                header, payload, _sig = _parse(line)
            except _ARErr:
                decoded.append({
                    "format": "jws", "jws": line, "parse_error": True,
                })
                continue
            decoded.append({
                "format": "jws", "jws": line,
                "header": header, "payload": payload,
            })
        return json_response(
            200, "OK",
            {
                "method": "INSPECT",
                "target": "lifecycle",
                "agent_id": agent_id_param,
                "events": decoded,
                "event_count": len(decoded),
                "issued_at": _utc_now_iso(),
            },
            method_name="INSPECT",
        )

    return error_response(
        422, "Unprocessable Entity",
        "unknown-inspect-target",
        f"target must be one of: audit, chain_head, lifecycle (got {target!r})",
    )


def _resolve_record_store(server_state: Any):
    """Resolve the AuditRecordStore for INSPECT lookups, sharing
    config knobs with ``_finalize_response``. Returns ``None`` when
    Attribution-Records are disabled — there's nothing to read in
    that case."""
    config = getattr(server_state, "config", None)
    audit = getattr(config, "audit", None) if config is not None else None
    if audit is None or not getattr(audit, "attribution_records_enabled", False):
        return None
    from server.audit_records import (
        AuditRecordStore, default_records_root,
    )
    root = getattr(audit, "records_root", "") or ""
    return AuditRecordStore(
        Path(root).expanduser() if root else default_records_root()
    )


def _resolve_chain_store(server_state: Any):
    """Same as :func:`_resolve_record_store` for the chain head store."""
    config = getattr(server_state, "config", None)
    audit = getattr(config, "audit", None) if config is not None else None
    if audit is None or not getattr(audit, "attribution_records_enabled", False):
        return None
    from server.audit_chain import (
        AuditChainStore, default_chain_head_root,
    )
    root = getattr(audit, "chain_head_root", "") or ""
    return AuditChainStore(
        Path(root).expanduser() if root else default_chain_head_root()
    )


def _resolve_lifecycle_store(server_state: Any):
    """Resolve the AuditLifecycleStore. Returns ``None`` when
    Attribution-Records are disabled — lifecycle events ride the
    same signing path as Attribution-Records, so the two opt-in
    together."""
    config = getattr(server_state, "config", None)
    audit = getattr(config, "audit", None) if config is not None else None
    if audit is None or not getattr(audit, "attribution_records_enabled", False):
        return None
    from server.audit_lifecycle import (
        AuditLifecycleStore, default_lifecycle_root,
    )
    root = getattr(audit, "lifecycle_root", "") or ""
    return AuditLifecycleStore(
        Path(root).expanduser() if root else default_lifecycle_root()
    )


def _inspect_acl_check(
    request: wire.AGTPRequest,
    server_state: Any,
    agent_doc: AgentDocument,
    *,
    target_agent_id: str,
) -> Optional[wire.AGTPResponse]:
    """Apply ``[audit].read_acl`` to an INSPECT request.

    Returns ``None`` when the caller is allowed; an AGTPResponse
    (401 / 403) when they aren't. Modes:

      * ``public`` — pass through.
      * ``agent_only`` — caller's agent_id MUST equal
        ``target_agent_id`` (case-insensitive). The Agent-ID
        header is authoritative; when mTLS is on, the dispatcher
        has already cross-checked the header against the verified
        cert (see ``server.mtls.CertVerifier``), so accepting the
        header value is equivalent to accepting the cert.
      * ``operator_only`` — the request MUST present a verified
        mTLS cert whose key fingerprint is listed in
        ``[audit].read_acl_operator_keys``.
    """
    config = getattr(server_state, "config", None)
    audit = getattr(config, "audit", None) if config is not None else None
    mode = (getattr(audit, "read_acl", "public") or "public") if audit else "public"

    if mode == "public":
        return None

    if mode == "agent_only":
        caller = (agent_doc.agent_id or "").lower() if agent_doc is not None else ""
        if not caller:
            return error_response(
                401, "Unauthorized",
                "inspect-acl-anonymous",
                "[audit].read_acl=agent_only refuses anonymous INSPECT; "
                "present an Agent-ID and (recommended) a verified mTLS cert.",
            )
        if caller != (target_agent_id or "").lower():
            return error_response(
                403, "Forbidden",
                "inspect-acl-cross-agent",
                f"agent {caller!r} cannot INSPECT records for "
                f"{target_agent_id!r} ([audit].read_acl=agent_only)",
                extra={
                    "caller_agent_id": caller,
                    "target_agent_id": target_agent_id,
                },
            )
        return None

    if mode == "operator_only":
        verified = getattr(request, "verified_cert", None)
        if verified is None:
            return error_response(
                401, "Unauthorized",
                "inspect-acl-no-cert",
                "[audit].read_acl=operator_only requires a verified "
                "mTLS client certificate.",
            )
        # Fingerprint is sha256(raw public key) for AGTP — same value
        # the daemon uses as the cert-derived Agent-ID. Operators add
        # entries to read_acl_operator_keys as that 64-hex string.
        allowed = {
            k.lower() for k in
            (getattr(audit, "read_acl_operator_keys", None) or [])
        }
        caller_key = (verified.agent_id or "").lower()
        if not caller_key or caller_key not in allowed:
            return error_response(
                403, "Forbidden",
                "inspect-acl-operator-not-listed",
                "cert public-key fingerprint is not on the operator "
                "ACL ([audit].read_acl_operator_keys).",
                extra={"caller_key": caller_key},
            )
        return None

    # Defensive — config loader rejects unknown modes, but if we
    # somehow get here, fail closed.
    return error_response(
        500, "Internal Server Error",
        "inspect-acl-unknown-mode",
        f"unrecognized [audit].read_acl mode: {mode!r}",
    )


def _lifecycle_auth_check(
    *,
    request: wire.AGTPRequest,
    server_state: Any,
    agent_doc: AgentDocument,
    event_type: str,
) -> Optional[wire.AGTPResponse]:
    """Apply ``[audit].lifecycle_auth`` to a lifecycle method call.

    Returns ``None`` when the caller is allowed; a 401/403
    AGTPResponse when refused.

    Modes:

      * ``open`` — pass through.
      * ``genesis_issuer`` — caller MUST present a verified mTLS
        cert whose public-key fingerprint equals the agent's
        Genesis ``issuer_public_key`` fingerprint. Agents without a
        loaded Genesis can't be lifecycle-managed (cryptographic
        accountability requires the issuer chain).
    """
    config = getattr(server_state, "config", None)
    audit = getattr(config, "audit", None) if config is not None else None
    mode = (getattr(audit, "lifecycle_auth", "open") or "open") if audit else "open"

    if mode == "open":
        return None

    if mode == "genesis_issuer":
        verified = getattr(request, "verified_cert", None)
        if verified is None:
            return error_response(
                401, "Unauthorized",
                "lifecycle-auth-no-cert",
                (
                    f"[audit].lifecycle_auth=genesis_issuer requires a "
                    f"verified mTLS client certificate for {event_type.upper()}"
                ),
            )
        lookup_genesis = getattr(server_state, "lookup_genesis", None)
        genesis = lookup_genesis(agent_doc.agent_id) if lookup_genesis else None
        if genesis is None:
            return error_response(
                403, "Forbidden",
                "lifecycle-auth-no-genesis",
                (
                    f"agent {agent_doc.agent_id} has no loaded Genesis; "
                    f"[audit].lifecycle_auth=genesis_issuer cannot verify "
                    f"the caller's authority"
                ),
            )
        from core.genesis import issuer_key_fingerprint
        try:
            expected = issuer_key_fingerprint(genesis)
        except Exception as exc:  # noqa: BLE001
            return error_response(
                500, "Internal Server Error",
                "lifecycle-auth-bad-genesis",
                (
                    f"could not derive issuer key fingerprint from "
                    f"agent's Genesis: {exc}"
                ),
            )
        caller_key = (verified.agent_id or "").lower()
        if caller_key != expected.lower():
            return error_response(
                403, "Forbidden",
                "lifecycle-auth-wrong-issuer",
                (
                    f"caller's cert key {caller_key!r} does not match "
                    f"the agent's Genesis issuer key {expected!r}"
                ),
                extra={
                    "caller_key": caller_key,
                    "expected_issuer_key": expected,
                },
            )
        return None

    # Defensive — config loader rejects unknown modes.
    return error_response(
        500, "Internal Server Error",
        "lifecycle-auth-unknown-mode",
        f"unrecognized [audit].lifecycle_auth mode: {mode!r}",
    )


# ---------------------------------------------------------------------------
# Phase 8: identity lifecycle methods.
# ---------------------------------------------------------------------------
#
# The daemon implements ACTIVATE / DEACTIVATE / REVOKE uniformly: each
# updates the target AgentDocument's ``status`` field and appends a
# signed lifecycle event to the agent's lifecycle stream
# (``audit/lifecycle/{agent_id}.jsonl``). The lifecycle stream is the
# AGTP-LOG-aligned read surface that regulators and chain inspectors
# walk to reconstruct an agent's identity history.
#
# Authorization is open in v1 — every caller can transition any
# agent's status. The audit trail is the accountability mechanism.
# Future revisions add cert-based and Authority-Scope-based gates;
# either fits cleanly on top of mod_agent_cert.


def _lifecycle_transition(
    *,
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
    event_type: str,
    new_status: str,
) -> wire.AGTPResponse:
    """Shared body for ACTIVATE / DEACTIVATE / REVOKE / REINSTATE / DEPRECATE.

    Updates ``agent_doc.status``, signs a lifecycle event, appends to
    the agent's lifecycle stream, persists the new status to disk,
    and returns a structured confirmation. Honors the no-op case
    where the agent is already in the target state (returns 200 with
    ``noop: true``)."""
    spec = REGISTRY[event_type.upper()]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    # Phase 8 T2.3 — lifecycle authorization. ``open`` (default)
    # falls through; ``genesis_issuer`` requires the caller's
    # verified cert key to match the agent's Genesis issuer.
    auth_denial = _lifecycle_auth_check(
        request=request,
        server_state=server_state,
        agent_doc=agent_doc,
        event_type=event_type,
    )
    if auth_denial is not None:
        return auth_denial

    reason = str(params.get("reason") or "").strip()
    previous_status = agent_doc.status

    if previous_status == new_status:
        return json_response(
            200, "OK",
            {
                "method": event_type.upper(),
                "agent_id": agent_doc.agent_id,
                "noop": True,
                "status": previous_status,
                "issued_at": _utc_now_iso(),
            },
            method_name=event_type.upper(),
        )

    # State transition. The in-memory mutation is the source of
    # truth for the rest of this request; the persist() call below
    # commits the change to disk so the next daemon restart picks
    # up the new status.
    agent_doc.status = new_status

    # Phase 8 T2.2: persist the new status to disk. Failure is
    # logged but doesn't fail the response — the in-memory mutation
    # is already authoritative for this process. Operators who care
    # about durability without restart-time gaps run a sync-after-
    # ACTIVATE hook in their orchestration.
    persist = getattr(server_state, "persist", None)
    if persist is not None:
        try:
            persist(agent_doc.agent_id)
        except OSError as exc:
            import sys as _sys
            print(
                f"[server] could not persist status for "
                f"{agent_doc.agent_id[:12]}... ({event_type}): {exc}",
                file=_sys.stderr,
            )

    # Emit lifecycle event when signing is configured. Failures here
    # are logged but don't fail the response — the state transition
    # is the source of truth; the audit stream is best-effort.
    lifecycle_store = _resolve_lifecycle_store(server_state)
    config = getattr(server_state, "config", None)
    signing_service = (
        getattr(config, "signing_service", None) if config is not None else None
    )
    audit_id = ""
    if lifecycle_store is not None and signing_service is not None:
        try:
            audit_id = _emit_lifecycle_event(
                signing_service=signing_service,
                lifecycle_store=lifecycle_store,
                config=config,
                event_type=event_type,
                agent_id=agent_doc.agent_id,
                previous_status=previous_status,
                new_status=new_status,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            import sys as _sys
            print(
                f"[server] lifecycle event emit failed for "
                f"{agent_doc.agent_id[:12]}... ({event_type}): {exc}",
                file=_sys.stderr,
            )

    body: Dict[str, Any] = {
        "method": event_type.upper(),
        "agent_id": agent_doc.agent_id,
        "previous_status": previous_status,
        "status": new_status,
        "event_type": event_type.lower(),
        "issued_at": _utc_now_iso(),
    }
    if reason:
        body["reason"] = reason
    if audit_id:
        body["audit_id"] = audit_id
    return json_response(200, "OK", body, method_name=event_type.upper())


def _emit_lifecycle_event(
    *,
    signing_service: Any,
    lifecycle_store: Any,
    config: Any,
    event_type: str,
    agent_id: str,
    previous_status: str,
    new_status: str,
    reason: str,
) -> str:
    """Sign a lifecycle event and append it to the agent's stream.

    Dispatches on ``[audit].mode``:

      * ``jws`` (default) — emit a JWS Compact Attribution-Record
        and store the compact string verbatim. ``audit_id`` =
        sha256(jws).
      * ``scitt`` — emit an RFC 9943 COSE_Sign1 statement over the
        same JSON payload, store as ``cose:<base64url>``.
        ``audit_id`` = sha256(COSE bytes).

    Returns the audit_id so the caller stamps it on the response.
    """
    from datetime import datetime as _dt, timezone as _tz
    issued_at = _dt.now(tz=_tz.utc).isoformat().replace("+00:00", "Z")
    server_id = (
        getattr(config.server, "server_id", "") or ""
        if config is not None and getattr(config, "server", None) is not None
        else ""
    )
    import uuid as _uuid
    response_id = f"resp-{_uuid.uuid4().hex[:12]}"
    extra: Dict[str, Any] = {
        "event_type": event_type.lower(),
        "previous_status": previous_status,
        "new_status": new_status,
    }
    if reason:
        extra["reason"] = reason

    audit = getattr(config, "audit", None) if config is not None else None
    mode = (getattr(audit, "mode", "jws") or "jws") if audit else "jws"

    if mode == "scitt":
        # SCITT mode: COSE_Sign1 over the canonical payload JSON.
        import json as _json
        from server.cose import build_cose_sign1, cose_audit_id
        payload = {
            "agent_id": agent_id,
            "server_id": server_id,
            "issued_at": issued_at,
            "response_id": response_id,
            "status": 200,
            "extra": extra,
        }
        # Canonical JSON so SCITT verifiers reproduce the same bytes
        # we signed.
        payload_bytes = _json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        cose_bytes = build_cose_sign1(
            private_key=signing_service._key,
            payload_bytes=payload_bytes,
            kid=signing_service.key_id,
        )
        lifecycle_store.append_cose(agent_id, cose_bytes)
        return cose_audit_id(cose_bytes)

    # Default: JWS Compact (same shape as Attribution-Record).
    record = signing_service.build_attribution_record(
        agent_id=agent_id,
        server_id=server_id,
        issued_at=issued_at,
        status=200,
        response_id=response_id,
        extra=extra,
    )
    lifecycle_store.append(agent_id, record.jws)
    return record.audit_id


@method(
    name="ACTIVATE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=[],
    optional_params=["reason"],
    error_codes=[400, 422],
    description=(
        "Transition the targeted agent to active status. Emits a "
        "signed lifecycle event into the agent's lifecycle stream. "
        "Repeated invocations on an already-active agent return "
        "200 with noop=true and emit no event."
    ),
    intent="Bring an agent into operational status.",
    actor="agent",
    outcome="The agent's status is set to active.",
    capability="discovery",
    confidence=0.95,
    impact="irreversible",
    is_idempotent=False,
)
def handle_activate(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    return _lifecycle_transition(
        request=request, server_state=server_state, agent_doc=agent_doc,
        event_type="activate", new_status="active",
    )


@method(
    name="DEACTIVATE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=[],
    optional_params=["reason"],
    error_codes=[400, 422],
    description=(
        "Transition the targeted agent to suspended status (a "
        "recoverable inactive state). Emits a signed lifecycle event. "
        "Repeated invocations on an already-suspended agent return "
        "200 with noop=true and emit no event."
    ),
    intent="Place an agent into a recoverable inactive state.",
    actor="agent",
    outcome="The agent's status is set to suspended.",
    capability="discovery",
    confidence=0.95,
    impact="reversible",
    is_idempotent=False,
)
def handle_deactivate(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    return _lifecycle_transition(
        request=request, server_state=server_state, agent_doc=agent_doc,
        event_type="deactivate", new_status="suspended",
    )


@method(
    name="REVOKE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=[],
    optional_params=["reason"],
    error_codes=[400, 422],
    description=(
        "Permanently retire the targeted agent. Sets status to "
        "retired and emits a signed lifecycle event. Per the spec, "
        "a retired agent's Genesis is archived; the Agent-ID is never "
        "reused. Repeated invocations on an already-retired agent "
        "return 200 with noop=true and emit no event."
    ),
    intent="Permanently retire an agent.",
    actor="agent",
    outcome="The agent's status is set to retired.",
    capability="discovery",
    confidence=0.95,
    impact="irreversible",
    is_idempotent=False,
)
def handle_revoke(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    return _lifecycle_transition(
        request=request, server_state=server_state, agent_doc=agent_doc,
        event_type="revoke", new_status="retired",
    )


@method(
    name="REINSTATE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=[],
    optional_params=["reason"],
    error_codes=[400, 422],
    description=(
        "Restore a previously revoked, deprecated, or suspended "
        "agent to active status. Emits a signed lifecycle event. "
        "Repeated invocations on an already-active agent return "
        "200 with noop=true and emit no event. Per AGTP-LOG §2 the "
        "Agent-ID is preserved across the transition — REINSTATE "
        "never mints a new identity."
    ),
    intent="Restore a non-active agent to operational status.",
    actor="agent",
    outcome="The agent's status is set to active.",
    capability="discovery",
    confidence=0.95,
    impact="reversible",
    is_idempotent=False,
)
def handle_reinstate(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    return _lifecycle_transition(
        request=request, server_state=server_state, agent_doc=agent_doc,
        event_type="reinstate", new_status="active",
    )


@method(
    name="DEPRECATE",
    category="mechanics",
    semantic_class="action-intent",
    idempotent=False,
    state_modifying=True,
    required_params=[],
    optional_params=["reason"],
    error_codes=[400, 422],
    description=(
        "Mark the targeted agent as deprecated. The agent stays "
        "operational (still accepts traffic) but signals planned "
        "retirement; clients SHOULD migrate. Emits a signed "
        "lifecycle event. Repeated invocations on an already-"
        "deprecated agent return 200 with noop=true and emit no "
        "event."
    ),
    intent="Signal that an agent is obsolete and pending retirement.",
    actor="agent",
    outcome="The agent's status is set to deprecated.",
    capability="discovery",
    confidence=0.95,
    impact="reversible",
    is_idempotent=False,
)
def handle_deprecate(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    return _lifecycle_transition(
        request=request, server_state=server_state, agent_doc=agent_doc,
        event_type="deprecate", new_status="deprecated",
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

    # ----- 1b. RCNS-2: unwrap the endpoint-keyed form. -----
    # PROPOSE bodies may arrive in either of two shapes:
    #
    #   legacy / method-only:  {"name": "RECONCILE", "path": "/x", ...}
    #   endpoint-keyed:        {"endpoint": {"method": "RECONCILE",
    #                                        "path": "/x", ...}}
    #
    # Both shapes carry the same information; the wrapped form is the
    # one RCNS-3 will emit programmatically when escalating a 404
    # into a negotiation. We normalize to the legacy top-level shape
    # internally so the rest of the handler stays unchanged.
    #
    # Mutual exclusivity: a body with both ``name`` and ``endpoint``
    # is malformed (the caller meant one or the other).
    endpoint_block = params.get("endpoint")
    if endpoint_block is not None:
        if not isinstance(endpoint_block, dict):
            resp = status_codes.bad_request_for_propose(
                issue=status_codes.BAD_REQUEST_ISSUE_MALFORMED_SCHEMA,
                explanation=(
                    "PROPOSE body 'endpoint' must be an object "
                    "({method, path, ...})"
                ),
                details={"field": "endpoint"},
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="malformed",
            )
            return resp
        if params.get("name"):
            resp = status_codes.bad_request_for_propose(
                issue=status_codes.BAD_REQUEST_ISSUE_MISSING_REQUIRED_FIELD,
                explanation=(
                    "PROPOSE body must carry either top-level 'name' "
                    "OR wrapped 'endpoint', not both"
                ),
                details={"conflict": ["name", "endpoint"]},
            )
            record_propose(
                server_state, agent_doc=agent_doc,
                proposal_body=params, decision="malformed",
            )
            return resp
        # Promote endpoint-block fields to the top-level shape that
        # ``EndpointSpec.from_proposal`` (and the rest of this
        # handler) consumes. ``method`` becomes ``name`` so the
        # required-field check in step 2 still applies cleanly.
        promoted = dict(params)
        promoted.pop("endpoint", None)
        if "method" in endpoint_block:
            promoted["name"] = endpoint_block["method"]
        for key in (
            "path", "input_schema", "output_schema",
            "description", "namespace", "category", "parameters",
            "semantic", "error_codes",
        ):
            if key in endpoint_block and key not in promoted:
                promoted[key] = endpoint_block[key]
        params = promoted

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
            # RCNS-2: surface recipe lineage so callers can diff a
            # contract against current recipes and detect when an
            # operator edit has bumped the version under them.
            if plan.recipe_name:
                synthesis_detail["recipe_name"] = plan.recipe_name
            if plan.recipe_version:
                synthesis_detail["recipe_version"] = plan.recipe_version
            if (
                len(plan.steps) > 1
                or (plan.policy_name and plan.policy_name != "passthrough")
            ):
                synthesis_detail["plan"] = plan.to_dict()

            # RCNS-2: include the resolved (method, path) at the top
            # level of the 263 body so callers don't have to dig into
            # ``endpoint``. This is the canonical form RCNS-3 will
            # consume when echoing a contract back to the caller.
            extras: Dict[str, Any] = {
                "synthesis": synthesis_detail,
                "agent_id": agent_doc.agent_id,
                "method": proposal_spec.name,
                "path": proposal_spec.path or "/",
            }
            resp = status_codes.proposal_approved(
                synthesis_id=synthesis_id,
                endpoint=proposal_spec.to_dict(),
                persistent=persistent_flag,
                expires_at=expires_at.isoformat().replace("+00:00", "Z")
                if expires_at is not None else None,
                granted_duration=granted_duration_str,
                extra=extras,
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
    agent_cert_extensions = _extensions_dict(verified_cert)
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
        agent_cert_extensions=agent_cert_extensions,
    )


def _extensions_dict(verified_cert: Any) -> Dict[str, Any]:
    """Project the parsed Agent-Cert extensions onto a plain dict.

    The agtp library cannot depend on server-side types; this helper
    flattens :class:`server.agent_cert_ext.AgentCertExtensions` into
    JSON-friendly types so ``EndpointContext.agent_cert_extensions``
    stays a plain dict regardless of how the dispatcher surfaces them.
    Empty dict when mTLS is off or no extensions are present.
    """
    if verified_cert is None:
        return {}
    ext = getattr(verified_cert, "extensions", None)
    if ext is None:
        return {}
    out: Dict[str, Any] = {}
    if ext.subject_agent_id is not None:
        out["subject_agent_id"] = ext.subject_agent_id
    if ext.principal_id is not None:
        out["principal_id"] = ext.principal_id
    if ext.authority_scopes is not None:
        out["authority_scopes"] = list(ext.authority_scopes)
    if ext.governance_zone is not None:
        out["governance_zone"] = ext.governance_zone
    if ext.trust_tier is not None:
        out["trust_tier"] = ext.trust_tier
    if ext.archetype is not None:
        out["archetype"] = ext.archetype
    if ext.activation_certificate_id is not None:
        out["activation_certificate_id"] = ext.activation_certificate_id
    return out


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
            # RCNS-3 contract scoping: a synthesis_id carries the
            # originating Agent-ID. A different agent presenting the
            # id is refused with 464 ``contract-not-yours``. When the
            # contract has no originator stamped (legacy /
            # pre-RCNS-3 explicit PROPOSE), this check is a no-op —
            # unscoped contracts stay reachable by any caller.
            from core import status as _status_cs
            originator = runtime.originating_agent_id(syn_id)
            if (
                originator
                and originator != agent_doc.agent_id
            ):
                return _status_cs.rcns_no_contract(
                    reason=_status_cs.RCNS_REASON_CONTRACT_NOT_YOURS,
                    explanation=(
                        "this synthesis_id was negotiated for a different "
                        "Agent-ID and is not transferable"
                    ),
                    details={
                        "synthesis_id": syn_id,
                        "presenter_agent_id": agent_doc.agent_id,
                    },
                )
            # Stamp RCNS attribution extras onto the response so
            # _finalize_response includes them in the Attribution-
            # Record. Chain inspectors group invocations by
            # contract_hash and trace negotiation_origin.
            response = runtime.execute(syn_id, request, server_state, agent_doc)
            extras = {
                "synthesis_id": syn_id,
                "contract_hash": runtime.contract_hash(syn_id) or "",
                "negotiation_origin": runtime.negotiation_origin(syn_id),
            }
            existing = getattr(response, "_attribution_extra", None)
            if isinstance(existing, dict):
                existing.update(extras)
            else:
                response._attribution_extra = extras  # type: ignore[attr-defined]
            return response
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
            # RCNS-3 dispatcher gate. When the four locks are open
            # (server config + Allow-RCNS header + rcns:negotiate
            # scope + trust_tier), the gate attempts synthesis and
            # returns 461 (confirm-first) or executes inline
            # (optimistic). Falls through to 404 when any lock is
            # closed.
            from server.rcns_gate import try_rcns
            rcns_response = try_rcns(
                request, server_state, agent_doc,
                method=method_name, path=request_path,
            )
            if rcns_response is not None:
                return rcns_response
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
