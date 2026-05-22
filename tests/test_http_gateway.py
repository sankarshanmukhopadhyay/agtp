"""
Tests for RCNS-5 — mod_http_gateway sidecar.

End-to-end coverage: a real HTTP listener accepts requests on an
ephemeral port, the gateway translates them into AGTPRequests
through the daemon's dispatch path, and the daemon's
AGTPResponse comes back as an HTTP response.

The tests use the stdlib's ``http.client`` so the gateway is
exercised over a real socket, not a mocked one. This catches
header-translation bugs and verb-resolution gaps that a unit
test would miss.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from operational.mod_http_gateway import HttpGatewayServer
from server import config as cfg_module
from server.config import (
    AgentsConfig, AuditConfig, GatewayConfig, MtlsConfig,
    RcnsConfig, ServerConfig, ServerInfo, ServerPolicy,
    SigningConfig, SynthesisConfig, default_methods_policy,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _ephemeral_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config() -> ServerConfig:
    return ServerConfig(
        server=ServerInfo(server_id="t.local", operator="o", contact="c"),
        policy=ServerPolicy(synthesis_enabled=True),
        synthesis=SynthesisConfig(),
        rcns=RcnsConfig(),
        audit=AuditConfig(),
        signing=SigningConfig(),
    )


def _doc(agent_id: str = "a" * 64) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["DISCOVER", "DESCRIBE", "QUERY", "FETCH"],
            wildcards=True,
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
    )


def _state(
    *,
    config: Optional[ServerConfig] = None,
    doc: Optional[AgentDocument] = None,
) -> Any:
    config = config or _config()
    doc = doc or _doc()
    state = MagicMock()
    state.config = config
    state.methods_policy = default_methods_policy()
    state.synthesis_runtime = None
    state.endpoint_registry = None
    state.list_ids = lambda: [doc.agent_id]
    state.lookup = lambda aid: doc if aid.lower() == doc.agent_id.lower() else None
    state.lookup_genesis = lambda _aid: None
    state.signing_service = None
    return state


@pytest.fixture
def gateway_server():
    """Spin up an HTTP gateway on an ephemeral port and tear it down
    after the test. Each test gets a fresh server thread + clean
    server_state so they can't interfere."""
    port = _ephemeral_port()
    state = _state()
    gw = HttpGatewayServer(
        host="127.0.0.1", port=port,
        server_state=state, pinned_agent_id="",
    )
    thread = threading.Thread(target=gw.serve_forever, daemon=True)
    thread.start()
    try:
        yield gw, state, port
    finally:
        gw.shutdown()
        thread.join(timeout=5)


def _http_request(
    *, port: int, method: str, path: str,
    headers: Optional[dict] = None,
    body: bytes = b"",
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    return conn.getresponse()


# ---------------------------------------------------------------------------
# Basic translation.
# ---------------------------------------------------------------------------


def test_gateway_translates_get_to_fetch(gateway_server) -> None:
    """HTTP GET /products with X-Agent-Id reaches the daemon as
    AGTP FETCH /products (via the default alias seed). Since no
    handler is registered for /products on this fixture, the
    daemon returns 405 ``method-not-implemented`` (FETCH is in
    the catalog but unregistered)."""
    gw, state, port = gateway_server
    resp = _http_request(
        port=port, method="GET", path="/products",
        headers={"X-Agent-Id": "a" * 64},
    )
    # FETCH is in the catalog but no handler is registered → 405.
    assert resp.status == 405
    body = json.loads(resp.read())
    assert body["error"]["code"] == "method-not-implemented"
    assert body["error"]["method"] == "FETCH"


def test_gateway_returns_401_without_agent_id(gateway_server) -> None:
    """No X-Agent-Id header and no pinned agent → 401."""
    gw, state, port = gateway_server
    resp = _http_request(
        port=port, method="GET", path="/products",
    )
    assert resp.status == 401
    body = json.loads(resp.read())
    assert body["error"]["code"] == "missing-agent-id"


def test_gateway_returns_404_for_unknown_agent(gateway_server) -> None:
    gw, state, port = gateway_server
    resp = _http_request(
        port=port, method="GET", path="/products",
        headers={"X-Agent-Id": "z" * 64},  # not in registry
    )
    assert resp.status == 404
    body = json.loads(resp.read())
    assert body["error"]["code"] == "agent-not-found"


def test_gateway_serves_embedded_method_through_alias() -> None:
    """A built-in method (DISCOVER) reachable through HTTP GET via
    alias resolution. Same agent_id as the fixture; the daemon
    serves the DISCOVER / index successfully."""
    # Build a setup where GET resolves to DISCOVER (operator override
    # for this test) so we can hit a real handler end-to-end.
    port = _ephemeral_port()
    state = _state()
    state.methods_policy.aliases = {"GET": "DISCOVER"}
    gw = HttpGatewayServer(
        host="127.0.0.1", port=port,
        server_state=state, pinned_agent_id="",
    )
    thread = threading.Thread(target=gw.serve_forever, daemon=True)
    thread.start()
    try:
        resp = _http_request(
            port=port, method="GET", path="/",
            headers={"X-Agent-Id": "a" * 64},
        )
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["target"] == "index"
    finally:
        gw.shutdown()
        thread.join(timeout=5)


def test_gateway_strips_allow_rcns_header() -> None:
    """A REST caller sending Allow-RCNS gets no negotiation — the
    header is dropped before dispatch. We verify by enabling RCNS
    on the server config and sending an unregistered (method,
    path) with Allow-RCNS. The expected outcome is 404 (the gate
    fell through), not 461."""
    port = _ephemeral_port()

    # Build a state where RCNS is enabled and an unregistered path
    # exists in the endpoint registry's view.
    config = _config()
    config.rcns = RcnsConfig(enabled=True, min_trust_tier=3)
    config.policy.methods.aliases = {"GET": "DISCOVER"}

    doc = AgentDocument(
        agtp_version="1.0", agent_id="a" * 64, name="lauren",
        principal="Chris", principal_id="chris", description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["DISCOVER"], wildcards=True,
            scopes=["rcns:negotiate"],
        ),
        scopes_accepted=[], issued_at="now", issuer="self",
        trust_tier=1,
    )
    from server.endpoint_registry import EndpointRegistry
    from server.synthesis.runtime import SynthesisRuntime

    state = MagicMock()
    state.config = config
    state.methods_policy = config.policy.methods
    state.synthesis_runtime = SynthesisRuntime()
    state.endpoint_registry = EndpointRegistry()
    state.list_ids = lambda: [doc.agent_id]
    state.lookup = lambda aid: doc if aid.lower() == doc.agent_id.lower() else None
    state.lookup_genesis = lambda _aid: None
    state.signing_service = None

    gw = HttpGatewayServer(
        host="127.0.0.1", port=port,
        server_state=state, pinned_agent_id="",
    )
    thread = threading.Thread(target=gw.serve_forever, daemon=True)
    thread.start()
    try:
        resp = _http_request(
            port=port, method="GET", path="/something",
            headers={
                "X-Agent-Id": "a" * 64,
                "Allow-RCNS": "true",  # should be stripped
            },
        )
        # 200 (the DISCOVER / handler renders an index even for
        # unknown DISCOVER paths via the discover-unknown-path branch
        # — actually the daemon returns 460 for unknown DISCOVER
        # paths) OR the dispatcher's 404 / 460 with no RCNS-Attempt-Id.
        # Either way, **no** 461 — the header was stripped.
        assert resp.status != 461
        # No RCNS-Attempt-Id header on the response either.
        assert resp.getheader("RCNS-Attempt-Id") is None
    finally:
        gw.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Pinned Agent-Id.
# ---------------------------------------------------------------------------


def test_pinned_agent_id_serves_requests_without_header() -> None:
    port = _ephemeral_port()
    state = _state()
    state.methods_policy.aliases = {"GET": "DISCOVER"}
    gw = HttpGatewayServer(
        host="127.0.0.1", port=port,
        server_state=state, pinned_agent_id="a" * 64,
    )
    thread = threading.Thread(target=gw.serve_forever, daemon=True)
    thread.start()
    try:
        resp = _http_request(port=port, method="GET", path="/")
        # No X-Agent-Id header; pinned id used; 200 from DISCOVER /.
        assert resp.status == 200
    finally:
        gw.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Header passthrough.
# ---------------------------------------------------------------------------


def test_gateway_forwards_request_id_header(gateway_server) -> None:
    """X-Request-Id → Request-ID. The daemon echoes Request-ID back
    on the response so we can verify the forwarding."""
    gw, state, port = gateway_server
    state.methods_policy.aliases = {"GET": "DISCOVER"}
    resp = _http_request(
        port=port, method="GET", path="/",
        headers={
            "X-Agent-Id": "a" * 64,
            "X-Request-Id": "req-test-123",
        },
    )
    assert resp.status == 200
    # The daemon's _finalize_response echoes Request-ID on the wire
    # response; the gateway passes outbound headers through.
    assert resp.getheader("Request-ID") == "req-test-123"


# ---------------------------------------------------------------------------
# Body forwarding.
# ---------------------------------------------------------------------------


def test_gateway_forwards_post_body() -> None:
    """A POST body reaches the AGTP handler via Content-Length +
    rfile.read. Verified by sending QUERY (the aliased target of
    POST under operator override) with an intent that the handler
    echoes back."""
    port = _ephemeral_port()
    state = _state()
    state.methods_policy.aliases = {"POST": "QUERY"}
    gw = HttpGatewayServer(
        host="127.0.0.1", port=port,
        server_state=state, pinned_agent_id="",
    )
    thread = threading.Thread(target=gw.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"intent": "ping"}).encode("utf-8")
        resp = _http_request(
            port=port, method="POST", path="/",
            headers={
                "X-Agent-Id": "a" * 64,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        # QUERY at the bare path returns 200 from the embedded
        # handler; the daemon echoes intent in the body.
        assert resp.status == 200
        out = json.loads(resp.read())
        assert out.get("intent") == "ping"
    finally:
        gw.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Server lifecycle.
# ---------------------------------------------------------------------------


def test_gateway_server_address_is_loopback() -> None:
    """Default bind host is 127.0.0.1 — refusing remote connections
    unless the operator overrides via AGTP_HTTP_GATEWAY_HOST."""
    port = _ephemeral_port()
    state = _state()
    gw = HttpGatewayServer(
        host="127.0.0.1", port=port,
        server_state=state, pinned_agent_id="",
    )
    assert gw.server_address[0] == "127.0.0.1"
    assert gw.server_address[1] == port
    gw.shutdown()


def test_gateway_install_skips_when_disabled(monkeypatch) -> None:
    """AGTP_HTTP_GATEWAY_ENABLED=0 short-circuits install() so the
    listener never starts — useful when an operator wants to load
    the module for tests without binding a port."""
    monkeypatch.setenv("AGTP_HTTP_GATEWAY_ENABLED", "0")
    from operational import mod_http_gateway
    state = MagicMock()
    state.http_gateway = None
    mod_http_gateway.install(state)
    # No gateway attached.
    assert getattr(state, "http_gateway", None) is None
