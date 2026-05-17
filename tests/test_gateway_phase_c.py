"""
Phase C gateway-capability tests.

Covers:

  * Welcome frame advertises the right capabilities based on daemon state
  * Daemon-side `sign_request` handling (with and without SigningService)
  * Daemon-side `outbound_request` handling
  * Module-side ``PythonDaemonClient.sign()`` round-trip
  * Module-side ``PythonDaemonClient.fetch()`` round-trip
  * Handler that uses ``ctx.daemon`` during dispatch
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from agtp.handlers import (
    DaemonError,
    EndpointContext,
    EndpointError,
    EndpointResponse,
    OutboundResponse,
)
from agtp.registry import HandlerRegistry
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from core.gateway import read_frame, write_frame
from mod_python.client import GatewayClient
from server.gateway import GatewayServer
from server.schema_validation import (
    spec_to_input_schema, spec_to_output_schema,
)
from server.signing import SigningService


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_signing_service(tmp_path: Path) -> SigningService:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "signing.key"
    p.write_bytes(pem)
    return SigningService.from_key_path(str(p))


def _echo_spec() -> EndpointSpec:
    return EndpointSpec(
        name="QUERY",
        path="/echo",
        description="Echo.",
        required_params=[ParamSpec(name="value", type="string", description="x")],
        output=[ParamSpec(name="echo", type="string", description="y")],
        semantic=SemanticBlock(
            intent="Echo.", actor="agent", outcome="Echoed.",
            capability="retrieval", confidence=0.99,
            impact="informational", is_idempotent=True,
        ),
        handler=HandlerBinding(
            type="registered_function",
            function="phase_c_demo.echo",
        ),
    )


# ---------------------------------------------------------------------------
# Welcome capabilities.
# ---------------------------------------------------------------------------


def test_welcome_advertises_baseline_only_when_no_signing(tmp_path: Path) -> None:
    """Daemon without signing service: welcome lists registered_function
    and outbound_call only (sign_request requires a loaded key)."""
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(socket_path=addr, server_id="t")
    server.start()
    try:
        # Connect directly with raw frames to inspect welcome.
        host, _, port_str = addr.rpartition(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((host, int(port_str)))
        rf = s.makefile("rb")
        wf = s.makefile("wb")
        write_frame(wf, {
            "type": "hello",
            "gateway_versions": ["1.0"],
            "module": {"id": "raw", "version": "0"},
        })
        welcome = read_frame(rf)
        s.close()
        assert welcome["type"] == "welcome"
        caps = set(welcome.get("capabilities") or [])
        assert "registered_function" in caps
        assert "outbound_call" in caps
        assert "sign_request" not in caps
    finally:
        server.stop()


def test_welcome_advertises_sign_request_when_signing_loaded(
    tmp_path: Path,
) -> None:
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(socket_path=addr, server_id="t")
    server.signing_service = _make_signing_service(tmp_path)
    server.start()
    try:
        host, _, port_str = addr.rpartition(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((host, int(port_str)))
        rf = s.makefile("rb")
        wf = s.makefile("wb")
        write_frame(wf, {
            "type": "hello",
            "gateway_versions": ["1.0"],
            "module": {"id": "raw", "version": "0"},
        })
        welcome = read_frame(rf)
        s.close()
        caps = set(welcome.get("capabilities") or [])
        assert "sign_request" in caps
        assert "outbound_call" in caps
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Daemon-side handlers exercised directly with raw frames.
# ---------------------------------------------------------------------------


class _ProtocolFixture:
    """Bring up a daemon and complete handshake + register; the test
    then drives raw frames in/out of the connection to verify
    daemon-side handling."""

    def __init__(self, *, with_signing_service: SigningService | None = None) -> None:
        self.port = _pick_free_port()
        self.addr = f"127.0.0.1:{self.port}"
        self.server = GatewayServer(socket_path=self.addr, server_id="t")
        if with_signing_service is not None:
            self.server.signing_service = with_signing_service
        spec = _echo_spec()
        self.server.register_endpoint(
            spec,
            input_schema=spec_to_input_schema(spec),
            output_schema=spec_to_output_schema(spec),
        )
        self.server.start()
        host, _, port_str = self.addr.rpartition(":")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect((host, int(port_str)))
        self.reader = self.sock.makefile("rb")
        self.writer = self.sock.makefile("wb")
        # Drive handshake.
        write_frame(self.writer, {
            "type": "hello",
            "gateway_versions": ["1.0"],
            "module": {"id": "raw", "version": "0"},
            "capabilities": ["registered_function", "sign_request", "outbound_call"],
        })
        self.welcome = read_frame(self.reader)
        assert self.welcome["type"] == "welcome"
        register = read_frame(self.reader)
        assert register["type"] == "register"
        write_frame(self.writer, {
            "type": "register_ack",
            "ok": True,
            "resolved": [],
        })
        # Wait for the daemon's _handle_connection to finish reading
        # the register_ack and set ._module — otherwise the test can
        # race ahead to dispatch before the daemon recognizes the
        # connection.
        assert self.server.wait_for_module(timeout=5.0), (
            "daemon did not register the test fixture as the module"
        )
        # Set a read timeout so a stalled test fails fast instead of
        # hanging forever.
        self.sock.settimeout(10.0)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.server.stop()


def test_sign_request_round_trip(tmp_path: Path) -> None:
    """A module sending sign_request gets back a verifiable sign_response."""
    service = _make_signing_service(tmp_path)
    fx = _ProtocolFixture(with_signing_service=service)
    try:
        # The test connection IS the module; dispatch happens by the
        # daemon thread when we call server.dispatch from this thread.
        # But to keep things simple, we'll exercise the sign-handler
        # path directly: in the read_until_response loop, sign_request
        # is serviced. Simulate by issuing dispatch + reading frames.
        ctx = EndpointContext(
            input={"value": "v"},
            agent_id="a", method="QUERY", path="/echo", request_id="r1",
        )
        result_container: dict = {}

        def server_dispatcher() -> None:
            try:
                result_container["result"] = fx.server.dispatch(ctx)
            except Exception as exc:  # noqa: BLE001
                result_container["error"] = exc

        # Run dispatch in a thread; this thread plays the module.
        t = threading.Thread(target=server_dispatcher, daemon=True)
        t.start()
        # Receive the request frame from the daemon.
        request = read_frame(fx.reader)
        assert request["type"] == "request"
        # As the "module", send a sign_request before responding.
        write_frame(fx.writer, {
            "type": "sign_request",
            "operation_id": "op-1",
            "data_b64": base64.urlsafe_b64encode(b"hello").rstrip(b"=").decode("ascii"),
        })
        # Read the daemon's sign_response.
        sign_resp = read_frame(fx.reader)
        assert sign_resp["type"] == "sign_response"
        assert sign_resp["operation_id"] == "op-1"
        assert sign_resp["kid"] == service.key_id
        # Verify the signature.
        sig_b64 = sign_resp["signature_b64"]
        padded = sig_b64 + "=" * (-len(sig_b64) % 4)
        sig = base64.urlsafe_b64decode(padded)
        assert service.verify(b"hello", sig) is True
        # Now finish the dispatch by sending the final response.
        write_frame(fx.writer, {
            "type": "response",
            "request_id": request["request_id"],
            "envelope": {"body": {"echo": "v"}, "status": 200},
        })
        t.join(timeout=5.0)
        assert "error" not in result_container
        result = result_container["result"]
        assert isinstance(result, EndpointResponse)
    finally:
        fx.close()


def test_sign_request_refused_without_signing_service() -> None:
    """When the daemon has no signing service, sign_request returns
    a sign_error with code signing_unavailable."""
    fx = _ProtocolFixture(with_signing_service=None)
    try:
        ctx = EndpointContext(
            input={"value": "v"},
            agent_id="a", method="QUERY", path="/echo", request_id="r2",
        )
        result_container: dict = {}

        def server_dispatcher() -> None:
            result_container["result"] = fx.server.dispatch(ctx)

        t = threading.Thread(target=server_dispatcher, daemon=True)
        t.start()
        request = read_frame(fx.reader)
        write_frame(fx.writer, {
            "type": "sign_request",
            "operation_id": "op-no-sign",
            "data_b64": "aGVsbG8",
        })
        err = read_frame(fx.reader)
        assert err["type"] == "sign_error"
        assert err["code"] == "signing_unavailable"
        # Finish dispatch.
        write_frame(fx.writer, {
            "type": "response",
            "request_id": request["request_id"],
            "envelope": {"body": {"echo": "v"}, "status": 200},
        })
        t.join(timeout=5.0)
    finally:
        fx.close()


def test_outbound_request_round_trip() -> None:
    """Daemon services outbound_request via client.core_client.fetch."""
    fx = _ProtocolFixture()
    try:
        ctx = EndpointContext(
            input={"value": "v"},
            agent_id="a", method="QUERY", path="/echo", request_id="r3",
        )

        def fake_fetch(uri, *, method, path, headers, body):
            return SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body_bytes=json.dumps({"upstream_said": "hi"}).encode("utf-8"),
            )

        result_container: dict = {}

        def server_dispatcher() -> None:
            with patch("client.core_client.fetch", fake_fetch):
                result_container["result"] = fx.server.dispatch(ctx)

        t = threading.Thread(target=server_dispatcher, daemon=True)
        t.start()
        request = read_frame(fx.reader)
        write_frame(fx.writer, {
            "type": "outbound_request",
            "operation_id": "op-out-1",
            "uri": "agtp://upstream.example.com",
            "method": "QUERY",
            "path": "/data",
            "headers": {"Agent-ID": "a"},
            "body": {"q": "x"},
        })
        outbound = read_frame(fx.reader)
        assert outbound["type"] == "outbound_response"
        assert outbound["operation_id"] == "op-out-1"
        assert outbound["status"] == 200
        assert outbound["body"] == {"upstream_said": "hi"}
        # Finish dispatch.
        write_frame(fx.writer, {
            "type": "response",
            "request_id": request["request_id"],
            "envelope": {"body": {"echo": "v"}, "status": 200},
        })
        t.join(timeout=5.0)
    finally:
        fx.close()


def test_outbound_request_surfaces_unreachable() -> None:
    fx = _ProtocolFixture()
    try:
        ctx = EndpointContext(
            input={"value": "v"},
            agent_id="a", method="QUERY", path="/echo", request_id="r4",
        )

        def failing_fetch(*_a, **_kw):
            raise ConnectionError("no route")

        def server_dispatcher() -> None:
            with patch("client.core_client.fetch", failing_fetch):
                fx.server.dispatch(ctx)

        t = threading.Thread(target=server_dispatcher, daemon=True)
        t.start()
        request = read_frame(fx.reader)
        write_frame(fx.writer, {
            "type": "outbound_request",
            "operation_id": "op-out-2",
            "uri": "agtp://x",
            "method": "QUERY",
            "path": "/",
        })
        err = read_frame(fx.reader)
        assert err["type"] == "outbound_error"
        assert err["code"] == "upstream_unreachable"
        write_frame(fx.writer, {
            "type": "response",
            "request_id": request["request_id"],
            "envelope": {"body": {"echo": "v"}, "status": 200},
        })
        t.join(timeout=5.0)
    finally:
        fx.close()


# ---------------------------------------------------------------------------
# Handler that uses ctx.daemon.
# ---------------------------------------------------------------------------


def test_handler_signs_data_through_daemon(tmp_path: Path) -> None:
    """A real mod_python handler calls ctx.daemon.sign() during dispatch;
    the signature comes back via the round-trip and verifies."""
    service = _make_signing_service(tmp_path)
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(socket_path=addr, server_id="t")
    server.signing_service = service
    spec = _echo_spec()
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    server.start()

    try:
        # Handler that asks the daemon to sign its echoed value.
        captured: dict = {}

        def sign_then_echo(ctx: EndpointContext):
            captured["caps_seen"] = list(
                (ctx.daemon._daemon_capabilities if ctx.daemon else [])
            )
            assert ctx.daemon is not None
            value = str(ctx.input.get("value") or "")
            sig = ctx.daemon.sign(value.encode("utf-8"))
            return EndpointResponse(body={
                "echo": value,
                "signature_b64": base64.urlsafe_b64encode(sig)
                    .rstrip(b"=").decode("ascii"),
            })

        registry = HandlerRegistry()
        registry.register(sign_then_echo, method="QUERY", path="/echo")
        client = GatewayClient(
            socket_path=addr,
            registry=registry,
            module_id="phase_c_test",
        )
        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()
        try:
            assert server.wait_for_module(timeout=5.0)
            result = server.dispatch(EndpointContext(
                input={"value": "secret"},
                agent_id="a",
                method="QUERY",
                path="/echo",
                request_id="r-sign",
            ))
            assert isinstance(result, EndpointResponse)
            assert result.body["echo"] == "secret"
            sig_b64 = result.body["signature_b64"]
            padded = sig_b64 + "=" * (-len(sig_b64) % 4)
            sig = base64.urlsafe_b64decode(padded)
            assert service.verify(b"secret", sig) is True
            # The handler saw the daemon advertise sign_request +
            # outbound_call (since the daemon has signing enabled).
            assert "sign_request" in captured["caps_seen"]
            assert "outbound_call" in captured["caps_seen"]
        finally:
            client.stop()
            thread.join(timeout=2.0)
    finally:
        server.stop()


def test_handler_sign_fails_gracefully_without_capability(tmp_path: Path) -> None:
    """When the daemon doesn't advertise sign_request, handler calls
    to ctx.daemon.sign() raise DaemonError with capability_not_claimed."""
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(socket_path=addr, server_id="t")
    # No signing_service — sign_request capability won't be advertised.
    spec = _echo_spec()
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    server.start()

    try:
        def handler(ctx: EndpointContext):
            try:
                ctx.daemon.sign(b"x")
                return EndpointResponse(body={"reached": "should-not"})
            except DaemonError as exc:
                return EndpointError(
                    code="sign_unavailable",
                    message=exc.code or "sign failed",
                )

        registry = HandlerRegistry()
        registry.register(
            handler, method="QUERY", path="/echo",
            errors=["sign_unavailable"],
        )
        client = GatewayClient(
            socket_path=addr,
            registry=registry,
            module_id="phase_c_test",
        )
        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()
        try:
            assert server.wait_for_module(timeout=5.0)
            result = server.dispatch(EndpointContext(
                input={"value": "x"}, agent_id="a",
                method="QUERY", path="/echo", request_id="r-no-sign",
            ))
            assert isinstance(result, EndpointError)
            assert result.code == "sign_unavailable"
            assert result.message == "capability_not_claimed"
        finally:
            client.stop()
            thread.join(timeout=2.0)
    finally:
        server.stop()
