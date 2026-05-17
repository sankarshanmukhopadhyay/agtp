"""
Tests for the mod_proxy operational module.

Exercises the resolver, the install() boot path, and the handler
closure's outbound-call logic with a fake client.fetch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock
from mod_proxy import install
from mod_proxy.handler import resolve_proxy


# ---------------------------------------------------------------------------
# resolve_proxy.
# ---------------------------------------------------------------------------


def _spec(method: str = "BOOK", path: str = "/room") -> EndpointSpec:
    return EndpointSpec(
        name=method,
        path=path,
        description="Proxied.",
        required_params=[ParamSpec(name="x", type="string", description="x")],
        semantic=SemanticBlock(
            intent="Proxy.",
            actor="agent",
            outcome="Forwarded.",
            capability="transaction",
            confidence=0.9,
            impact="reversible",
            is_idempotent=False,
        ),
    )


def _binding(url: str = "agtp://upstream.example.com") -> HandlerBinding:
    return HandlerBinding(type="proxy", url=url)


def _ctx(input_data=None) -> EndpointContext:
    return EndpointContext(
        input=input_data or {"x": "v"},
        agent_id="agent-abc",
        principal_id="chris@example.com",
        authority_scope=["scope:read"],
        request_id="req-1",
        method="BOOK",
        path="/room",
    )


def test_resolve_proxy_rejects_non_proxy_binding() -> None:
    spec = _spec()
    binding = HandlerBinding(type="registered_function", function="x.y")
    with pytest.raises(ValueError, match="only handles proxy"):
        resolve_proxy(binding, spec=spec)


def test_resolve_proxy_rejects_empty_url() -> None:
    spec = _spec()
    binding = HandlerBinding(type="proxy", url="")
    with pytest.raises(ValueError, match="no upstream url"):
        resolve_proxy(binding, spec=spec)


def test_resolve_proxy_rejects_non_agtp_url() -> None:
    spec = _spec()
    binding = HandlerBinding(type="proxy", url="https://example.com")
    with pytest.raises(ValueError, match="must be agtp"):
        resolve_proxy(binding, spec=spec)


def test_proxy_handler_forwards_request_and_returns_response() -> None:
    handler = resolve_proxy(_binding(), spec=_spec())

    captured = {}

    def fake_fetch(uri, *, method, path, headers, body):
        captured["uri"] = uri
        captured["method"] = method
        captured["path"] = path
        captured["headers"] = dict(headers)
        captured["body"] = body
        return SimpleNamespace(
            status_code=200,
            body_bytes=json.dumps({"reservation_id": "res-1"}).encode("utf-8"),
        )

    with patch("client.core_client.fetch", fake_fetch):
        result = handler(_ctx())

    assert isinstance(result, EndpointResponse)
    assert result.body == {"reservation_id": "res-1"}
    assert result.status == 200

    # The outbound call preserved the right addressing.
    assert captured["uri"] == "agtp://upstream.example.com"
    assert captured["method"] == "BOOK"
    assert captured["path"] == "/room"
    assert captured["headers"]["Agent-ID"] == "agent-abc"
    assert captured["headers"]["Principal-ID"] == "chris@example.com"
    assert captured["headers"]["Authority-Scope"] == "scope:read"
    assert json.loads(captured["body"].decode("utf-8")) == {"x": "v"}


def test_proxy_handler_surfaces_unreachable() -> None:
    handler = resolve_proxy(_binding(), spec=_spec())
    def fake_fetch(*_args, **_kw):
        raise ConnectionError("no route to host")
    with patch("client.core_client.fetch", fake_fetch):
        result = handler(_ctx())
    assert isinstance(result, EndpointError)
    assert result.code == "proxy_upstream_unreachable"
    assert "no route to host" in result.message


def test_proxy_handler_surfaces_upstream_error_status() -> None:
    handler = resolve_proxy(_binding(), spec=_spec())

    def fake_fetch(*_args, **_kw):
        return SimpleNamespace(
            status_code=500,
            body_bytes=json.dumps({"error": {"message": "boom"}}).encode("utf-8"),
        )

    with patch("client.core_client.fetch", fake_fetch):
        result = handler(_ctx())
    assert isinstance(result, EndpointError)
    assert result.code == "proxy_upstream_error"
    assert result.details["status"] == 500


def test_proxy_handler_surfaces_malformed_upstream_body() -> None:
    handler = resolve_proxy(_binding(), spec=_spec())

    def fake_fetch(*_args, **_kw):
        return SimpleNamespace(status_code=200, body_bytes=b"not json")

    with patch("client.core_client.fetch", fake_fetch):
        result = handler(_ctx())
    assert isinstance(result, EndpointError)
    assert result.code == "proxy_upstream_malformed"


# ---------------------------------------------------------------------------
# install() boot path.
# ---------------------------------------------------------------------------


def test_install_patches_handler_resolution() -> None:
    """mod_proxy.install registers resolve_proxy onto
    server.handler_resolution. Idempotent."""
    import server.handler_resolution as _hr

    # Save and restore to keep test isolation clean.
    prev = getattr(_hr, "resolve_proxy", None)
    prev_marker = getattr(_hr, "_mod_proxy_installed", False)
    try:
        # Remove any previous install so we can verify the patch fires.
        if hasattr(_hr, "resolve_proxy"):
            delattr(_hr, "resolve_proxy")
        if hasattr(_hr, "_mod_proxy_installed"):
            delattr(_hr, "_mod_proxy_installed")

        class _FakeState:
            pass

        install(_FakeState())
        assert getattr(_hr, "_mod_proxy_installed", False) is True
        assert getattr(_hr, "resolve_proxy", None) is resolve_proxy
    finally:
        # Restore prior state so other tests aren't affected.
        if prev is None:
            if hasattr(_hr, "resolve_proxy"):
                delattr(_hr, "resolve_proxy")
        else:
            setattr(_hr, "resolve_proxy", prev)
        if not prev_marker:
            if hasattr(_hr, "_mod_proxy_installed"):
                delattr(_hr, "_mod_proxy_installed")
        else:
            setattr(_hr, "_mod_proxy_installed", prev_marker)


def test_resolve_handler_rejects_proxy_when_module_not_loaded() -> None:
    """Without mod_proxy installed, resolving a proxy binding fails
    with a clear error directing the operator to load the module."""
    from server.handler_resolution import InvalidHandlerError, resolve_handler

    import server.handler_resolution as _hr
    prev = getattr(_hr, "resolve_proxy", None)
    if hasattr(_hr, "resolve_proxy"):
        delattr(_hr, "resolve_proxy")
    try:
        with pytest.raises(InvalidHandlerError) as exc_info:
            resolve_handler(_binding(), spec=_spec())
        assert exc_info.value.detail == "mod-proxy-not-loaded"
    finally:
        if prev is not None:
            setattr(_hr, "resolve_proxy", prev)


def test_resolve_handler_routes_to_proxy_when_loaded() -> None:
    """When mod_proxy is installed, resolve_handler dispatches a
    proxy binding through it."""
    from server.handler_resolution import resolve_handler

    install(SimpleNamespace())  # patches handler_resolution

    try:
        handler = resolve_handler(_binding(), spec=_spec())
        assert handler is not None
        # The returned closure carries our identifying attribute.
        assert getattr(handler, "__agtp_handler_kind__", "") == "proxy"
        assert getattr(handler, "__agtp_upstream__", "") == "agtp://upstream.example.com"
    finally:
        import server.handler_resolution as _hr
        if hasattr(_hr, "resolve_proxy"):
            delattr(_hr, "resolve_proxy")
        if hasattr(_hr, "_mod_proxy_installed"):
            delattr(_hr, "_mod_proxy_installed")
