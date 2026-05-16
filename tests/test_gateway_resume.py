"""
Tests for ``register_resume`` (gateway spec §6.4).

The optimization: a module that reconnects with a known
``cached_manifest_hash`` skips schema retransmission when the daemon's
current hash matches. Useful for PHP-FPM-style worker recycle where
the manifest hasn't changed between worker generations.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from agtp.handlers import EndpointResponse
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
            function="samples.gateway_demo.echo",
        ),
    )


def _make_server(addr: str) -> GatewayServer:
    server = GatewayServer(
        socket_path=addr,
        server_id="resume-test",
        daemon_version="agtpd-test",
        catalog_version="1.0.0",
    )
    spec = _echo_spec()
    server.register_endpoint(
        spec,
        input_schema=spec_to_input_schema(spec),
        output_schema=spec_to_output_schema(spec),
    )
    return server


# ---------------------------------------------------------------------------
# Daemon side: cached_manifest_hash match → register_resume.
# ---------------------------------------------------------------------------


def test_register_resume_sent_when_hashes_match() -> None:
    """Daemon emits register_resume (no schemas) when hashes match."""
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = _make_server(addr)
    server.start()

    try:
        # First connection: get the manifest_hash via a normal register.
        host, _, port_str = addr.rpartition(":")
        s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s1.settimeout(5.0)
        s1.connect((host, int(port_str)))
        r1 = s1.makefile("rb")
        w1 = s1.makefile("wb")
        write_frame(w1, {
            "type": "hello",
            "gateway_versions": ["1.0"],
            "module": {"id": "m", "version": "0.0"},
        })
        assert read_frame(r1)["type"] == "welcome"
        register = read_frame(r1)
        assert register["type"] == "register"
        manifest_hash = register["manifest_hash"]
        assert manifest_hash.startswith("sha256:")
        write_frame(w1, {"type": "register_ack", "ok": True, "resolved": []})
        # Don't keep the connection alive; the daemon replaces modules
        # on new connections in v1.
        s1.close()
        time.sleep(0.05)

        # Second connection: declare the cached hash, expect register_resume.
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.settimeout(5.0)
        s2.connect((host, int(port_str)))
        r2 = s2.makefile("rb")
        w2 = s2.makefile("wb")
        write_frame(w2, {
            "type": "hello",
            "gateway_versions": ["1.0"],
            "module": {"id": "m", "version": "0.0"},
            "cached_manifest_hash": manifest_hash,
        })
        assert read_frame(r2)["type"] == "welcome"
        resume = read_frame(r2)
        assert resume["type"] == "register_resume"
        assert resume["manifest_hash"] == manifest_hash
        # No schemas in the resume frame — that's the entire point.
        assert "schemas" not in resume
        assert "endpoints" not in resume
        write_frame(w2, {"type": "register_ack", "ok": True, "resolved": []})
        s2.close()
    finally:
        server.stop()


def test_register_full_sent_when_hash_mismatched() -> None:
    """A stale cached hash falls back to the full register frame."""
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = _make_server(addr)
    server.start()

    try:
        host, _, port_str = addr.rpartition(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((host, int(port_str)))
        r = s.makefile("rb")
        w = s.makefile("wb")
        write_frame(w, {
            "type": "hello",
            "gateway_versions": ["1.0"],
            "module": {"id": "m", "version": "0.0"},
            "cached_manifest_hash": "sha256:" + ("0" * 64),  # bogus
        })
        assert read_frame(r)["type"] == "welcome"
        register = read_frame(r)
        assert register["type"] == "register"
        # Full register includes the schemas inline.
        assert "schemas" in register
        assert "endpoints" in register
        s.close()
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Module side: cache the hash, reuse on resume.
# ---------------------------------------------------------------------------


def _make_client_registry() -> HandlerRegistry:
    reg = HandlerRegistry()
    from samples.gateway_demo import echo as echo_handler
    reg.register(echo_handler, method="QUERY", path="/echo")
    return reg


def test_module_caches_hash_after_full_register() -> None:
    """After a normal register, the module's cached_manifest_hash is set."""
    port = _pick_free_port()
    addr = f"127.0.0.1:{port}"
    server = _make_server(addr)
    server.start()

    client = GatewayClient(
        socket_path=addr,
        registry=_make_client_registry(),
        module_id="resume-test-mod",
    )
    thread = threading.Thread(target=client.run, daemon=True)
    thread.start()
    try:
        assert server.wait_for_module(timeout=2.0)
        # The client should have populated its cache after registration.
        assert client.cached_manifest_hash.startswith("sha256:")
        assert client._cached_bindings  # at least one binding cached
        assert ("QUERY", "/echo") in client._cached_bindings
    finally:
        client.stop()
        server.stop()
        thread.join(timeout=2.0)


def test_module_handles_register_resume_frame() -> None:
    """The GatewayClient's resume path: hello-with-cached-hash → resume
    → register_ack with cached bindings.

    This exercises the module-side resume logic directly without going
    through the GatewayServer. Pre-seeded cache simulates what a real
    worker would persist across restarts.
    """
    # Stand up a tiny mock daemon that emits register_resume.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]
    addr = f"127.0.0.1:{port}"

    captured_hello = {}
    captured_ack = {}

    def mock_daemon():
        conn, _ = server_sock.accept()
        try:
            r = conn.makefile("rb")
            w = conn.makefile("wb")
            captured_hello.update(read_frame(r))
            write_frame(w, {
                "type": "welcome",
                "gateway_version": "1.0",
                "daemon": {"version": "mock", "server_id": "test"},
                "capabilities": ["registered_function"],
            })
            write_frame(w, {
                "type": "register_resume",
                "manifest_hash": "sha256:" + ("a" * 64),
            })
            captured_ack.update(read_frame(r))
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    daemon_thread = threading.Thread(target=mock_daemon, daemon=True)
    daemon_thread.start()

    try:
        registry = _make_client_registry()
        entry = registry.lookup("QUERY", "/echo")
        assert entry is not None

        client = GatewayClient(
            socket_path=addr,
            registry=registry,
            module_id="resume-mod",
            cached_manifest_hash="sha256:" + ("a" * 64),
        )
        # Simulate a persisted binding cache (the runtime concern; the
        # protocol just requires that the cache exist).
        client._cached_bindings = {("QUERY", "/echo"): entry.handler}

        # Run only the handshake; we don't need the serve loop for this test.
        client._connect()
        try:
            client._handshake()
        finally:
            client._close()

        daemon_thread.join(timeout=2.0)

        # The hello frame advertised our cached hash.
        assert captured_hello["cached_manifest_hash"] == "sha256:" + ("a" * 64)
        # We acked OK with our cached bindings reused.
        assert captured_ack["type"] == "register_ack"
        assert captured_ack["ok"] is True
        assert "QUERY /echo" in (captured_ack.get("resolved") or [])
        # The client's working binding set was restored from the cache.
        assert ("QUERY", "/echo") in client._bindings
    finally:
        try:
            server_sock.close()
        except OSError:
            pass
