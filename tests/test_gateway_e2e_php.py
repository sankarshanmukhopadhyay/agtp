"""
End-to-end PHP gateway test.

Spawns ``php <mod_php>/bin/run.php`` as a subprocess, points it at
a real ``GatewayServer`` on a TCP loopback socket, then exercises
the round-trip with the sample PHP handlers from
``samples/gateway_demo.php``.

The ``mod_php`` runtime lives in the external ``agtp-php`` repo
(https://github.com/nomoticai/agtp-php). The test discovers it via:

  1. ``$AGTP_MOD_PHP_DIR`` — explicit path to ``mod_php/`` directory.
  2. ``../agtp-php/mod_php/`` — sibling-checkout convention.

The whole test is skipped when ``php`` is not on PATH, when neither
location resolves to a ``bin/run.php`` file, or when Composer
dependencies haven't been installed (``mod_php/vendor/`` is absent).
CI runners with PHP 8.1+, the agtp-php sibling checkout, and
Composer pre-installed get full coverage; runners without don't
fail the build.

Operators replicating this manually:

    git clone https://github.com/nomoticai/agtp-php ../agtp-php
    composer install --working-dir=../agtp-php/agtp-php
    composer install --working-dir=../agtp-php/mod_php
    python -m pytest tests/test_gateway_e2e_php.py -v
"""

from __future__ import annotations

import os
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
SAMPLE_BOOTSTRAP = REPO_ROOT / "samples" / "gateway_demo.php"


def _resolve_mod_php_dir() -> Optional[Path]:
    env = os.environ.get("AGTP_MOD_PHP_DIR")
    if env:
        candidate = Path(env)
        if (candidate / "bin" / "run.php").exists():
            return candidate
    sibling = REPO_ROOT.parent / "agtp-php" / "mod_php"
    if (sibling / "bin" / "run.php").exists():
        return sibling
    return None


MOD_PHP_DIR = _resolve_mod_php_dir()
MOD_PHP_RUN = MOD_PHP_DIR / "bin" / "run.php" if MOD_PHP_DIR else None
MOD_PHP_VENDOR = MOD_PHP_DIR / "vendor" if MOD_PHP_DIR else None


def _php_available() -> bool:
    return shutil.which("php") is not None


def _composer_installed() -> bool:
    """True when mod_php has its Composer dependencies in place."""
    return MOD_PHP_VENDOR is not None and (MOD_PHP_VENDOR / "autoload.php").exists()


# Whole module is skipped when PHP isn't around or when the external
# agtp-php checkout isn't available. The mod_php runtime lives in
# https://github.com/nomoticai/agtp-php — clone it as a sibling
# (../agtp-php/) or set AGTP_MOD_PHP_DIR explicitly.
pytestmark = [
    pytest.mark.skipif(
        not _php_available(),
        reason="php interpreter not on PATH; mod_php cannot be exercised",
    ),
    pytest.mark.skipif(
        MOD_PHP_DIR is None,
        reason="mod_php not found; set AGTP_MOD_PHP_DIR or clone agtp-php as ../agtp-php",
    ),
    pytest.mark.skipif(
        not _composer_installed(),
        reason="mod_php/vendor/autoload.php missing; run `composer install` in mod_php/",
    ),
]


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
        description="Echo input back.",
        required_params=[ParamSpec(name="value", type="string", description="x")],
        output=[ParamSpec(name="echo", type="string", description="y")],
        semantic=SemanticBlock(
            intent="Echo.", actor="agent", outcome="Returns the value.",
            capability="retrieval", confidence=0.99,
            impact="informational", is_idempotent=True,
        ),
        handler=HandlerBinding(
            type="registered_function",
            function="Samples\\GatewayDemo\\GatewayDemoHandlers::echo",
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
            intent="Book.", actor="agent", outcome="Returns reservation id.",
            capability="transaction", confidence=0.9,
            impact="reversible", is_idempotent=False,
        ),
        handler=HandlerBinding(
            type="registered_function",
            function="Samples\\GatewayDemo\\GatewayDemoHandlers::bookRoom",
        ),
    )


def _start_server(specs: list) -> tuple[GatewayServer, str]:
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = GatewayServer(
        socket_path=addr,
        server_id="e2e-php",
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


def _start_mod_php(addr: str) -> subprocess.Popen:
    """Launch mod_php pointed at the addr; capture stderr for diagnostics."""
    proc = subprocess.Popen(
        [
            "php",
            str(MOD_PHP_RUN),
            "--gateway-socket", addr,
            "--bootstrap", str(SAMPLE_BOOTSTRAP),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )
    return proc


@pytest.fixture
def php_harness() -> Iterator[tuple[GatewayServer, subprocess.Popen]]:
    server, addr = _start_server([_echo_spec(), _book_spec()])
    proc = _start_mod_php(addr)
    try:
        if not server.wait_for_module(timeout=10.0):
            stderr = proc.stderr.read(4096).decode("utf-8", errors="replace") if proc.stderr else ""
            proc.terminate()
            proc.wait(timeout=5.0)
            pytest.fail(f"mod_php did not register within 10s. stderr:\n{stderr}")
        yield server, proc
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.stop()


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_echo_round_trip(php_harness) -> None:
    server, _proc = php_harness

    ctx = EndpointContext(
        input={"value": "hello-from-php"},
        agent_id="agent-1",
        method="QUERY",
        path="/echo",
        request_id="req-php-1",
    )
    result = server.dispatch(ctx)
    assert isinstance(result, EndpointResponse), f"unexpected result: {result}"
    assert result.body == {"echo": "hello-from-php"}


def test_book_room_success(php_harness) -> None:
    server, _proc = php_harness

    ctx = EndpointContext(
        input={"guest": "Chris", "room_type": "double"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-php-2",
    )
    result = server.dispatch(ctx)
    assert isinstance(result, EndpointResponse), f"unexpected result: {result}"
    assert result.body["reservation_id"] == "res-Chris-double"
    assert result.body["agent"] == "agent-abc"


def test_book_room_declared_error(php_harness) -> None:
    server, _proc = php_harness

    ctx = EndpointContext(
        input={"guest": "x", "room_type": "presidential_suite"},
        agent_id="agent-abc",
        method="BOOK",
        path="/room",
        request_id="req-php-3",
    )
    result = server.dispatch(ctx)
    assert isinstance(result, EndpointError), f"unexpected result: {result}"
    assert result.code == "room_unavailable"
    assert result.details == {"room_type": "presidential_suite"}
