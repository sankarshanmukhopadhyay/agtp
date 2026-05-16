"""
Tests for the public ``agtp`` package: registry, decorator, testing
helpers, and the re-exports from ``agtp/__init__.py``.

These tests exercise the handler-author-facing surface in isolation
from the daemon. The daemon's dispatch path is not invoked.
"""

from __future__ import annotations

import pytest

from agtp import (
    EndpointContext,
    EndpointError,
    EndpointResponse,
    HandlerRegistry,
    endpoint,
    registry as global_registry,
)
from agtp.testing import assert_error, assert_ok, make_context


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_global_registry():
    """Reset the process-wide registry between tests."""
    global_registry.clear()
    yield
    global_registry.clear()


# ---------------------------------------------------------------------------
# Decorator and registry.
# ---------------------------------------------------------------------------


def test_endpoint_decorator_registers_handler() -> None:
    @endpoint(method="BOOK", path="/room")
    def book_room(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={"reservation_id": "res-1"})

    entry = global_registry.lookup("BOOK", "/room")
    assert entry is not None
    assert entry.method == "BOOK"
    assert entry.path == "/room"
    assert entry.handler is book_room


def test_endpoint_decorator_returns_original_function() -> None:
    @endpoint(method="QUERY", path="/")
    def my_handler(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={"ok": True})

    # The decorator must not wrap or replace the function.
    assert my_handler.__name__ == "my_handler"
    # It must remain callable directly.
    result = my_handler(make_context())
    assert isinstance(result, EndpointResponse)


def test_endpoint_decorator_normalizes_method_to_uppercase() -> None:
    @endpoint(method="book", path="/room")
    def book_room(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={})

    assert global_registry.lookup("BOOK", "/room") is not None
    assert global_registry.lookup("book", "/room") is not None  # also normalized


def test_endpoint_decorator_carries_errors_and_scopes() -> None:
    @endpoint(
        method="BOOK",
        path="/room",
        errors=["room_unavailable", "invalid_dates"],
        required_scopes=["booking:write"],
        description="Books a room.",
    )
    def book_room(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={})

    entry = global_registry.lookup("BOOK", "/room")
    assert entry is not None
    assert entry.errors == ["room_unavailable", "invalid_dates"]
    assert entry.required_scopes == ["booking:write"]
    assert entry.description == "Books a room."


def test_duplicate_registration_raises() -> None:
    @endpoint(method="BOOK", path="/room")
    def first(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={})

    with pytest.raises(RuntimeError, match="already registered"):
        @endpoint(method="BOOK", path="/room")
        def second(ctx: EndpointContext) -> EndpointResponse:
            return EndpointResponse(body={})


def test_isolated_registry_does_not_share_with_global() -> None:
    """Direct HandlerRegistry instantiation gives test isolation."""
    private = HandlerRegistry()

    def handler(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={})

    private.register(handler, method="QUERY", path="/test")

    assert private.lookup("QUERY", "/test") is not None
    assert global_registry.lookup("QUERY", "/test") is None


def test_registry_all_and_len() -> None:
    @endpoint(method="QUERY", path="/a")
    def a(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={})

    @endpoint(method="QUERY", path="/b")
    def b(ctx: EndpointContext) -> EndpointResponse:
        return EndpointResponse(body={})

    entries = global_registry.all()
    assert len(entries) == 2
    assert len(global_registry) == 2
    assert ("QUERY", "/a") in global_registry
    assert ("QUERY", "/b") in global_registry


# ---------------------------------------------------------------------------
# Testing helpers.
# ---------------------------------------------------------------------------


def test_make_context_defaults() -> None:
    ctx = make_context()
    assert ctx.method == "QUERY"
    assert ctx.path == "/"
    assert ctx.agent_id == "test-agent"
    assert ctx.input == {}
    assert ctx.authority_scope == []
    assert ctx.headers == {}


def test_make_context_overrides() -> None:
    ctx = make_context(
        input={"room_type": "double"},
        method="book",
        path="/room",
        agent_id="abc123",
        authority_scope=["booking:write"],
        task_id="task-42",
    )
    assert ctx.method == "BOOK"
    assert ctx.input == {"room_type": "double"}
    assert ctx.agent_id == "abc123"
    assert ctx.authority_scope == ["booking:write"]
    assert ctx.task_id == "task-42"


def test_assert_ok_returns_response() -> None:
    response = EndpointResponse(body={"ok": True})
    returned = assert_ok(response)
    assert returned is response


def test_assert_ok_raises_on_error() -> None:
    err = EndpointError(code="bad", message="oops")
    with pytest.raises(AssertionError, match="EndpointError"):
        assert_ok(err)


def test_assert_error_returns_error() -> None:
    err = EndpointError(code="room_unavailable", message="full")
    returned = assert_error(err)
    assert returned is err


def test_assert_error_checks_code_when_supplied() -> None:
    err = EndpointError(code="room_unavailable", message="full")
    assert_error(err, code="room_unavailable")

    with pytest.raises(AssertionError, match="expected EndpointError code"):
        assert_error(err, code="invalid_dates")


def test_assert_error_raises_on_response() -> None:
    response = EndpointResponse(body={"ok": True})
    with pytest.raises(AssertionError, match="EndpointResponse"):
        assert_error(response)


# ---------------------------------------------------------------------------
# End-to-end smoke: decorator + handler + testing helpers.
# ---------------------------------------------------------------------------


def test_handler_round_trip() -> None:
    """A handler declared with @endpoint can be looked up, invoked
    through the testing helpers, and returns the expected shape."""

    @endpoint(
        method="BOOK",
        path="/room",
        errors=["room_unavailable"],
    )
    def book_room(ctx: EndpointContext):
        if ctx.input.get("room_type") == "presidential_suite":
            return EndpointError(
                code="room_unavailable",
                message="suite not available",
                details={"requested": ctx.input["room_type"]},
            )
        return EndpointResponse(
            body={"reservation_id": "res-1", "agent": ctx.agent_id}
        )

    entry = global_registry.lookup("BOOK", "/room")
    assert entry is not None

    ok_response = assert_ok(
        entry.handler(make_context(input={"room_type": "double"}))
    )
    assert ok_response.body["reservation_id"] == "res-1"

    err = assert_error(
        entry.handler(make_context(input={"room_type": "presidential_suite"})),
        code="room_unavailable",
    )
    assert err.details == {"requested": "presidential_suite"}
