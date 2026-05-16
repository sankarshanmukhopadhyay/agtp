"""
End-to-end Rust gateway test.

Builds and runs ``samples/gateway_demo_rust`` as a subprocess. Skipped
when ``cargo`` is not on PATH or when the sample binary fails to
build before the first test.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path
from typing import Iterator

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
SAMPLE_DIR = REPO_ROOT / "samples" / "gateway_demo_rust"


def _cargo_available() -> bool:
    return shutil.which("cargo") is not None


pytestmark = pytest.mark.skipif(
    not _cargo_available(),
    reason="cargo not on PATH; mod_rust cannot be exercised",
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
            function="samples/gateway_demo_rust.echo",
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
            function="samples/gateway_demo_rust.book_room",
        ),
    )


def _start_server(specs: list) -> tuple[GatewayServer, str]:
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(
        socket_path=addr,
        server_id="e2e-rust",
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


@pytest.fixture(scope="module")
def rust_sample_binary() -> str:
    """Build the Rust sample once per session and return its path."""
    result = subprocess.run(
        ["cargo", "build"],
        cwd=str(SAMPLE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        pytest.skip(
            f"cargo build failed for samples/gateway_demo_rust:\n"
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    # Locate the binary in target/debug/.
    binary = SAMPLE_DIR / "target" / "debug" / "gateway-demo-rust"
    if not binary.exists():
        binary_exe = SAMPLE_DIR / "target" / "debug" / "gateway-demo-rust.exe"
        if binary_exe.exists():
            binary = binary_exe
    if not binary.exists():
        pytest.fail(f"built gateway-demo-rust binary not found in {binary.parent}")
    return str(binary)


@pytest.fixture
def rust_harness(rust_sample_binary: str) -> Iterator[tuple[GatewayServer, subprocess.Popen]]:
    server, addr = _start_server([_echo_spec(), _book_spec()])
    proc = subprocess.Popen(
        [rust_sample_binary, "--gateway-socket", addr],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not server.wait_for_module(timeout=10.0):
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
                f"Rust sample did not register within 10s. stderr:\n"
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


def test_echo_round_trip(rust_harness) -> None:
    server, _ = rust_harness
    result = server.dispatch(EndpointContext(
        input={"value": "hello-from-rust"},
        agent_id="agent-1",
        method="QUERY",
        path="/echo",
        request_id="req-rust-1",
    ))
    assert isinstance(result, EndpointResponse), f"unexpected: {result}"
    assert result.body == {"echo": "hello-from-rust"}


def test_book_room_success(rust_harness) -> None:
    server, _ = rust_harness
    result = server.dispatch(EndpointContext(
        input={"guest": "Chris", "room_type": "double"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-rust-2",
    ))
    assert isinstance(result, EndpointResponse), f"unexpected: {result}"
    assert result.body["reservation_id"] == "res-Chris-double"
    assert result.body["agent"] == "agent-abc"


def test_book_room_declared_error(rust_harness) -> None:
    server, _ = rust_harness
    result = server.dispatch(EndpointContext(
        input={"guest": "x", "room_type": "presidential_suite"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-rust-3",
    ))
    assert isinstance(result, EndpointError), f"unexpected: {result}"
    assert result.code == "room_unavailable"
    assert result.details == {"room_type": "presidential_suite"}
