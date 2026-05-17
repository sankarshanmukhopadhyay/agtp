"""
Proxy handler factory: returns a callable that forwards an AGTP
request to an upstream daemon and returns the response.

The factory uses the existing AGTP client primitives in
``client.core_client`` to open outbound connections. Connection
pooling and resilience features (retry, circuit breaker) are
deferred to a future revision; v1 makes one outbound call per
inbound request and surfaces upstream failures as ``EndpointError``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import EndpointSpec, HandlerBinding


def resolve_proxy(
    binding: HandlerBinding,
    *,
    spec: EndpointSpec,
) -> Callable[..., Any]:
    """Build a closure that proxies invocations to ``binding.url``.

    The closure has the same shape as every other resolved handler:
    ``(EndpointContext) -> EndpointResponse | EndpointError``. The
    daemon's dispatch pipeline calls it the same way it'd call a
    registered_function — the proxy is invisible to the rest of the
    system.
    """
    if binding.type != "proxy":
        raise ValueError(
            f"resolve_proxy only handles proxy bindings; got {binding.type!r}"
        )
    upstream = binding.url or ""
    if not upstream:
        raise ValueError("proxy binding has no upstream url")
    if not upstream.startswith(("agtp://", "agtps://")):
        raise ValueError(
            f"proxy binding url must be agtp:// or agtps://; got {upstream!r}"
        )

    endpoint_method = spec.method
    endpoint_path = spec.path or "/"

    def proxy_handler(ctx: EndpointContext) -> Any:
        # Lazy import: the client may not be available in every
        # environment (e.g., test fixtures stripped to the daemon).
        try:
            from client.core_client import fetch as _fetch
        except ImportError as exc:
            return EndpointError(
                code="proxy_unavailable",
                message=f"AGTP client unavailable on this daemon: {exc}",
                details={"upstream": upstream},
            )

        # Build the outbound headers from the inbound context. The
        # upstream sees the original Agent-ID so it can authenticate
        # the calling agent; this is correct for federation, where
        # the proxy is transparent. Sites that need different
        # behavior (e.g., the proxy acts as its own agent) should
        # author a registered_function handler instead.
        headers = {
            "Agent-ID": ctx.agent_id,
        }
        if ctx.principal_id:
            headers["Principal-ID"] = ctx.principal_id
        if ctx.session_id:
            headers["Session-ID"] = ctx.session_id
        if ctx.task_id:
            headers["Task-ID"] = ctx.task_id
        if ctx.authority_scope:
            headers["Authority-Scope"] = ",".join(ctx.authority_scope)

        body_bytes = json.dumps(ctx.input or {}).encode("utf-8")

        try:
            result = _fetch(
                upstream,
                method=endpoint_method,
                path=endpoint_path,
                headers=headers,
                body=body_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            return EndpointError(
                code="proxy_upstream_unreachable",
                message=f"upstream call failed: {exc}",
                details={"upstream": upstream},
            )

        status = getattr(result, "status_code", None) or getattr(result, "status", 500)
        upstream_body_bytes = (
            getattr(result, "body_bytes", None)
            or getattr(result, "body", b"")
            or b""
        )
        if isinstance(upstream_body_bytes, str):
            upstream_body_bytes = upstream_body_bytes.encode("utf-8")

        try:
            payload = json.loads(upstream_body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return EndpointError(
                code="proxy_upstream_malformed",
                message=f"upstream returned non-JSON body (status {status})",
                details={"upstream": upstream, "status": status},
            )

        if status >= 400:
            err = payload.get("error") if isinstance(payload, dict) else None
            return EndpointError(
                code="proxy_upstream_error",
                message=(
                    err.get("message") if isinstance(err, dict) and "message" in err
                    else f"upstream returned status {status}"
                ),
                details={
                    "upstream": upstream,
                    "status": status,
                    "upstream_body": payload,
                },
            )

        body = payload if isinstance(payload, dict) else {"result": payload}
        return EndpointResponse(body=body, status=status or 200)

    proxy_handler.__agtp_handler_kind__ = "proxy"
    proxy_handler.__agtp_upstream__ = upstream
    proxy_handler.__agtp_endpoint__ = (endpoint_method, endpoint_path)
    return proxy_handler
