"""
Resolve :class:`HandlerBinding` declarations to callables.

Phase 2 supports one of the three binding kinds:

  * ``registered_function`` — ``reference`` is a Python dotted path
    (``staybeta.handlers.book_room``). The resolver imports the
    parent module and pulls the named attribute, verifying it's a
    callable. The handler is expected to follow the public
    :class:`agtp.handlers` signature: a single
    :class:`~agtp.handlers.EndpointContext` argument, returning
    :class:`~agtp.handlers.EndpointResponse` or
    :class:`~agtp.handlers.EndpointError`.

The other two kinds are stubbed:

  * ``composition`` — Phase 3 wires the synthesis runtime as the
    handler. Phase 2 raises :class:`NotImplementedError` so the
    operator gets a clear pointer when they reach for it early.
  * ``external_service`` — Phase 4 implements the upstream HTTP
    proxy. Same NotImplementedError treatment.

Resolution failures (missing module, missing attribute,
non-callable target) raise :class:`InvalidHandlerError` — the
caller (typically the boot sequence) decides whether to skip the
endpoint or abort startup.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.endpoint import EndpointSpec, HandlerBinding


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class InvalidHandlerError(Exception):
    """
    Raised when a :class:`HandlerBinding` cannot be resolved to a
    callable.

    Stable detail tags (set on ``self.detail``):

      * ``import-failed``      the module the reference points at
                               cannot be imported.
      * ``attribute-missing``  the module imports but does not have
                               the named attribute.
      * ``not-callable``       the attribute exists but is not callable.
      * ``empty-reference``    the binding's reference is empty / blank.
      * ``bad-binding-type``   the binding's type isn't one of the
                               recognized kinds.
      * ``recipe-not-found``   composition reference names a recipe
                               not loaded into the synthesis runtime.
      * ``recipe-step-method-missing`` recipe references a step method
                               not registered on this server.
      * ``runtime-not-configured`` server has no synthesis runtime
                               attached (composition is unavailable).
      * ``composition-needs-spec`` resolve_handler called for a
                               composition binding without the
                               originating EndpointSpec.
    """

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


# ---------------------------------------------------------------------------
# registered_function — fully implemented in Phase 2.
# ---------------------------------------------------------------------------


def resolve_registered_function(reference: str) -> Callable[..., Any]:
    """
    Legacy in-daemon resolution of a ``registered_function`` binding.

    .. note::

       The gateway path (:func:`resolve_via_gateway`, configured via
       ``--gateway-socket``) is the recommended way to host Python
       handlers as of M3 step (c). The in-daemon import path resolved
       here remains the **default** when no gateway socket is
       configured, and continues to be supported for unit tests,
       legacy deployments, and the embedded methods path. New
       deployments should prefer the gateway path so handlers live in
       a separate process — see
       ``docs/architecture/server-modules.md`` and
       ``docs/architecture/gateway-protocol-v1.md``.

    Splits at the last ``.`` so the module path is the prefix and
    the function name is the trailing token. ``importlib.import_module``
    handles dotted module paths so ``a.b.c.fn`` imports ``a.b.c`` and
    pulls ``fn`` from it.
    """
    if not reference or not isinstance(reference, str):
        raise InvalidHandlerError(
            "handler reference is empty", detail="empty-reference",
        )
    if "." not in reference:
        raise InvalidHandlerError(
            f"handler reference {reference!r} must include a module "
            f"path (e.g. 'package.module.function')",
            detail="attribute-missing",
        )
    module_path, _, attr = reference.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise InvalidHandlerError(
            f"cannot import module {module_path!r} for handler "
            f"reference {reference!r}: {exc}",
            detail="import-failed",
        ) from exc
    try:
        target = getattr(module, attr)
    except AttributeError as exc:
        raise InvalidHandlerError(
            f"module {module_path!r} has no attribute {attr!r} "
            f"(handler reference {reference!r})",
            detail="attribute-missing",
        ) from exc
    if not callable(target):
        raise InvalidHandlerError(
            f"handler reference {reference!r} resolved to a "
            f"non-callable object ({type(target).__name__})",
            detail="not-callable",
        )
    return target


# ---------------------------------------------------------------------------
# composition — Phase 3.
# ---------------------------------------------------------------------------


def _clone_recipe_steps(recipe: Any) -> list:
    """Defensive copy of a recipe's step list. Recipes are templates;
    each composition handler should hold its own steps so a shared
    recipe surviving multiple endpoints doesn't risk cross-mutation.
    """
    from server.synthesis.plan import CompositionStep, ParameterSource
    return [
        CompositionStep(
            method_name=s.method_name,
            parameter_source={
                k: ParameterSource(kind=v.kind, value=v.value)
                for k, v in s.parameter_source.items()
            },
            capture_output_as=s.capture_output_as,
        )
        for s in recipe.steps
    ]


def resolve_composition(
    reference: str,
    server_state: Optional[Any] = None,
    spec: Optional[EndpointSpec] = None,
) -> Callable[..., Any]:
    """
    Resolve a composition-bound endpoint to a callable.

    ``reference`` is the name of a recipe registered in the
    :class:`server.synthesis.SynthesisRuntime` attached to
    ``server_state``. The recipe's steps walk through the same
    dispatcher every external invocation goes through, so authority
    is preserved end-to-end.

    Resolution-time checks (all surface as
    :class:`InvalidHandlerError`, not lazily at first traffic):

      * ``server_state.synthesis_runtime`` is configured.
      * ``reference`` matches a recipe loaded into the runtime.
      * Every step method the recipe references is registered on
        this server (i.e., present in ``server.methods.REGISTRY``).
      * ``spec`` (the registered :class:`EndpointSpec`) was supplied
        — composition handlers need it to construct the
        :class:`SynthesisPlan`'s ``proposed_method`` field.
      * The endpoint's ``errors`` list declares
        ``"composition_failed"`` so the handler can surface
        underlying step failures cleanly.

    Returns a closure handler matching the public-API signature:
    ``(EndpointContext) -> EndpointResponse | EndpointError``.
    """
    if not reference or not isinstance(reference, str):
        raise InvalidHandlerError(
            "composition handler reference is empty",
            detail="empty-reference",
        )
    if spec is None:
        raise InvalidHandlerError(
            f"resolve_composition for recipe {reference!r} requires "
            f"the originating EndpointSpec — composition handlers "
            f"build a SynthesisPlan whose proposed_method is the "
            f"endpoint's spec",
            detail="composition-needs-spec",
        )

    runtime = getattr(server_state, "synthesis_runtime", None)
    if runtime is None:
        raise InvalidHandlerError(
            f"composition handler references recipe {reference!r}, "
            f"but no synthesis runtime is attached to the server",
            detail="runtime-not-configured",
        )

    recipe = runtime.get_recipe(reference)
    if recipe is None:
        available = runtime.list_recipes()
        raise InvalidHandlerError(
            f"composition handler references recipe {reference!r}, "
            f"but no such recipe is registered. Available recipes: "
            f"{', '.join(available) or '(none)'}",
            detail="recipe-not-found",
        )

    # Every step method must be registered on this server. Without
    # this check the failure would only surface at the first
    # invocation when the dispatcher returns 405 for the missing
    # step method. Catching it at startup gives the operator a clean
    # error.
    from server.methods import REGISTRY as _METHOD_REGISTRY
    missing_step_methods = [
        s.method_name for s in recipe.steps
        if s.method_name not in _METHOD_REGISTRY
    ]
    if missing_step_methods:
        raise InvalidHandlerError(
            f"composition recipe {reference!r} references step "
            f"method(s) not registered on this server: "
            f"{', '.join(missing_step_methods)}. "
            f"Either register a handler for each, or remove the "
            f"step from the recipe.",
            detail="recipe-step-method-missing",
        )

    # The endpoint must declare ``composition_failed`` in its errors
    # list. The composition handler returns that EndpointError code
    # whenever a recipe step fails; if it isn't declared the
    # dispatcher would refuse the handler's output as an undeclared
    # error code (a confusing 500 at first traffic).
    if "composition_failed" not in (spec.errors or []):
        raise InvalidHandlerError(
            f"composition endpoint ({spec.name}, {spec.path}) must "
            f"declare 'composition_failed' in its errors list so the "
            f"handler can surface step-failure shapes",
            detail="composition-missing-error-code",
        )

    # Build the SynthesisPlan once and capture in the closure. Recipe
    # templates don't change at runtime, and the plan is read-only
    # during execution (the runtime threads parameters through
    # without mutating the plan).
    from server.synthesis.plan import SynthesisPlan
    plan = SynthesisPlan(
        proposed_method=spec,
        steps=_clone_recipe_steps(recipe),
        output_aggregation=recipe.output_aggregation,
        description=recipe.description,
        policy_name="recipes",
    )

    # The closure imports happen at handler-call time (lazy) so the
    # resolver itself stays cheap and import-cycle-free.
    def composition_handler(ctx: Any) -> Any:
        from agtp.handlers import EndpointError, EndpointResponse
        from core import wire as wire_mod

        # Look up the agent_doc from the server-state (the dispatcher
        # passes the AgentRegistry in as ctx.server_state). The
        # runtime's per-step dispatcher needs the full AgentDocument
        # for capability checks.
        agent_doc = None
        if ctx.server_state is not None and hasattr(ctx.server_state, "lookup"):
            agent_doc = ctx.server_state.lookup(ctx.agent_id)
        if agent_doc is None:
            return EndpointError(
                code="composition_failed",
                message=(
                    f"composition handler could not resolve agent "
                    f"{ctx.agent_id!r} on this server"
                ),
                details={"recipe": reference},
            )

        # Synthesize an AGTPRequest carrying the validated input. The
        # runtime's _build_step_request derives each step's request
        # from this (preserving auth-relevant headers; stripping
        # Synthesis-Id so steps don't recurse).
        body_bytes = json.dumps(ctx.input).encode("utf-8") if ctx.input else b""
        # Reconstruct headers in canonical (case-preserving) form;
        # ctx.headers carries lowercased keys from the dispatcher.
        canonical_headers = {
            "Accept": "application/json",
        }
        for k, v in (ctx.headers or {}).items():
            if k == "agent-id" or k == "target-agent":
                # §10: emit the canonical Agent-ID name regardless of
                # which header the inbound request used.
                canonical_headers["Agent-ID"] = v
            elif k == "host":
                canonical_headers["Host"] = v
        if body_bytes:
            canonical_headers["Content-Type"] = "application/json"

        runtime_request = wire_mod.AGTPRequest(
            method=ctx.method,
            headers=canonical_headers,
            body_bytes=body_bytes,
            path=ctx.path,
        )

        runtime_response = runtime.execute_plan(
            plan, runtime_request, ctx.server_state, agent_doc,
        )

        # Translate the runtime's response into the public-API shape.
        # The runtime's success body is
        #     {"method", "synthesis_id", "outcome", "output", "steps"}
        # and the failure body's status code carries the underlying
        # step's status, with body
        #     {"method", "synthesis_id", "outcome": "error",
        #      "error": {"code": "synthesis-step-failed", ...}, "steps"}
        try:
            payload = json.loads(runtime_response.body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return EndpointError(
                code="composition_failed",
                message="composition runtime returned an unparseable body",
                details={"status": runtime_response.status_code},
            )

        if (
            runtime_response.status_code == 200
            and payload.get("outcome") == "ok"
        ):
            output = payload.get("output")
            if isinstance(output, dict):
                body = output
            else:
                # Aggregations of "list" / scalar outputs ride under
                # the canonical ``result`` key so the endpoint's
                # output schema can declare ``result`` and have the
                # validator accept the response.
                body = {"result": output}
            return EndpointResponse(body=body)

        # Step failure path. Surface the underlying step's status,
        # method, and any captured outputs for the agent.
        err_block = payload.get("error", {}) if isinstance(payload, dict) else {}
        return EndpointError(
            code="composition_failed",
            message=(
                f"composition step {err_block.get('failed_step', '?')} "
                f"({err_block.get('method', '?')}) failed: status "
                f"{err_block.get('underlying_status', runtime_response.status_code)}"
            ),
            details={
                "recipe": reference,
                "failed_step": err_block.get("failed_step"),
                "step_method": err_block.get("method"),
                "underlying_status": err_block.get("underlying_status"),
                "underlying": err_block.get("underlying"),
                "captured_outputs": err_block.get("captured_outputs"),
            },
        )

    # Tag the closure so introspection / tests can identify it.
    composition_handler.__agtp_handler_kind__ = "composition"
    composition_handler.__agtp_recipe_name__ = reference
    return composition_handler


# ---------------------------------------------------------------------------
# external_service — Phase 4. Wraps an existing HTTPS API as an AGTP
# endpoint. The handler issues an HTTP request to the configured URL,
# translates request/response payloads via the binding's
# ``input_map`` / ``output_map``, and surfaces upstream errors as
# structured :class:`agtp.handlers.EndpointError`s.
#
# The implementation deliberately uses stdlib ``urllib.request`` so
# the server doesn't take a hard dependency on ``requests``. Tests
# patch the small ``_do_external_request`` helper rather than the
# urllib internals.
# ---------------------------------------------------------------------------


#: Pattern for shell-style environment-variable references in header
#: values (``${API_KEY}`` etc.). Matches ASCII identifier characters
#: only — header values are otherwise passed through unchanged.
_ENV_VAR_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def translate_input(
    agtp_body: Dict[str, Any],
    input_map: Dict[str, str],
) -> Dict[str, Any]:
    """
    Rename top-level fields of ``agtp_body`` per ``input_map``.

    ``input_map`` is ``AGTP-name → HTTP-name``. Fields not in the
    map pass through with their original AGTP names so callers can
    mix renamed and pass-through fields without listing every one.
    """
    if not isinstance(agtp_body, dict):
        return {}
    if not input_map:
        return dict(agtp_body)
    out: Dict[str, Any] = {}
    for k, v in agtp_body.items():
        out[input_map.get(k, k)] = v
    return out


def translate_output(
    http_body: Dict[str, Any],
    output_map: Dict[str, str],
) -> Dict[str, Any]:
    """
    Rename top-level fields of ``http_body`` so the resulting dict
    is in AGTP-shape.

    ``output_map`` is ``AGTP-name → HTTP-name`` (matching the TOML
    declaration's directionality). The reverse rename happens here:
    each HTTP-side key is looked up in the inverted map and
    rewritten to its AGTP name. HTTP fields without a mapping pass
    through unchanged so handlers can return forward-compat shapes.
    """
    if not isinstance(http_body, dict):
        # Non-object responses ride under a canonical ``result`` key
        # so the endpoint's output schema can declare ``result`` and
        # have the validator accept the response.
        return {"result": http_body}
    if not output_map:
        return dict(http_body)
    inverted = {v: k for k, v in output_map.items()}
    out: Dict[str, Any] = {}
    for k, v in http_body.items():
        out[inverted.get(k, k)] = v
    return out


def resolve_headers(
    headers: Dict[str, str],
    *,
    environ: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """
    Substitute ``${VAR}`` references in header values against the
    process environment (or the supplied ``environ`` dict).

    Returns ``(resolved_headers, missing_vars)``. Missing variables
    are replaced with the empty string in the resolved header value
    AND added to ``missing_vars`` so callers can warn / abort
    according to their preferred policy. The default behavior here
    is permissive (the server logs a warning at startup); strict
    behavior is left to the boot sequence.
    """
    env = environ if environ is not None else os.environ
    resolved: Dict[str, str] = {}
    missing: List[str] = []

    def _sub(match: re.Match) -> str:
        var = match.group(1)
        if var in env:
            return env[var]
        if var not in missing:
            missing.append(var)
        return ""

    for name, value in (headers or {}).items():
        resolved[name] = _ENV_VAR_REF_PATTERN.sub(_sub, str(value))
    return resolved, missing


# ---------------------------------------------------------------------------
# HTTP transport.
# ---------------------------------------------------------------------------


class _UpstreamRequestOutcome:
    """Internal result type from :func:`_do_external_request`. Holds
    either the parsed response (status + headers + body) or a
    structured failure tag. Tests patch ``_do_external_request`` to
    return a synthetic outcome rather than mocking urllib internals."""

    __slots__ = ("status", "headers", "body_bytes", "failure_kind", "detail")

    def __init__(
        self,
        *,
        status: int = 0,
        headers: Optional[Dict[str, str]] = None,
        body_bytes: bytes = b"",
        failure_kind: Optional[str] = None,
        detail: str = "",
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.body_bytes = body_bytes
        self.failure_kind = failure_kind
        self.detail = detail


def _do_external_request(
    *,
    method: str,
    url: str,
    body: bytes,
    headers: Dict[str, str],
    timeout: float,
) -> _UpstreamRequestOutcome:
    """
    Issue a single HTTP request and surface the outcome as a
    :class:`_UpstreamRequestOutcome`.

    Failure kinds (mirror the public-API error codes):

      * ``upstream_timeout``           — connection or read timeout.
      * ``upstream_connection_error``  — DNS failure, connection
                                         refused, TLS handshake
                                         failure, etc.
      * (none)                         — request completed (the
                                         caller decides whether the
                                         status code counts as
                                         success or failure).

    HTTP error responses (4xx / 5xx) are returned as outcomes with
    a real ``status`` and ``body_bytes``; the error-mapping logic
    runs in the handler closure. Tests patch THIS function to
    inject synthetic outcomes — the handler closure is exercised
    end-to-end without touching the network.
    """
    req = urllib.request.Request(url=url, data=body or None, method=method)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _UpstreamRequestOutcome(
                status=getattr(resp, "status", 200) or 200,
                headers=dict(resp.headers.items() if resp.headers else []),
                body_bytes=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        # 4xx / 5xx responses raise HTTPError but still have a body
        # we can read. Treat as a non-failure outcome carrying the
        # error status so the handler's error_map can run.
        try:
            body_bytes = exc.read()
        except Exception:  # noqa: BLE001
            body_bytes = b""
        return _UpstreamRequestOutcome(
            status=exc.code,
            headers=dict(exc.headers.items() if exc.headers else []),
            body_bytes=body_bytes,
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        # urllib raises URLError(reason=socket.timeout(...)) on
        # connection / read timeouts; flatten both shapes here.
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return _UpstreamRequestOutcome(
                failure_kind="upstream_timeout", detail=str(reason),
            )
        return _UpstreamRequestOutcome(
            failure_kind="upstream_connection_error", detail=str(reason),
        )
    except ssl.SSLError as exc:
        return _UpstreamRequestOutcome(
            failure_kind="upstream_connection_error", detail=str(exc),
        )


# ---------------------------------------------------------------------------
# resolve_external_service.
# ---------------------------------------------------------------------------


def resolve_external_service(
    binding: HandlerBinding,
    *,
    spec: Optional[EndpointSpec] = None,
    environ: Optional[Dict[str, str]] = None,
) -> Callable[..., Any]:
    """
    Resolve an ``external_service`` binding to a callable.

    Resolution-time checks (registration is the right place to fail
    so first-traffic doesn't surface mysterious 5xxs):

      * ``binding.method`` is set to a recognized HTTP verb.
      * ``binding.url`` is an HTTPS URL.
      * ``binding.timeout_seconds`` is positive.
      * Header env-var substitution is computed once at startup;
        missing variables log a warning to stderr but do not refuse
        registration (operators sometimes inject API keys at
        deployment time, after binary boot).

    The returned closure issues one HTTP request per invocation,
    translates the response, and surfaces upstream failures as
    :class:`~agtp.handlers.EndpointError` instances with one of:

      * ``upstream_timeout``
      * ``upstream_connection_error``
      * ``upstream_malformed_response``
      * ``upstream_authentication_failed`` (HTTP 401 / 403)
      * ``upstream_error`` (status not in ``error_map``)
      * the mapped code from ``binding.error_map``

    Resolution does NOT propagate the agent's identity to the
    upstream by default. Authentication-passthrough is reserved for
    later phases — Phase 4's threat model is "I want to wrap an
    existing API"; passing the agent through changes the upstream's
    auth surface and warrants its own design pass.
    """
    if not binding or binding.type != "external_service":
        raise InvalidHandlerError(
            f"resolve_external_service called for binding type "
            f"{getattr(binding, 'type', None)!r}",
            detail="bad-binding-type",
        )
    if not binding.url:
        raise InvalidHandlerError(
            "external_service binding has no url",
            detail="empty-reference",
        )
    if not binding.method:
        raise InvalidHandlerError(
            "external_service binding has no HTTP method",
            detail="external-service-missing-method",
        )
    if not binding.url.startswith("https://"):
        raise InvalidHandlerError(
            f"external_service url must be an HTTPS URL "
            f"(got {binding.url!r}); plaintext is refused",
            detail="external-service-bad-scheme",
        )
    if binding.timeout_seconds is None or binding.timeout_seconds <= 0:
        raise InvalidHandlerError(
            "external_service timeout_seconds must be positive",
            detail="external-service-bad-timeout",
        )

    upstream_url = binding.url
    upstream_method = binding.method.upper()
    timeout = float(binding.timeout_seconds)
    input_map = dict(binding.input_transform or {})
    output_map = dict(binding.output_transform or {})
    error_map = {str(k): str(v) for k, v in (binding.error_map or {}).items()}

    # Resolve headers once at startup. Missing env vars warn but
    # don't refuse registration — see docstring.
    resolved_headers, missing_vars = resolve_headers(
        binding.headers or {}, environ=environ,
    )
    if missing_vars:
        print(
            f"[server] external_service binding {upstream_url!r}: "
            f"missing environment variables: "
            f"{', '.join(sorted(missing_vars))} — "
            f"corresponding header values are empty",
            file=sys.stderr,
        )

    def external_handler(ctx: Any) -> Any:
        from agtp.handlers import EndpointError, EndpointResponse

        # Translate input shape and serialize as JSON for the upstream.
        http_input = translate_input(ctx.input or {}, input_map)
        body_bytes = (
            json.dumps(http_input).encode("utf-8") if http_input else b""
        )
        request_headers = dict(resolved_headers)
        # Default Content-Type for bodies that the operator didn't
        # set explicitly — most JSON-shaped APIs expect it.
        if body_bytes and not any(
            h.lower() == "content-type" for h in request_headers
        ):
            request_headers["Content-Type"] = "application/json"
        # Agent identity is intentionally not propagated by default.
        # (See docstring + docs/external-service-handlers.md security
        # section.)

        outcome = _do_external_request(
            method=upstream_method,
            url=upstream_url,
            body=body_bytes,
            headers=request_headers,
            timeout=timeout,
        )

        # Transport-level failures: surface as the matching public
        # error code without consulting error_map.
        if outcome.failure_kind:
            return EndpointError(
                code=outcome.failure_kind,
                message=(
                    f"external service request failed: "
                    f"{outcome.detail or outcome.failure_kind}"
                ),
                details={
                    "upstream_url": upstream_url,
                    "upstream_method": upstream_method,
                },
            )

        # Parse the response body. Empty bodies become the empty
        # dict (some APIs return 204 / 200 + no body for write
        # operations); non-JSON bodies are an upstream-malformed
        # signal when the status is otherwise success-ish.
        parsed_body: Any = None
        if outcome.body_bytes:
            try:
                parsed_body = json.loads(outcome.body_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return EndpointError(
                    code="upstream_malformed_response",
                    message=(
                        "external service returned a body that is "
                        "not valid JSON"
                    ),
                    details={
                        "upstream_status": outcome.status,
                        "upstream_url": upstream_url,
                    },
                )

        # Error responses. ``error_map`` wins for codes the operator
        # configured; 401 / 403 ride a dedicated public code so
        # auth failures are visible without per-API mapping;
        # everything else falls through to ``upstream_error``.
        if outcome.status >= 400:
            mapped = error_map.get(str(outcome.status))
            if mapped is not None:
                code = mapped
            elif outcome.status in (401, 403):
                code = "upstream_authentication_failed"
            else:
                code = "upstream_error"
            return EndpointError(
                code=code,
                message=f"upstream returned HTTP {outcome.status}",
                details={
                    "upstream_status": outcome.status,
                    "upstream_url": upstream_url,
                    "upstream_body": parsed_body,
                },
            )

        agtp_body = translate_output(parsed_body or {}, output_map)
        # Carry the upstream status forward when it's a non-200
        # success (201 Created / 202 Accepted / etc.). Default 200.
        return EndpointResponse(body=agtp_body, status=outcome.status or 200)

    external_handler.__agtp_handler_kind__ = "external_service"
    external_handler.__agtp_upstream_url__ = upstream_url
    external_handler.__agtp_upstream_method__ = upstream_method
    return external_handler


# ---------------------------------------------------------------------------
# Top-level dispatch.
# ---------------------------------------------------------------------------


def resolve_via_gateway(
    binding: HandlerBinding,
    *,
    spec: EndpointSpec,
    gateway_server: Any,
) -> Callable[..., Any]:
    """
    Resolve a ``registered_function`` binding to a gateway-dispatch closure.

    When the daemon is running with ``--gateway-socket`` set,
    ``registered_function`` bindings are NOT imported in-daemon. The
    function's dotted path is sent to a connected runtime module
    (``mod_python``, ``mod_php``, ...) inside the ``register`` frame;
    that module resolves the reference against its local import path.

    The returned closure has the same signature as every other
    resolved handler — ``(EndpointContext) -> EndpointResponse |
    EndpointError`` — so :func:`server.methods._serve_endpoint` calls
    it without knowing the dispatch is happening over a socket. On
    gateway failure (no module connected, mid-request disconnect),
    the closure returns an :class:`EndpointError` with code
    ``gateway_unavailable``; ``_serve_endpoint`` translates that into
    a 503 wire response.

    Composition and external_service bindings continue to resolve
    in-daemon even when gateway mode is on — composition is a daemon
    concern (it walks the synthesis runtime), and external_service is
    the daemon's reverse-proxy. The gateway is only for bridging to
    user code.
    """
    if binding.type != "registered_function":
        raise InvalidHandlerError(
            f"resolve_via_gateway only handles registered_function bindings; "
            f"got {binding.type!r}",
            detail="bad-binding-type",
        )
    if not binding.function:
        raise InvalidHandlerError(
            "registered_function binding has no function reference",
            detail="empty-reference",
        )
    if gateway_server is None:
        raise InvalidHandlerError(
            "resolve_via_gateway requires a GatewayServer",
            detail="runtime-not-configured",
        )

    def gateway_handler(ctx: Any) -> Any:
        return gateway_server.dispatch(ctx)

    gateway_handler.__agtp_handler_kind__ = "gateway"
    gateway_handler.__agtp_handler_reference__ = binding.function
    gateway_handler.__agtp_endpoint__ = (spec.method, spec.path or "/")
    return gateway_handler


def resolve_handler(
    binding: HandlerBinding,
    server_state: Optional[Any] = None,
    *,
    spec: Optional[EndpointSpec] = None,
) -> Callable[..., Any]:
    """
    Dispatch to the right resolver based on ``binding.type``.

    ``spec`` is the originating :class:`EndpointSpec`. Required for
    ``composition`` bindings (the handler builds a SynthesisPlan
    whose ``proposed_method`` is the spec); ignored by
    ``registered_function``. The boot sequence in
    ``server.main.AgentRegistry.configure_endpoints`` passes it
    through automatically.

    When ``server_state`` carries a non-``None`` ``gateway_server``
    attribute, ``registered_function`` bindings resolve to a
    gateway-dispatch closure instead of an in-daemon import. The
    closure proxies to a connected runtime module via the gateway
    socket. Composition and external_service bindings continue to
    resolve in-daemon regardless of gateway mode.

    Returns the callable; raises :class:`InvalidHandlerError` for
    registered_function and composition resolution failures, or
    :class:`NotImplementedError` for the still-stubbed
    ``external_service`` kind. Callers typically catch both and
    skip the offending endpoint with a logged warning.
    """
    gateway_server = getattr(server_state, "gateway_server", None)

    if binding.type == "registered_function":
        if gateway_server is not None:
            if spec is None:
                raise InvalidHandlerError(
                    "gateway-mode resolution of registered_function binding "
                    "requires the originating EndpointSpec",
                    detail="composition-needs-spec",
                )
            return resolve_via_gateway(
                binding, spec=spec, gateway_server=gateway_server,
            )
        return resolve_registered_function(binding.function or "")
    if binding.type == "composition":
        return resolve_composition(
            binding.recipe or "", server_state, spec=spec,
        )
    if binding.type == "external_service":
        return resolve_external_service(binding, spec=spec)
    raise InvalidHandlerError(
        f"handler binding type {binding.type!r} is not recognized; "
        f"expected one of registered_function / composition / "
        f"external_service",
        detail="bad-binding-type",
    )


__all__ = [
    "InvalidHandlerError",
    "resolve_composition",
    "resolve_external_service",
    "resolve_handler",
    "resolve_registered_function",
    "resolve_via_gateway",
]
