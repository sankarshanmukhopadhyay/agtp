"""
Daemon-side gateway-server tests using a mock-module fixture.

Exercises :class:`server.gateway.GatewayServer` end-to-end on the
loopback transport (TCP/127.0.0.1) so the test suite runs the same
on Linux, macOS, and Windows. Production usage prefers Unix sockets;
the GatewayServer accepts both transports.

The "mock module" is a thread that connects to the server, performs
the handshake/registration, and serves request frames according to a
table the test supplies. This isolates the daemon-side logic from
the still-being-built ``mod_python``.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from core.gateway import read_frame, write_frame
from server.gateway import GatewayServer
from server.schema_validation import (
    spec_to_input_schema, spec_to_output_schema,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Bind ephemeral port, close, return the port number.

    Race condition is benign for tests: the OS rarely reuses the port
    in the microsecond gap, and each test that races just retries.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_spec(method: str, path: str, errors: list = ()) -> EndpointSpec:
    """Build a minimal EndpointSpec with a registered_function binding."""
    return EndpointSpec(
        name=method,
        path=path,
        description=f"{method} {path}",
        required_params=[ParamSpec(
            name="value",
            type="string",
            description="payload",
        )],
        output=[ParamSpec(
            name="echo",
            type="string",
            description="echoed value",
        )],
        errors=list(errors),
        semantic=SemanticBlock(
            intent=f"{method} the resource",
            actor="agent",
            outcome="returns the resource",
            capability="retrieval",
            confidence=0.9,
            impact="informational",
            is_idempotent=True,
        ),
        handler=HandlerBinding(type="registered_function", function="test.handler"),
    )


@pytest.fixture
def gateway_loopback():
    """A started GatewayServer on a fresh loopback port + the address.

    Tears down the server after the test, regardless of failure.
    """
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(
        socket_path=addr,
        server_id="test-server",
        daemon_version="agtpd-test",
        catalog_version="1.0.0",
    )
    yield server, addr
    server.stop()


# ---------------------------------------------------------------------------
# Mock module: connects, does handshake/registration, serves requests.
# ---------------------------------------------------------------------------


class MockModule:
    """Test-only client that emulates a runtime module.

    Use ``responder=...`` to inject behavior per request. The responder
    takes the parsed ``request`` frame and returns a response payload
    (the ``envelope`` value); the test infrastructure wraps it in a
    full response frame.
    """

    def __init__(
        self,
        address: str,
        *,
        responder: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        gateway_versions: list = ("1.0",),
        capabilities: list = ("registered_function",),
        ack_ok: bool = True,
        ack_errors: Optional[list] = None,
    ) -> None:
        self.address = address
        self.responder = responder
        self.gateway_versions = list(gateway_versions)
        self.capabilities = list(capabilities)
        self.ack_ok = ack_ok
        self.ack_errors = list(ack_errors or [])

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.registered_endpoints: list = []
        self.received_schemas: dict = {}
        self.welcome: Dict[str, Any] = {}
        self.manifest_hash: str = ""
        self.exception: Optional[BaseException] = None

    def connect_and_register(self) -> None:
        host, _, port_str = self.address.rpartition(":")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect((host, int(port_str)))
        reader = self._sock.makefile("rb")
        writer = self._sock.makefile("wb")

        # Send hello.
        write_frame(writer, {
            "type": "hello",
            "gateway_versions": self.gateway_versions,
            "module": {
                "id": "mock_module",
                "version": "0.0.1",
                "runtime": "test",
                "pid": 12345,
            },
            "capabilities": self.capabilities,
        })

        # Read welcome.
        self.welcome = read_frame(reader)
        assert self.welcome["type"] == "welcome"

        # Read register.
        register = read_frame(reader)
        assert register["type"] == "register"
        self.registered_endpoints = list(register.get("endpoints") or [])
        self.received_schemas = dict(register.get("schemas") or {})
        self.manifest_hash = str(register.get("manifest_hash") or "")

        # Send register_ack.
        ack: Dict[str, Any] = {"type": "register_ack", "ok": self.ack_ok}
        if self.ack_ok:
            ack["resolved"] = [
                f"{ep['method']} {ep['path']}" for ep in self.registered_endpoints
            ]
        else:
            ack["errors"] = self.ack_errors
        write_frame(writer, ack)
        self._reader = reader
        self._writer = writer
        self._ready.set()

    def serve_loop(self) -> None:
        try:
            self.connect_and_register()
            while not self._stop.is_set():
                try:
                    request = read_frame(self._reader)
                except Exception:
                    return
                if request.get("type") != "request":
                    continue
                if self.responder is None:
                    response_envelope: Dict[str, Any] = {
                        "body": {"echo": "default"},
                    }
                else:
                    response_envelope = self.responder(request)
                write_frame(self._writer, {
                    "type": "response",
                    "request_id": request["request_id"],
                    "envelope": response_envelope,
                })
        except BaseException as exc:  # noqa: BLE001
            self.exception = exc
        finally:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_loop, name="mock-module", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5.0):
            raise TimeoutError("mock module did not complete registration")
        if self.exception is not None:
            raise self.exception

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def _make_ctx(method: str, path: str, value: str = "hello") -> EndpointContext:
    return EndpointContext(
        input={"value": value},
        agent_id="test-agent-abc",
        principal_id="chris@example.com",
        authority_scope=["scope:read"],
        request_id="test-1",
        method=method,
        path=path,
        headers={"agent-id": "test-agent-abc"},
    )


def test_dispatch_returns_503_when_no_module_connected(gateway_loopback) -> None:
    server, _ = gateway_loopback
    server.register_endpoint(
        _make_spec("BOOK", "/room"),
        input_schema=spec_to_input_schema(_make_spec("BOOK", "/room")),
        output_schema=spec_to_output_schema(_make_spec("BOOK", "/room")),
    )
    server.start()

    result = server.dispatch(_make_ctx("BOOK", "/room"))
    assert isinstance(result, EndpointError)
    assert result.code == "gateway_unavailable"


def test_handshake_and_register_succeed(gateway_loopback) -> None:
    server, addr = gateway_loopback
    spec = _make_spec("BOOK", "/room", errors=["room_unavailable"])
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    server.start()

    mock = MockModule(addr)
    mock.start()

    # Daemon told us about its server_id + catalog version.
    assert mock.welcome["daemon"]["server_id"] == "test-server"
    assert mock.welcome["daemon"]["catalog_version"] == "1.0.0"
    assert mock.welcome["gateway_version"] == "1.0"

    # We received the endpoint declaration and schemas.
    assert len(mock.registered_endpoints) == 1
    ep = mock.registered_endpoints[0]
    assert ep["method"] == "BOOK"
    assert ep["path"] == "/room"
    assert ep["handler_reference"] == "test.handler"
    assert "room_unavailable" in ep["errors"]
    # Schemas were inlined under the ref labels we received.
    assert ep["input_schema_ref"].startswith("#/schemas/")
    in_ref_key = ep["input_schema_ref"].split("/")[-1]
    assert in_ref_key in mock.received_schemas
    # manifest_hash is a stable sha256.
    assert mock.manifest_hash.startswith("sha256:")
    assert len(mock.manifest_hash) == len("sha256:") + 64

    mock.stop()


def test_dispatch_round_trip(gateway_loopback) -> None:
    server, addr = gateway_loopback
    spec = _make_spec("BOOK", "/room")
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    server.start()

    received_requests: list = []

    def responder(request: Dict[str, Any]) -> Dict[str, Any]:
        received_requests.append(request)
        return {
            "body": {"echo": request["envelope"]["input"]["value"]},
            "status": 200,
        }

    mock = MockModule(addr, responder=responder)
    mock.start()
    assert server.wait_for_module(timeout=2.0)

    result = server.dispatch(_make_ctx("BOOK", "/room", value="hello-world"))
    assert isinstance(result, EndpointResponse)
    assert result.body == {"echo": "hello-world"}
    assert result.status == 200

    # The mock module received exactly one request frame with the right shape.
    assert len(received_requests) == 1
    received = received_requests[0]
    assert received["envelope"]["method"] == "BOOK"
    assert received["envelope"]["path"] == "/room"
    assert received["envelope"]["agent_id"] == "test-agent-abc"
    assert received["envelope"]["principal_id"] == "chris@example.com"
    assert received["envelope"]["authority_scope"] == ["scope:read"]
    assert received["trust"]["verified"] is True
    assert received["trust"]["method"] == "agent_id_header"

    mock.stop()


def test_dispatch_carries_endpoint_error_back(gateway_loopback) -> None:
    server, addr = gateway_loopback
    spec = _make_spec("BOOK", "/room", errors=["room_unavailable"])
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    server.start()

    def responder(request: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "endpoint_error": {
                "code": "room_unavailable",
                "message": "no rooms",
                "details": {"hotel": "Grand"},
            }
        }

    mock = MockModule(addr, responder=responder)
    mock.start()
    assert server.wait_for_module(timeout=2.0)

    result = server.dispatch(_make_ctx("BOOK", "/room"))
    assert isinstance(result, EndpointError)
    assert result.code == "room_unavailable"
    assert result.message == "no rooms"
    assert result.details == {"hotel": "Grand"}

    mock.stop()


def test_dispatch_after_module_disconnect_returns_503(gateway_loopback) -> None:
    server, addr = gateway_loopback
    spec = _make_spec("BOOK", "/room")
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    server.start()

    mock = MockModule(addr)
    mock.start()
    assert server.wait_for_module(timeout=2.0)

    # Disconnect the mock module.
    mock.stop()
    # Give the daemon's read loop a moment to notice. The next dispatch
    # writes onto a closed socket; the dispatch path treats that as a
    # gateway-unavailable failure.
    time.sleep(0.1)

    result = server.dispatch(_make_ctx("BOOK", "/room"))
    assert isinstance(result, EndpointError)
    assert result.code == "gateway_unavailable"


def test_version_negotiation_refuses_unknown_version(gateway_loopback) -> None:
    server, addr = gateway_loopback
    server.start()

    host, _, port_str = addr.rpartition(":")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect((host, int(port_str)))
    reader = s.makefile("rb")
    writer = s.makefile("wb")

    write_frame(writer, {
        "type": "hello",
        "gateway_versions": ["99.9"],
        "module": {"id": "m", "version": "0.0"},
    })

    err = read_frame(reader)
    assert err["type"] == "error"
    assert err["code"] == "gateway_version_unsupported"
    s.close()
