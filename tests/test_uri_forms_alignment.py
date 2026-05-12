"""
§11 URI forms tests.

Covers:

  * Parser recognizes all six §11 forms (1, 1a, 2, 2a, 3, 4) with
    correct ``form`` property values.
  * ``ParsedURI.agent_handle`` populated for Forms 3 / 4.
  * Form 3 vs Form 4 distinguished by ``agtp.`` host prefix.
  * ``format_uri`` constructs each form without ever emitting port.
  * Server-side path-based resolution: ``/agents/{name}`` requests
    are routed to the named agent via ``registry.agents`` lookup.
  * Mismatch detection: ``Agent-ID`` header disagreeing with the
    path-resolved agent → 400 ``agent-identity-mismatch``.
  * Unknown handle → 404 ``agent-handle-not-found``.
  * Sub-paths under ``/agents/{name}/...`` rejected with a clear
    error message (Q4: reserved for future revisions).

Pre-§11 URI tests are in ``tests/test_manifest.py`` (URIFormTests).
This module adds the §11-specific coverage.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.ids import (
    AgentIDError, ParsedURI, format_uri, parse_uri,
)
from server.config import (
    AgentsConfig, AuditConfig, ServerConfig, ServerInfo, ServerPolicy,
)
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ===========================================================================
# Parser: each form recognized correctly.
# ===========================================================================


class FormRecognitionTests(unittest.TestCase):

    def test_form_1_bare_agent_id(self):
        p = parse_uri(f"agtp://{LAUREN_ID}")
        self.assertEqual(p.agent_id, LAUREN_ID)
        self.assertIsNone(p.agent_handle)
        self.assertIsNone(p.host)
        self.assertEqual(p.form, "1")
        self.assertFalse(p.is_server_level)
        self.assertFalse(p.is_domain_anchored)

    def test_form_1a_agent_id_with_host(self):
        p = parse_uri(f"agtp://{LAUREN_ID}@agents.acme.com")
        self.assertEqual(p.agent_id, LAUREN_ID)
        self.assertEqual(p.host, "agents.acme.com")
        self.assertIsNone(p.agent_handle)
        self.assertEqual(p.form, "1a")

    def test_form_2_specific_server(self):
        p = parse_uri("agtp://agents.acme.com")
        self.assertIsNone(p.agent_id)
        self.assertIsNone(p.agent_handle)
        self.assertEqual(p.host, "agents.acme.com")
        self.assertEqual(p.form, "2")
        self.assertTrue(p.is_server_level)

    def test_form_2a_bare_domain(self):
        # Form 2 and Form 2a are structurally identical. The parser
        # returns "2" for both; "2a" is a deployment-convention
        # label at the spec level.
        p = parse_uri("agtp://acme.com")
        self.assertIsNone(p.agent_id)
        self.assertIsNone(p.agent_handle)
        self.assertEqual(p.host, "acme.com")
        self.assertEqual(p.form, "2")
        self.assertTrue(p.is_server_level)

    def test_form_3_domain_anchored_agent(self):
        p = parse_uri("agtp://acme.com/agents/lauren")
        self.assertIsNone(p.agent_id)
        self.assertEqual(p.agent_handle, "lauren")
        self.assertEqual(p.host, "acme.com")
        self.assertEqual(p.form, "3")
        self.assertFalse(p.is_server_level)
        self.assertTrue(p.is_domain_anchored)

    def test_form_4_subdomain_anchored_agent(self):
        # ``agtp.`` host prefix is the Form 4 distinguisher.
        p = parse_uri("agtp://agtp.acme.com/agents/lauren")
        self.assertEqual(p.agent_handle, "lauren")
        self.assertEqual(p.host, "agtp.acme.com")
        self.assertEqual(p.form, "4")
        self.assertTrue(p.is_domain_anchored)

    def test_handle_with_dots_and_dashes(self):
        # Handles allow lowercase, digits, dots, dashes, underscores.
        p = parse_uri("agtp://acme.com/agents/lauren-v2")
        self.assertEqual(p.agent_handle, "lauren-v2")
        p2 = parse_uri("agtp://acme.com/agents/team.lauren")
        self.assertEqual(p2.agent_handle, "team.lauren")
        p3 = parse_uri("agtp://acme.com/agents/lauren_v3")
        self.assertEqual(p3.agent_handle, "lauren_v3")


# ===========================================================================
# Sub-paths reserved (§11 Q4).
# ===========================================================================


class SubPathReservedTests(unittest.TestCase):

    def test_sub_path_under_agents_rejected(self):
        with self.assertRaises(AgentIDError) as ctx:
            parse_uri("agtp://acme.com/agents/lauren/calendar")
        # The error message points the operator at the right concern.
        self.assertIn("reserved", str(ctx.exception).lower())

    def test_deeply_nested_sub_path_rejected(self):
        with self.assertRaises(AgentIDError):
            parse_uri("agtp://acme.com/agents/lauren/calendar/2026")

    def test_form_3_exact_match_still_works(self):
        # Sanity: the reject doesn't catch the legitimate Form 3.
        p = parse_uri("agtp://acme.com/agents/lauren")
        self.assertEqual(p.form, "3")


# ===========================================================================
# Port handling (§11: no port in canonical URI).
# ===========================================================================


class PortHandlingTests(unittest.TestCase):

    def test_parser_accepts_port_for_dev_test(self):
        # The grammar accepts :port for test fixtures and dev
        # convenience; the canonical wire form omits it.
        p = parse_uri("agtp://127.0.0.1:8443")
        self.assertEqual(p.port, 8443)
        self.assertEqual(p.host, "127.0.0.1")
        self.assertEqual(p.effective_port, 8443)

    def test_parser_default_port(self):
        p = parse_uri("agtp://acme.com")
        self.assertIsNone(p.port)
        self.assertEqual(p.effective_port, 4480)

    def test_format_uri_never_emits_port(self):
        # The canonical form drops port even when one is requested
        # (mirroring how HTTPS canonical URIs omit :443).
        self.assertEqual(
            format_uri(host="acme.com", port=8443),
            "agtp://acme.com",
        )
        self.assertEqual(
            format_uri(host="acme.com"),
            "agtp://acme.com",
        )
        # Form 1a is also port-less in canonical form.
        self.assertEqual(
            format_uri(agent_id=LAUREN_ID, host="acme.com", port=8443),
            f"agtp://{LAUREN_ID}@acme.com",
        )


# ===========================================================================
# format_uri: construct each form correctly.
# ===========================================================================


class FormatUriTests(unittest.TestCase):

    def test_format_form_1(self):
        self.assertEqual(
            format_uri(agent_id=LAUREN_ID), f"agtp://{LAUREN_ID}",
        )

    def test_format_form_1a(self):
        self.assertEqual(
            format_uri(agent_id=LAUREN_ID, host="acme.com"),
            f"agtp://{LAUREN_ID}@acme.com",
        )

    def test_format_form_2(self):
        self.assertEqual(
            format_uri(host="agents.acme.com"),
            "agtp://agents.acme.com",
        )

    def test_format_form_3(self):
        self.assertEqual(
            format_uri(host="acme.com", agent_handle="lauren"),
            "agtp://acme.com/agents/lauren",
        )

    def test_format_form_4(self):
        # ``agtp.`` subdomain is just a host-naming convention; the
        # caller passes the full host. format_uri doesn't enforce
        # the prefix.
        self.assertEqual(
            format_uri(host="agtp.acme.com", agent_handle="lauren"),
            "agtp://agtp.acme.com/agents/lauren",
        )

    def test_format_agent_handle_without_host_refused(self):
        with self.assertRaises(AgentIDError):
            format_uri(agent_handle="lauren")

    def test_format_invalid_handle_refused(self):
        with self.assertRaises(AgentIDError):
            format_uri(host="acme.com", agent_handle="invalid handle")

    def test_format_round_trips(self):
        # parse(format(x)) == x for each form.
        for uri in (
            f"agtp://{LAUREN_ID}",
            f"agtp://{LAUREN_ID}@acme.com",
            "agtp://agents.acme.com",
            "agtp://acme.com",
            "agtp://acme.com/agents/lauren",
            "agtp://agtp.acme.com/agents/lauren",
        ):
            with self.subTest(uri=uri):
                parsed = parse_uri(uri)
                rebuilt = format_uri(
                    agent_id=parsed.agent_id,
                    agent_handle=parsed.agent_handle,
                    host=parsed.host,
                )
                self.assertEqual(rebuilt, uri)


# ===========================================================================
# Server-side Form 3 / 4 path resolution end-to-end.
# ===========================================================================


def _stage_agents(agents_dir: Path) -> None:
    src = REPO_ROOT / "server" / "agents"
    for name in ("lauren.agent.json", "orchestrator.agent.json"):
        (agents_dir / name).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


class _Server:
    def __init__(self, registry, config):
        self.registry = registry
        self.config = config
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self): self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=handle_connection,
                args=(conn, self.registry, self.config),
                daemon=True,
            ).start()


def _send(server, method, path="/", *, headers=None, body=None):
    sock = socket.create_connection(
        (server.host, server.port), timeout=5.0,
    )
    body_bytes = (
        json.dumps(body).encode("utf-8") if body is not None else b""
    )
    h = {
        "Host": f"{server.host}:{server.port}",
        "Accept": "application/json",
        "Content-Length": str(len(body_bytes)),
    }
    if body_bytes:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = wire.AGTPRequest(
        method=method, headers=h, body_bytes=body_bytes, path=path,
    )
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        sock.close()


class ServerSideAgentsPathResolutionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(server_id="srv", operator="x", contact=""),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
            audit=AuditConfig(),
        )
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_agents_path_resolves_to_agent_by_name(self):
        # ``/agents/lauren`` resolves to the Lauren agent by the
        # AgentDocument.name field. No Agent-ID header needed.
        resp = _send(self.server, "DESCRIBE", path="/agents/lauren")
        self.assertEqual(resp.status_code, 200)

    def test_agents_path_lookup_is_case_insensitive(self):
        # AgentDocument.name on the staged Lauren fixture is "Lauren";
        # the lookup is case-insensitive so URI authors can write the
        # name in any case.
        resp = _send(self.server, "DESCRIBE", path="/agents/Lauren")
        self.assertEqual(resp.status_code, 200)

    def test_agents_path_unknown_handle_returns_404(self):
        resp = _send(self.server, "DESCRIBE", path="/agents/no-such-agent")
        self.assertEqual(resp.status_code, 404)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "agent-handle-not-found")
        self.assertEqual(body["error"]["handle"], "no-such-agent")

    def test_agents_path_agreeing_with_header_succeeds(self):
        # Path resolves to Lauren AND Agent-ID header is Lauren's
        # canonical hash → they agree, dispatch proceeds normally.
        resp = _send(
            self.server, "DESCRIBE",
            path="/agents/lauren",
            headers={"Agent-ID": LAUREN_ID},
        )
        self.assertEqual(resp.status_code, 200)

    def test_agents_path_disagreeing_with_header_returns_400(self):
        # Path resolves to Lauren but Agent-ID header names the
        # Orchestrator → conflict.
        resp = _send(
            self.server, "DESCRIBE",
            path="/agents/lauren",
            headers={"Agent-ID": ORCH_ID},
        )
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "agent-identity-mismatch")

    def test_form_3_response_carries_server_id(self):
        # §10 invariant: every response carries Server-ID. Verify the
        # path-resolution code path doesn't break it.
        resp = _send(self.server, "DESCRIBE", path="/agents/lauren")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(wire.header(resp, "Server-ID"), "srv")


if __name__ == "__main__":
    unittest.main()
