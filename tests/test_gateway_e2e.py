"""
End-to-end gateway tests: real ``server.gateway.GatewayServer`` talks
to real ``mod_python.client.GatewayClient`` over a TCP loopback
socket. No subprocesses; both halves run as threads in the test
process so failures produce normal tracebacks.

These tests pin the protocol contract from both sides. If a future
change to the daemon's frame shape silently breaks mod_python (or
vice versa), these tests fail.

The handlers under test are the ones in ``samples/gateway_demo.py``,
exercised through the ``@endpoint`` decorator they use — that's the
same surface a third-party developer would use.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import pytest

from agtp.handlers import EndpointError, EndpointResponse
from agtp.registry import HandlerRegistry
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from mod_python.client import GatewayClient
from server.gateway import GatewayServer
from server.schema_validation import (
    spec_to_input_schema, spec_to_output_schema,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _echo_spec() -> EndpointSpec:
    return EndpointSpec(
        name="QUERY",
        path="/echo",
        description="Echo the input value back unchanged.",
        required_params=[ParamSpec(
            name="value", type="string", description="value to echo",
        )],
        output=[ParamSpec(
            name="echo", type="string", description="echoed value",
        )],
        semantic=SemanticBlock(
            intent="Return the input value unchanged.",
            actor="agent",
            outcome="Returns the value echoed.",
            capability="retrieval",
            confidence=0.99,
            impact="informational",
            is_idempotent=True,
        ),
        handler=HandlerBinding(
            type="registered_function",
            function="samples.gateway_demo.echo",
        ),
    )


def _book_spec() -> EndpointSpec:
    return EndpointSpec(
        name="BOOK",
        path="/room",
        description="Toy room booking.",
        required_params=[
            ParamSpec(name="guest", type="string", description="guest name"),
            ParamSpec(name="room_type", type="string", description="room type"),
        ],
        output=[
            ParamSpec(name="reservation_id", type="string", description="id"),
            ParamSpec(name="agent", type="string", description="booking agent"),
        ],
        errors=["room_unavailable"],
        semantic=SemanticBlock(
            intent="Book a room.",
            actor="agent",
            outcome="Returns a reservation id.",
            capability="transaction",
            confidence=0.9,
            impact="reversible",
            is_idempotent=False,
        ),
        handler=HandlerBinding(
            type="registered_function",
            function="samples.gateway_demo.book_room",
        ),
    )


# ---------------------------------------------------------------------------
# Fixture: a running daemon + a connected mod_python in two threads.
# ---------------------------------------------------------------------------


class _GatewayHarness:
    """Daemon + mod_python both running in-process, ready for dispatch.

    Handlers are registered explicitly into the client's private
    :class:`HandlerRegistry`. We do not exercise the ``@endpoint``
    decorator here — that's covered in ``test_agtp_registry.py`` —
    so each test gets isolated handler state regardless of Python's
    module cache.
    """

    def __init__(self) -> None:
        self.server: Optional[GatewayServer] = None
        self.client: Optional[GatewayClient] = None
        self.client_thread: Optional[threading.Thread] = None
        self.client_registry = HandlerRegistry()

    def start(self, specs_and_handlers: list) -> None:
        """``specs_and_handlers`` is a list of ``(EndpointSpec, callable)``."""
        port = _pick_free_port()
        addr = f"127.0.0.1:{port}"

        self.server = GatewayServer(
            socket_path=addr,
            server_id="e2e-server",
            daemon_version="agtpd-e2e",
            catalog_version="1.0.0",
        )
        for spec, handler in specs_and_handlers:
            self.server.register_endpoint(
                spec,
                input_schema=spec_to_input_schema(spec),
                output_schema=spec_to_output_schema(spec),
            )
            self.client_registry.register(
                handler,
                method=spec.method,
                path=spec.path or "/",
                errors=list(spec.errors or []),
                description=spec.description,
            )
        self.server.start()

        self.client = GatewayClient(
            socket_path=addr,
            registry=self.client_registry,
            module_id="mod_python_e2e",
        )
        self.client_thread = threading.Thread(
            target=self.client.run, name="e2e-mod-python", daemon=True
        )
        self.client_thread.start()

        if not self.server.wait_for_module(timeout=5.0):
            raise TimeoutError("mod_python did not register within 5s")

    def stop(self) -> None:
        if self.client is not None:
            self.client.stop()
        if self.server is not None:
            self.server.stop()
        if self.client_thread is not None:
            self.client_thread.join(timeout=2.0)


@pytest.fixture
def harness():
    h = _GatewayHarness()
    yield h
    h.stop()


# Direct references to the sample handlers, imported once. samples.gateway_demo
# also registers them into the *global* agtp.registry via @endpoint as a
# side effect, but the e2e tests use their own private HandlerRegistry so
# that side effect doesn't matter here.
from samples.gateway_demo import book_room, echo  # noqa: E402


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_echo_round_trip(harness: _GatewayHarness) -> None:
    harness.start([(_echo_spec(), echo)])

    from agtp.handlers import EndpointContext
    ctx = EndpointContext(
        input={"value": "hello"},
        agent_id="agent-1",
        method="QUERY",
        path="/echo",
        request_id="req-e2e-1",
    )

    result = harness.server.dispatch(ctx)
    assert isinstance(result, EndpointResponse)
    assert result.body == {"echo": "hello"}
    assert result.status == 200


def test_book_room_success(harness: _GatewayHarness) -> None:
    harness.start([(_book_spec(), book_room)])

    from agtp.handlers import EndpointContext
    ctx = EndpointContext(
        input={"guest": "Chris", "room_type": "double"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-e2e-2",
    )

    result = harness.server.dispatch(ctx)
    assert isinstance(result, EndpointResponse)
    assert result.body["reservation_id"] == "res-Chris-double"
    assert result.body["agent"] == "agent-abc"


def test_book_room_declared_error(harness: _GatewayHarness) -> None:
    harness.start([(_book_spec(), book_room)])

    from agtp.handlers import EndpointContext
    ctx = EndpointContext(
        input={"guest": "Chris", "room_type": "presidential_suite"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-e2e-3",
    )

    result = harness.server.dispatch(ctx)
    assert isinstance(result, EndpointError)
    assert result.code == "room_unavailable"
    assert result.details == {"room_type": "presidential_suite"}


def test_multiple_endpoints_routed_correctly(harness: _GatewayHarness) -> None:
    harness.start([(_echo_spec(), echo), (_book_spec(), book_room)])

    from agtp.handlers import EndpointContext

    echo_result = harness.server.dispatch(EndpointContext(
        input={"value": "x"},
        agent_id="a",
        method="QUERY",
        path="/echo",
        request_id="r1",
    ))
    book_result = harness.server.dispatch(EndpointContext(
        input={"guest": "G", "room_type": "single"},
        agent_id="a",
        method="BOOK",
        path="/room",
        request_id="r2",
    ))

    assert isinstance(echo_result, EndpointResponse)
    assert echo_result.body == {"echo": "x"}
    assert isinstance(book_result, EndpointResponse)
    assert book_result.body["reservation_id"] == "res-G-single"


def test_module_disconnect_yields_503(harness: _GatewayHarness) -> None:
    harness.start([(_echo_spec(), echo)])

    # Cleanly shut down the module side.
    harness.client.stop()
    # Close the socket from our side so the daemon notices on next dispatch.
    if harness.client._sock is not None:
        try:
            harness.client._sock.shutdown(socket.SHUT_RDWR)
            harness.client._sock.close()
        except OSError:
            pass
    # Give the daemon a moment.
    time.sleep(0.1)

    from agtp.handlers import EndpointContext
    result = harness.server.dispatch(EndpointContext(
        input={"value": "x"},
        agent_id="a",
        method="QUERY",
        path="/echo",
        request_id="r-after",
    ))
    assert isinstance(result, EndpointError)
    assert result.code == "gateway_unavailable"
