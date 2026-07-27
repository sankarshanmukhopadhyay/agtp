"""
In-process coordinator harness for demos and tests.

:class:`InProcessCoordinator` boots a real AGTP server in coordinator mode
on a loopback ephemeral port, on its own daemon thread, hosting a set of
agents that are self-announced at start (exactly as ``run(presence=True)``
does). Optionally it gossips with peer coordinators.

This is deliberately shipped in the package rather than duplicated across
test files and demo scripts — spinning up a coordinator to talk to over
the wire is a common need for anyone embedding presence.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, List, Optional

from core.identity import AgentDocument, RequiresDeclaration
from presence.store import PresenceStore
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from server.main import AgentRegistry, handle_connection


def make_doc(
    agent_id: str,
    name: str,
    methods: Iterable[str],
    *,
    tier: int = 1,
    owner_id: str = "",
) -> AgentDocument:
    """Build a minimal, transport-only AgentDocument for a coordinator to host."""
    return AgentDocument(
        agtp_version="1.0",
        agent_id=agent_id,
        name=name,
        principal="Chris",
        principal_id="chris",
        description="",
        status="active",
        skills=[name],
        requires=RequiresDeclaration(methods=list(methods)),
        scopes_accepted=[],
        issued_at="now",
        issuer="self",
        trust_tier=tier,
        owner_id=owner_id,
    )


class InProcessCoordinator:
    """A loopback AGTP coordinator on its own thread."""

    def __init__(
        self,
        docs: Iterable[AgentDocument],
        *,
        peers: Optional[List[str]] = None,
        wildcards_accepted: bool = True,
        signing_service=None,
        require_discovery_scope: bool = False,
        verify_signatures: bool = False,
        ans: bool = False,
    ):
        self._tmp = TemporaryDirectory()
        self.registry = AgentRegistry(Path(self._tmp.name))
        for d in docs:
            self.registry.agents[d.agent_id] = d
        self.registry.signing_service = signing_service
        self.registry.presence_require_discovery_scope = require_discovery_scope
        self.registry.presence_verify_signatures = verify_signatures
        if ans:
            from ans.store import NameStore
            from ans.federation import FederationTrust
            self.registry.ans_store = NameStore()
            self.registry.federation_trust = FederationTrust()
            self.registry.federation_use_tls = False
        self.config = ServerConfig(
            server=ServerInfo(server_id="coord.local", operator="x", contact=""),
            policy=ServerPolicy(wildcards_accepted=wildcards_accepted),
            agents=AgentsConfig(disclosure="public"),
        )

        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)

        # Coordinator mode: attach the store, self-announce hosted agents.
        self.registry.presence_store = PresenceStore()
        self.registry.presence_relay_endpoint = f"{self.host}:{self.port}"
        self.registry.presence_peers = list(peers or [])
        # DHT node (by-ID routing overlay), id derived from the endpoint.
        import hashlib
        from dht.kademlia import KademliaNode
        from presence.rendezvous import RendezvousIndex
        node_id = hashlib.sha256(f"{self.host}:{self.port}".encode()).hexdigest()
        self.registry.dht_node = KademliaNode(node_id, self.host, self.port)
        self.registry.rendezvous_index = RendezvousIndex()
        for d in self.registry.agents.values():
            rec = self.registry.presence_store.build_record(
                d, relay_endpoint=self.registry.presence_relay_endpoint
            )
            if signing_service is not None:
                from presence.recordsig import sign_record
                sign_record(rec, signing_service)
            self.registry.presence_store.announce(rec)

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @property
    def store(self) -> PresenceStore:
        return self.registry.presence_store

    @property
    def dht_node(self):
        return self.registry.dht_node

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> "InProcessCoordinator":
        self._thread.start()
        # Wait until the accept loop is actually serving, so a client
        # connecting immediately after start() never races startup under
        # concurrent load. The listener is already bound+listening; this
        # confirms a request round-trips.
        for _ in range(200):
            try:
                socket.create_connection((self.host, self.port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.01)
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._tmp.cleanup()

    def _loop(self) -> None:
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

    def __enter__(self) -> "InProcessCoordinator":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
