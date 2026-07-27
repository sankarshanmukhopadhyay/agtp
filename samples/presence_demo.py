"""
AGTP-Presence M1 demo — the Appendix B script, runnable.

Proves the announce -> converge -> discover -> route loop end to end with
**zero agent-held routable addresses**: two relay-mediated agents are
hosted on a coordinator, announced at boot, discovered by capability, and
reached by Agent-ID. No IP or port for either agent appears anywhere in
the client-visible output.

Run::

    python -m samples.presence_demo

Exit code 0 means every Appendix B assertion held. The transcript is
printed to stdout; redirect it to capture alongside the other demo
outputs::

    python -m samples.presence_demo > samples/presence_demo.transcript.txt

This boots an in-process coordinator on loopback so the demo is a single
command. The equivalent two-terminal form is::

    python -m server --presence --insecure --agents-dir <dir-with-2-agents>
    # then drive it with presence.client / the CLI
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from client.core_client import send_method
from core.identity import AgentDocument, RequiresDeclaration
from presence import client as pclient
from presence.store import PresenceStore
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from server.main import AgentRegistry, handle_connection


SUMMARIZER_ID = "a" * 64
AUDITOR_ID = "b" * 64


def _doc(agent_id: str, name: str, methods) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name=name, principal="Chris",
        principal_id="chris", description="", status="active", skills=[name],
        requires=RequiresDeclaration(methods=methods), scopes_accepted=[],
        issued_at="now", issuer="self", trust_tier=1,
    )


class _Coordinator:
    def __init__(self, docs):
        self._tmp = TemporaryDirectory()
        self.registry = AgentRegistry(Path(self._tmp.name))
        for d in docs:
            self.registry.agents[d.agent_id] = d
        self.config = ServerConfig(
            server=ServerInfo(server_id="coord.local", operator="demo", contact=""),
            policy=ServerPolicy(wildcards_accepted=True),
            agents=AgentsConfig(disclosure="public"),
        )
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        # Coordinator mode: attach store + self-announce hosted agents.
        self.registry.presence_store = PresenceStore()
        self.registry.presence_relay_endpoint = f"{self.host}:{self.port}"
        for d in self.registry.agents.values():
            rec = self.registry.presence_store.build_record(
                d, relay_endpoint=self.registry.presence_relay_endpoint
            )
            self.registry.presence_store.announce(rec)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        time.sleep(0.05)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._tmp.cleanup()

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


def _show(title, obj):
    print(f"\n--- {title} ---")
    print(json.dumps(obj, indent=2))


def main() -> int:
    failures = []

    def check(label, ok):
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M1 demo (Appendix B)")
    print("=" * 68)

    # Step 1-2: start coordinator hosting two relay-mediated agents; both
    # are ANNOUNCEd at boot ("showing up is the registration").
    coord = _Coordinator([
        _doc(SUMMARIZER_ID, "summarizer", ["QUERY", "SUMMARIZE", "DESCRIBE"]),
        _doc(AUDITOR_ID, "auditor", ["VALIDATE", "REPORT", "DESCRIBE"]),
    ])
    coord.start()
    print(f"\ncoordinator listening on 127.0.0.1:{coord.port}")
    print("hosted (relay-mediated) agents: summarizer, auditor")
    print("both self-announced at boot - no directory query issued")

    try:
        # Step 3: DISCOVER /population by capability. Assert exactly one
        # result; it is an Agent-ID + manifest; zero IP addresses.
        body = pclient.discover_population_json(
            coord.host, coord.port, capability="validate", use_tls=False
        )
        _show("DISCOVER /population?capability=validate", body)
        check("exactly one result for capability=validate",
              body.get("total_matches") == 1)
        result = (body.get("results") or [{}])[0]
        check("result is the auditor Agent-ID",
              result.get("agent_id") == AUDITOR_ID)
        check("result carries a manifest URI (agtp://<agent-id>)",
              result.get("manifest_uri") == f"agtp://{AUDITOR_ID}")
        blob = json.dumps(body)
        check("zero IP/port literals in the discovery payload",
              coord.host not in blob and str(coord.port) not in blob
              and "attachment" not in blob)

        # Step 4: PROBE the discovered agent. Assert present.
        probe = pclient.probe_json(
            coord.host, coord.port, AUDITOR_ID, use_tls=False
        )
        _show("PROBE <auditor>", probe)
        check("PROBE reports the auditor present", probe.get("present") is True)

        # Step 5: DESCRIBE routed by Agent-ID (resolution + delivery via
        # the coordinator/relay). Assert the agent document comes back.
        resp = send_method(
            AUDITOR_ID, coord.host, coord.port, "DESCRIBE", use_tls=False
        )
        doc = json.loads(resp.body_bytes.decode("utf-8"))
        _show("DESCRIBE routed by <auditor> Agent-ID", {
            "status": resp.status_code,
            "agent_id": doc.get("agent_id"),
            "name": doc.get("name"),
        })
        check("DESCRIBE by Agent-ID returns the agent document",
              resp.status_code == 200 and doc.get("agent_id") == AUDITOR_ID)

        # Step 6: assert across the transcript that no agent-held network
        # literal appeared in any client-visible discovery output. (The
        # only network literal the client knows is the coordinator's own
        # endpoint, which it dialed directly — never an agent's.)
        transcript = json.dumps(body) + json.dumps(probe)
        check("no agent-held IP/port in discovery/probe output",
              coord.host not in transcript and str(coord.port) not in transcript)
    finally:
        coord.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED — {len(failures)} assertion(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED - announce/discover/route loop held, zero agent addresses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
