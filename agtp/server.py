"""
AGTP Agent Server.

Hosts one or more Agent Documents and serves them over AGTP on port 4480.
TLS 1.3 is mandatory in production deployments per draft §5; the --cert
and --key flags enable it. For local development, --insecure permits
plaintext.

Method dispatch is table-driven via `agtp.methods.REGISTRY`. Adding a
method means adding a decorator in methods.py; the server itself stays
unchanged.

Run:
  python -m agtp.server --insecure --port 4480 --agents-dir agents/
  python -m agtp.server --port 4480 --cert cert.pem --key key.pem
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from agtp import wire
from agtp.identity import AgentDocument, from_dict
from agtp.methods import REGISTRY, dispatch, error_response


DEFAULT_PORT = 4480


class AgentRegistry:
    """In-memory map of agent_id -> AgentDocument, loaded from disk."""

    def __init__(self, agents_dir: Path):
        self.agents_dir = Path(agents_dir)
        self.agents: Dict[str, AgentDocument] = {}
        self._load()

    def _load(self) -> None:
        if not self.agents_dir.exists():
            return
        for json_path in sorted(self.agents_dir.glob("*.agent.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                doc = from_dict(data)
                self.agents[doc.agent_id] = doc
                print(f"[server] loaded {doc.name} ({doc.agent_id[:12]}...)")
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[server] skipping {json_path}: {exc}", file=sys.stderr)

    def lookup(self, agent_id: str) -> Optional[AgentDocument]:
        return self.agents.get(agent_id)

    def list_ids(self) -> List[str]:
        return list(self.agents.keys())


def _select_target(
    request: wire.AGTPRequest, registry: AgentRegistry
) -> tuple[Optional[AgentDocument], Optional[wire.AGTPResponse]]:
    """
    Resolve which AgentDocument the request is addressing.

    Returns (doc, None) on success or (None, error_response) on failure.
    Target-Agent header is honored. With no header, a single-agent server
    selects its sole agent for caller convenience.
    """
    target = wire.header(request, "Target-Agent")
    if not target:
        ids = registry.list_ids()
        if len(ids) == 1:
            return registry.lookup(ids[0]), None
        return None, error_response(
            400,
            "Bad Request",
            "missing-target-agent",
            "Target-Agent header required when server hosts multiple agents",
        )

    doc = registry.lookup(target)
    if doc is None:
        return None, error_response(
            404,
            "Not Found",
            "agent-not-found",
            f"no agent with id {target} on this server",
        )
    return doc, None


def handle_connection(conn, registry: AgentRegistry) -> None:
    """Handle a single AGTP connection: read one request, write one response."""
    try:
        reader = conn.makefile("rb")
        request = wire.parse_request(reader)

        agent_doc, target_err = _select_target(request, registry)
        if target_err is not None:
            conn.sendall(target_err.serialize())
            return

        assert agent_doc is not None
        response = dispatch(request, registry, agent_doc)
        conn.sendall(response.serialize())
    except wire.WireFormatError as exc:
        try:
            conn.sendall(
                error_response(
                    400, "Bad Request", "invalid-wire-format", str(exc)
                ).serialize()
            )
        except OSError:
            pass
    except OSError:
        pass
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def run(
    host: str,
    port: int,
    agents_dir: Path,
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
) -> None:
    registry = AgentRegistry(agents_dir)
    if not registry.agents:
        print(
            f"[server] WARNING: no agents loaded from {agents_dir}",
            file=sys.stderr,
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(64)

    if certfile and keyfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        sock = ctx.wrap_socket(sock, server_side=True)
        scheme = "agtps"
    else:
        scheme = "agtp"

    print(f"[server] listening on {scheme}://{host}:{port}")
    print(f"[server] agents: {len(registry.agents)} loaded")
    print(f"[server] methods: {len(REGISTRY)} registered ({', '.join(sorted(REGISTRY))})")
    for agent_id, doc in registry.agents.items():
        print(f"[server]   {doc.name}: agtp://{agent_id}")

    try:
        while True:
            try:
                conn, _ = sock.accept()
            except ssl.SSLError as exc:
                print(f"[server] TLS handshake failed: {exc}", file=sys.stderr)
                continue
            t = threading.Thread(
                target=handle_connection, args=(conn, registry), daemon=True
            )
            t.start()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agtp-server", description="AGTP Agent Server"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--agents-dir",
        default="agents",
        help="Directory containing *.agent.json files",
    )
    parser.add_argument("--cert", help="TLS certificate file")
    parser.add_argument("--key", help="TLS private key file")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Run plaintext (development only)",
    )
    args = parser.parse_args()

    if not args.insecure and not (args.cert and args.key):
        print(
            "[server] TLS required in production; pass --cert and --key, "
            "or --insecure for development",
            file=sys.stderr,
        )
        return 2

    run(args.host, args.port, Path(args.agents_dir), args.cert, args.key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
