"""
Test helpers for AGTP handler authors.

Lets a developer write unit tests against their handlers without
spinning up ``agtpd`` or the gateway socket. Build a synthetic
:class:`EndpointContext`, call the handler, assert on the
:class:`EndpointResponse` or :class:`EndpointError`.

::

    from agtp.testing import make_context, assert_ok, assert_error
    from myapp.handlers import book_room

    def test_book_room_success():
        ctx = make_context(input={
            "guest_name": "Chris",
            "check_in": "2026-06-01",
            "check_out": "2026-06-03",
            "room_type": "double",
        })
        response = assert_ok(book_room(ctx))
        assert "reservation_id" in response.body

    def test_book_room_refused_when_room_type_unavailable():
        ctx = make_context(input={"room_type": "presidential_suite", ...})
        error = assert_error(book_room(ctx), code="room_unavailable")
        assert error.details["room_type"] == "presidential_suite"

This module deliberately has no dependency on ``agtpd``, the gateway
socket, the wire format, or the TOML endpoint registry. The handler
is exercised as a pure function.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agtp.handlers import (
    EndpointContext,
    EndpointError,
    EndpointResponse,
    HandlerResult,
)


def make_context(
    *,
    input: Optional[Dict[str, Any]] = None,
    method: str = "QUERY",
    path: str = "/",
    agent_id: str = "test-agent",
    principal_id: str = "",
    agent_scopes: Optional[List[str]] = None,
    authority_scope: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    request_id: str = "test-req-1",
    headers: Optional[Dict[str, str]] = None,
) -> EndpointContext:
    """Build a synthetic :class:`EndpointContext` for unit testing.

    Defaults are chosen to be the most common shape: an authenticated
    test agent with no special scopes, no session, an empty body.
    Override any field by keyword.
    """
    return EndpointContext(
        input=dict(input or {}),
        agent_id=agent_id,
        principal_id=principal_id,
        agent_scopes=list(agent_scopes or []),
        authority_scope=list(authority_scope or []),
        session_id=session_id,
        task_id=task_id,
        request_id=request_id,
        method=method.upper(),
        path=path,
        headers=dict(headers or {}),
    )


def assert_ok(result: HandlerResult) -> EndpointResponse:
    """Assert the handler returned a success; return the response.

    Use when the test expects the happy path. Raises ``AssertionError``
    with a useful message if the handler returned an error instead.
    """
    if isinstance(result, EndpointError):
        raise AssertionError(
            f"expected EndpointResponse, got EndpointError "
            f"code={result.code!r} message={result.message!r}"
        )
    if not isinstance(result, EndpointResponse):
        raise AssertionError(
            f"expected EndpointResponse, got {type(result).__name__}: {result!r}"
        )
    return result


def assert_error(
    result: HandlerResult, *, code: Optional[str] = None,
) -> EndpointError:
    """Assert the handler returned a declared error; return it.

    If ``code`` is supplied, additionally asserts the error's code
    matches exactly.
    """
    if isinstance(result, EndpointResponse):
        raise AssertionError(
            f"expected EndpointError, got EndpointResponse "
            f"status={result.status} body={result.body!r}"
        )
    if not isinstance(result, EndpointError):
        raise AssertionError(
            f"expected EndpointError, got {type(result).__name__}: {result!r}"
        )
    if code is not None and result.code != code:
        raise AssertionError(
            f"expected EndpointError code={code!r}, got code={result.code!r} "
            f"(message: {result.message!r})"
        )
    return result


__all__ = [
    "assert_error",
    "assert_ok",
    "make_context",
]
