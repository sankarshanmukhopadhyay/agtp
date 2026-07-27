"""
AGTP-Presence M2 demo — the visibility model, runnable.

One subject agent, two viewers. The subject declares a tier-scoped
posture; a high-trust viewer sees it, a low-trust viewer cannot, and the
low-trust viewer's PROBE returns a 404 byte-indistinguishable from "no
such agent". Then the subject goes invisible and vanishes from discovery
for everyone.

Run::

    python -m samples.presence_visibility_demo

Exit 0 means every M2 visibility assertion held.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from core.identity import AgentDocument, RequiresDeclaration
from presence import client as pclient
from presence.store import PresenceStore
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from server.main import AgentRegistry, handle_connection


SUBJECT = "a" * 64
VIEWER_HI = "1" * 64   # tier 1 — as trusted as the subject
VIEWER_LO = "2" * 64   # tier 3 — less trusted


def _doc(agent_id, name, methods, *, tier, owner_id=""):
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name=name, principal="Chris",
        principal_id="chris", description="", status="active", skills=[name],
        requires=RequiresDeclaration(methods=methods), scopes_accepted=[],
        issued_at="now", issuer="self", trust_tier=tier, owner_id=owner_id,
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
        self.registry.presence_store = PresenceStore()
        self.registry.presence_relay_endpoint = f"{self.host}:{self.port}"
        for d in self.registry.agents.values():
            self.registry.presence_store.announce(
                self.registry.presence_store.build_record(
                    d, relay_endpoint=self.registry.presence_relay_endpoint
                )
            )
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


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M2 demo - visibility model")
    print("=" * 68)

    coord = _Coordinator([
        _doc(SUBJECT, "subject", ["VALIDATE", "DESCRIBE"], tier=1, owner_id="acme.tld"),
        _doc(VIEWER_HI, "viewer-hi", ["QUERY"], tier=1, owner_id="acme.tld"),
        _doc(VIEWER_LO, "viewer-lo", ["QUERY"], tier=3, owner_id="other.tld"),
    ])
    coord.start()
    host, port = coord.host, coord.port

    def pop(as_agent):
        return pclient.discover_population_json(
            host, port, capability="validate", as_agent=as_agent, use_tls=False
        )

    try:
        # 1. Subject declares a tier-scoped posture.
        pclient.announce(
            host, port, SUBJECT,
            visibility={"presence_mode": "tier-scoped", "disclosure_mode": "capabilities"},
            use_tls=False,
        )
        print("\nsubject announced tier-scoped (trust tier 1)")

        hi = pop(VIEWER_HI)
        lo = pop(VIEWER_LO)
        print(f"  high-trust viewer (tier 1) sees {hi['total_matches']} match(es)")
        print(f"  low-trust  viewer (tier 3) sees {lo['total_matches']} match(es)")
        check("tier-1 viewer sees the tier-scoped subject", hi["total_matches"] == 1)
        check("tier-3 viewer does NOT see it", lo["total_matches"] == 0)

        # 2. PROBE from the low-trust viewer is a 404 indistinguishable
        #    from a nonexistent agent.
        seen = pclient.probe(host, port, SUBJECT, as_agent=VIEWER_HI, use_tls=False)
        hidden = pclient.probe(host, port, SUBJECT, as_agent=VIEWER_LO, use_tls=False)
        print(f"\n  PROBE by high-trust: {seen.status_code}  "
              f"PROBE by low-trust: {hidden.status_code}")
        check("high-trust PROBE finds the subject (200)", seen.status_code == 200)
        check("low-trust PROBE is denied as 404 (looks nonexistent)",
              hidden.status_code == 404)

        # Prove indistinguishability: withdraw, then a truly-absent probe
        # of the same id yields byte-identical output.
        pclient.withdraw(host, port, SUBJECT, use_tls=False)
        absent = pclient.probe(host, port, SUBJECT, as_agent=VIEWER_LO, use_tls=False)
        check("out-of-scope 404 is byte-identical to nonexistent 404",
              hidden.body_bytes == absent.body_bytes)

        # 3. Re-announce invisible: vanishes for everyone, including tier 1.
        pclient.announce(
            host, port, SUBJECT,
            visibility={"presence_mode": "invisible", "disclosure_mode": "existence-only"},
            use_tls=False,
        )
        check("invisible subject is absent even for a tier-1 viewer",
              pop(VIEWER_HI)["total_matches"] == 0)
    finally:
        coord.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - visibility scoping and PROBE-404 indistinguishability held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
