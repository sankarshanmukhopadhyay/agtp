"""
Smoke tests for the elemen browser bridge.

The JS UI is not unit-tested here. What we exercise is the Python
surface that the UI calls into: discover_methods and invoke_method.
A passing run gives us confidence that auto-DISCOVER and the Try-it
form will work end-to-end against a real server.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Make `import agtp` work from the repo root.
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# elemen is a sibling directory, not a package; load its client module
# explicitly so we exercise the same code path the elemen app uses.
ELEMEN_DIR = REPO_ROOT / "elemen"
sys.path.insert(0, str(ELEMEN_DIR))

# Set AGTP_LIB_PATH so the elemen client locates the package even if
# the layout shifts. The package is at REPO_ROOT/agtp; v1/ has the shims.
os.environ.setdefault("AGTP_LIB_PATH", str(REPO_ROOT / "v1"))

import client as elemen_client  # noqa: E402  -- elemen/client.py
from agtp.server import AgentRegistry, handle_connection  # noqa: E402


LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


class _Server:
    """In-process AGTP server bound to a free port for tests."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try: self.sock.close()
        except OSError: pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=handle_connection, args=(conn, self.registry), daemon=True
            ).start()


class ElemenBridgeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents = Path(cls.tmp.name)
        src = REPO_ROOT / "v1" / "server" / "agents"
        for f in ("lauren.agent.json", "orchestrator.agent.json"):
            (agents / f).write_text(
                (src / f).read_text(encoding="utf-8"), encoding="utf-8"
            )
        cls.server = _Server(AgentRegistry(agents))
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def _uri(self, agent_id: str) -> str:
        return f"agtp://{agent_id}@{self.server.host}:{self.server.port}"

    # ---- discover_methods ----

    def test_discover_returns_bucketed_shape(self):
        result = elemen_client.discover_methods(
            self._uri(LAUREN_ID), insecure=True, insecure_skip_verify=True
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("embedded", result)
        self.assertIn("custom", result)
        self.assertEqual(result["custom"], [])
        self.assertEqual(
            {e["name"] for e in result["embedded"]},
            {"QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE",
             "PLAN", "EXECUTE", "CONFIRM", "NOTIFY"},
        )
        self.assertEqual(result["summary"]["embedded_count"], 8)

    def test_discover_against_orchestrator_lists_twelve(self):
        result = elemen_client.discover_methods(
            self._uri(ORCH_ID), insecure=True, insecure_skip_verify=True
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["embedded_count"], 12)

    # ---- invoke_method ----

    def test_invoke_query_returns_200(self):
        result = elemen_client.invoke_method(
            self._uri(LAUREN_ID),
            "QUERY",
            {"intent": "what is the weather"},
            insecure=True,
            insecure_skip_verify=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["method"], "QUERY")

    def test_invoke_unsupported_method_returns_405(self):
        result = elemen_client.invoke_method(
            self._uri(LAUREN_ID),
            "DELEGATE",
            {"task": "x", "sub_agent": ORCH_ID},
            insecure=True,
            insecure_skip_verify=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 405)

    def test_invoke_propagates_404_when_agent_missing(self):
        bogus = "0" * 64
        result = elemen_client.invoke_method(
            f"agtp://{bogus}@{self.server.host}:{self.server.port}",
            "QUERY",
            {"intent": "hi"},
            insecure=True,
            insecure_skip_verify=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 404)


if __name__ == "__main__":
    unittest.main()
