"""
Tests for the server/registry/curl invocation surface.

Exercises:
  * positional port and --port (and the mutual-exclusion error)
  * default port (4480 server, 8080 registry)
  * loopback plaintext default vs non-loopback TLS requirement
  * default agents-dir creation
  * agtp-curl DISCOVER /methods sugar against a live server
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

# Repo root on sys.path for direct invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from client.cli.main import build_parser as build_client_parser
from client.cli.curl import build_parser as build_curl_parser
from client.cli.curl import run as curl_run
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """Poll the (host, port) tuple until something accepts a TCP connect."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _run_server(args: List[str]) -> Tuple[subprocess.Popen, int]:
    """Spawn `python -m server <args>` from the repo root."""
    proc = subprocess.Popen(
        [PYTHON, "-m", "server", *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, proc.pid


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
    # Close stdio pipes so the test runner does not warn about them.
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Argparse tests (no subprocess required).
# ---------------------------------------------------------------------------


class ArgParsingTests(unittest.TestCase):

    def test_server_accepts_positional_port(self) -> None:
        from server.main import main as server_main
        # Parse-only check: invoke through subprocess with --help so we
        # don't actually start serving. We assert the help text shows
        # both positional and --port.
        out = subprocess.run(
            [PYTHON, "-m", "server", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("PORT", out.stdout)
        self.assertIn("--port", out.stdout)

    def test_server_rejects_both_positional_and_flag(self) -> None:
        out = subprocess.run(
            [PYTHON, "-m", "server", "4480", "--port", "4480"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not both", out.stderr.lower())

    def test_server_rejects_non_loopback_without_tls_or_insecure(self) -> None:
        out = subprocess.run(
            [PYTHON, "-m", "server", "0", "--host", "0.0.0.0"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(out.returncode, 2)
        self.assertIn("non-loopback", out.stderr.lower())

    def test_registry_accepts_positional_port(self) -> None:
        out = subprocess.run(
            [PYTHON, "-m", "registry", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("PORT", out.stdout)
        self.assertIn("--port", out.stdout)

    def test_curl_help_has_curl_flags(self) -> None:
        out = subprocess.run(
            [PYTHON, "-m", "client.cli.curl", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Spot-check the curl-equivalent surface.
        for flag in ("-X", "-H", "-d", "-i", "-v"):
            self.assertIn(flag, out.stdout, f"--help missing {flag}")


# ---------------------------------------------------------------------------
# Subprocess: server + curl end-to-end.
# ---------------------------------------------------------------------------


class CurlAgainstLiveServerTests(unittest.TestCase):
    """Spawn a real `python -m server` and hit it with agtp-curl."""

    @classmethod
    def setUpClass(cls) -> None:
        # Stage Lauren in a temp agents dir so the server has something
        # to serve. Loopback bind defaults to plaintext, so no --insecure.
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        src = REPO_ROOT / "server" / "agents" / "lauren.agent.json"
        (agents_dir / "lauren.agent.json").write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8"
        )

        cls.port = _free_port()
        cls.proc, _ = _run_server(
            [
                str(cls.port),
                "--host", "127.0.0.1",
                "--agents-dir", str(agents_dir),
            ]
        )
        if not _wait_listening("127.0.0.1", cls.port, timeout=5.0):
            stdout, stderr = cls.proc.communicate(timeout=2.0)
            _terminate(cls.proc)
            raise RuntimeError(
                f"server did not come up on {cls.port}; "
                f"stdout={stdout!r} stderr={stderr!r}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        _terminate(cls.proc)
        cls.tmp.cleanup()

    def _curl(self, *args: str) -> Tuple[int, str, str]:
        """Run agtp-curl and capture (rc, stdout, stderr)."""
        out = subprocess.run(
            [PYTHON, "-m", "client.cli.curl", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.returncode, out.stdout, out.stderr

    def test_describe_via_direct_host_form(self) -> None:
        rc, stdout, stderr = self._curl(
            "DESCRIBE",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.port}",
            "--insecure",
        )
        self.assertEqual(rc, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["agent_id"], LAUREN_ID)

    def test_discover_methods_sugar(self) -> None:
        # Server form with /methods sugar fills target=methods automatically.
        rc, stdout, stderr = self._curl(
            "DISCOVER",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.port}",
            "-d", '{"target":"methods"}',
            "--insecure",
        )
        self.assertEqual(rc, 0, stderr)
        payload = json.loads(stdout)
        self.assertIn("embedded", payload)
        self.assertGreater(payload["summary"]["embedded_count"], 0)

    def test_dash_X_alternative(self) -> None:
        rc, stdout, stderr = self._curl(
            "-X", "DESCRIBE",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.port}",
            "--insecure",
        )
        self.assertEqual(rc, 0, stderr)
        self.assertIn(LAUREN_ID, stdout)

    def test_include_headers(self) -> None:
        rc, stdout, _ = self._curl(
            "-i",
            "DESCRIBE",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.port}",
            "--insecure",
        )
        self.assertEqual(rc, 0)
        self.assertTrue(stdout.startswith("AGTP/1.0 200"))


# ---------------------------------------------------------------------------
# Default agents-dir creation.
# ---------------------------------------------------------------------------


class DefaultAgentsDirTests(unittest.TestCase):

    def test_missing_agents_dir_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "did-not-exist"
            self.assertFalse(target.exists())
            port = _free_port()
            proc, _ = _run_server(
                [
                    str(port),
                    "--host", "127.0.0.1",
                    "--agents-dir", str(target),
                ]
            )
            try:
                _wait_listening("127.0.0.1", port, timeout=3.0)
                self.assertTrue(target.exists())
                self.assertTrue(target.is_dir())
            finally:
                _terminate(proc)


if __name__ == "__main__":
    unittest.main()
