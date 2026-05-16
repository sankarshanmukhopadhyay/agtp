"""
End-to-end Go gateway test.

Spawns ``samples/gateway_demo_go`` as a subprocess (via ``go run`` if
no prebuilt binary exists), points it at a real ``GatewayServer`` on
a TCP loopback port, exercises the round-trip with the sample
handlers.

Skipped when ``go`` is not on PATH. CI runners with Go 1.21+ get full
coverage; runners without don't fail the build.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from server.gateway import GatewayServer
from server.schema_validation import (
    spec_to_input_schema, spec_to_output_schema,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "samples" / "gateway_demo_go"


def _go_available() -> bool:
    return shutil.which("go") is not None


pytestmark = pytest.mark.skipif(
    not _go_available(),
    reason="go interpreter not on PATH; mod_go cannot be exercised",
)


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
            function="samples/gateway_demo_go.echo",
        ),
    )


def _book_spec() -> EndpointSpec:
    return EndpointSpec(
        name="BOOK",
        path="/room",
        description="Book a room.",
        required_params=[
            ParamSpec(name="guest", type="string", description="guest"),
            ParamSpec(name="room_type", type="string", description="type"),
        ],
        output=[
            ParamSpec(name="reservation_id", type="string", description="id"),
            ParamSpec(name="agent", type="string", description="agent"),
        ],
        errors=["room_unavailable"],
        semantic=SemanticBlock(
            intent="Book.", actor="agent", outcome="Reservation id.",
            capability="transaction", confidence=0.9,
            impact="reversible", is_idempotent=False,
        ),
        handler=HandlerBinding(
            type="registered_function",
            function="samples/gateway_demo_go.bookRoom",
        ),
    )


def _start_server(specs: list) -> tuple[GatewayServer, str]:
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(
        socket_path=addr,
        server_id="e2e-go",
        daemon_version="agtpd-e2e",
        catalog_version="1.0.0",
    )
    for spec in specs:
        server.register_endpoint(
            spec,
            input_schema=spec_to_input_schema(spec),
            output_schema=spec_to_output_schema(spec),
        )
    server.start()
    return server, addr


def _start_mod_go(addr: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["go", "run", ".", "--gateway-socket", addr],
        cwd=str(SAMPLE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def go_harness() -> Iterator[tuple[GatewayServer, subprocess.Popen]]:
    server, addr = _start_server([_echo_spec(), _book_spec()])
    proc = _start_mod_go(addr)
    try:
        if not server.wait_for_module(timeout=30.0):
            stderr = b""
            if proc.stderr:
                try:
                    stderr = proc.stderr.read(4096)
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            pytest.fail(
                f"gateway-demo-go did not register within 30s. stderr:\n"
                f"{stderr.decode('utf-8', errors='replace')}"
            )
        yield server, proc
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.stop()


def test_echo_round_trip(go_harness) -> None:
    server, _ = go_harness
    result = server.dispatch(EndpointContext(
        input={"value": "hello-from-go"},
        agent_id="agent-1",
        method="QUERY",
        path="/echo",
        request_id="req-go-1",
    ))
    assert isinstance(result, EndpointResponse), f"unexpected: {result}"
    assert result.body == {"echo": "hello-from-go"}


def test_book_room_success(go_harness) -> None:
    server, _ = go_harness
    result = server.dispatch(EndpointContext(
        input={"guest": "Chris", "room_type": "double"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-go-2",
    ))
    assert isinstance(result, EndpointResponse), f"unexpected: {result}"
    assert result.body["reservation_id"] == "res-Chris-double"
    assert result.body["agent"] == "agent-abc"


def test_book_room_declared_error(go_harness) -> None:
    server, _ = go_harness
    result = server.dispatch(EndpointContext(
        input={"guest": "x", "room_type": "presidential_suite"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-go-3",
    ))
    assert isinstance(result, EndpointError), f"unexpected: {result}"
    assert result.code == "room_unavailable"
    assert result.details == {"room_type": "presidential_suite"}
