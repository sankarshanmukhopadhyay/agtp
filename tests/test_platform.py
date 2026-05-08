"""
Cross-platform smoke tests run as subprocesses.

These exist to catch regressions where the package works in-process
(via the in-thread test server in test_methods.py) but breaks under
the actual `python -m client.*` invocation pipeline used by users and
the demo script.

Skips cleanly on hosts that lack required binaries.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

WINDOWS = platform.system() == "Windows"
MACOS = platform.system() == "Darwin"
LINUX = platform.system() == "Linux"

GIT_BASH = WINDOWS and "MINGW" in os.environ.get("MSYSTEM", "")
POWERSHELL = WINDOWS and not GIT_BASH

LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"


def require_python() -> None:
    if not PYTHON:
        raise unittest.SkipTest("no python interpreter available")


def require_curl() -> None:
    if shutil.which("curl") is None:
        raise unittest.SkipTest("curl binary not on PATH")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(host: str, port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _stage_lauren(agents_dir: Path) -> None:
    src = REPO_ROOT / "server" / "agents" / "lauren.agent.json"
    (agents_dir / "lauren.agent.json").write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-m", *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class FullStackSubprocessTests(unittest.TestCase):
    """
    End-to-end: spawn registry + server, hit them with the client and
    with agtp-curl, then tear everything down. Runs in under 30 seconds.
    """

    @classmethod
    def setUpClass(cls) -> None:
        require_python()

        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_lauren(agents_dir)

        cls.reg_port = _free_port()
        cls.srv_port = _free_port()
        cls.store = agents_dir / "registry_data.json"

        cls.reg = _spawn([
            "registry",
            str(cls.reg_port),
            "--host", "127.0.0.1",
            "--store", str(cls.store),
        ])
        if not _wait_listening("127.0.0.1", cls.reg_port, timeout=5.0):
            _terminate(cls.reg)
            raise RuntimeError("registry did not come up")

        # Pre-register Lauren so the client's bare-ID lookup resolves.
        from registry.main import RegistryStore
        RegistryStore(cls.store).register(
            LAUREN_ID, "127.0.0.1", cls.srv_port
        )

        cls.srv = _spawn([
            "server",
            str(cls.srv_port),
            "--host", "127.0.0.1",
            "--agents-dir", str(agents_dir),
        ])
        if not _wait_listening("127.0.0.1", cls.srv_port, timeout=5.0):
            _terminate(cls.srv)
            _terminate(cls.reg)
            raise RuntimeError("server did not come up")

    @classmethod
    def tearDownClass(cls) -> None:
        _terminate(cls.srv)
        _terminate(cls.reg)
        cls.tmp.cleanup()

    def _run(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-m", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_client_describes_via_registry(self) -> None:
        out = self._run(
            "client",
            f"agtp://{LAUREN_ID}",
            "--registry", f"http://127.0.0.1:{self.reg_port}",
            "--insecure",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["agent_id"], LAUREN_ID)

    def test_client_query_via_direct_form(self) -> None:
        out = self._run(
            "client",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.srv_port}",
            "QUERY",
            "--param", "intent=hello",
            "--insecure",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["method"], "QUERY")
        self.assertEqual(payload["intent"], "hello")

    def test_curl_discovers_methods(self) -> None:
        out = self._run(
            "client.cli.curl",
            "DISCOVER",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.srv_port}",
            "-d", '{"target":"methods"}',
            "--insecure",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertIn("embedded", payload)
        self.assertGreaterEqual(payload["summary"]["embedded_count"], 1)

    def test_unknown_method_returns_501(self) -> None:
        out = self._run(
            "client",
            f"agtp://{LAUREN_ID}@127.0.0.1:{self.srv_port}",
            "FAKEMETHOD",
            "--param", "x=1",
            "--insecure",
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("method-not-implemented", out.stdout)


class PlatformDetectionTests(unittest.TestCase):
    """Sanity checks on the platform-detection helpers themselves."""

    def test_exactly_one_os_flag_is_true(self) -> None:
        self.assertEqual(sum([WINDOWS, MACOS, LINUX]), 1)

    def test_git_bash_implies_windows(self) -> None:
        if GIT_BASH:
            self.assertTrue(WINDOWS)

    def test_powershell_implies_windows_and_not_git_bash(self) -> None:
        if POWERSHELL:
            self.assertTrue(WINDOWS)
            self.assertFalse(GIT_BASH)


if __name__ == "__main__":
    unittest.main()
