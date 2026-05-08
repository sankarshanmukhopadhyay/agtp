"""
AGTP Registry Server.

Runs over HTTPS. Resolves Agent IDs to {host, port} pairs so that bare
`agtp://{agent-id}` URIs can be looked up before connecting.

For v1 this is a single-instance, file-backed registry. v2 will replace
the file backend with a database and add a registration UI.

API:
  GET  /registry/{agent-id}        Resolve an agent ID to {host, port}
  GET  /registry/{agent-id}.json   Same, explicit format
  GET  /health                     Liveness probe

Responses are JSON. 200 on hit, 404 on miss.

Run:
  python -m registry --port 8080
  python -m registry --port 443 --cert cert.pem --key key.pem
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from core._paths import normalize
from core.ids import AgentIDError, validate_agent_id


REGISTRY_FILE_DEFAULT = "registry_data.json"
DEFAULT_PORT = 8080


class RegistryStore:
    """File-backed registry of agent ID -> {host, port}."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def lookup(self, agent_id: str) -> Optional[dict]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data.get(agent_id)

    def register(self, agent_id: str, host: str, port: int) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data[agent_id] = {"host": host, "port": port}
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_all(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class RegistryHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the registry."""

    store: RegistryStore  # injected on the server class

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            f"[registry] {self.address_string()} - {fmt % args}\n"
        )

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/health":
            self._json(200, {"status": "ok"})
            return

        if path.startswith("/registry/"):
            agent_id = path[len("/registry/") :]
            if agent_id.endswith(".json"):
                agent_id = agent_id[: -len(".json")]
            self._handle_lookup(agent_id)
            return

        self._json(404, {"error": "not-found", "detail": "no such endpoint"})

    def _handle_lookup(self, agent_id: str) -> None:
        try:
            validate_agent_id(agent_id)
        except AgentIDError as exc:
            self._json(400, {"error": "invalid-agent-id", "detail": str(exc)})
            return

        record = self.store.lookup(agent_id)
        if record is None:
            self._json(
                404,
                {
                    "error": "agent-not-registered",
                    "detail": f"no agent registered with id {agent_id}",
                },
            )
            return

        self._json(
            200,
            {
                "agent_id": agent_id,
                "host": record["host"],
                "port": record["port"],
            },
        )

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)


def run(
    host: str,
    port: int,
    store_path: Path,
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
) -> None:
    store = RegistryStore(store_path)

    class Handler(RegistryHandler):
        pass

    Handler.store = store

    server = ThreadingHTTPServer((host, port), Handler)

    if certfile and keyfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"

    print(
        f"[registry] listening on {scheme}://{host}:{port} "
        f"(store: {store_path})"
    )
    print(f"[registry] health: {scheme}://{host}:{port}/health")
    print(
        f"[registry] lookup: {scheme}://{host}:{port}/registry/{{agent-id}}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[registry] shutting down")
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agtp-registry",
        description="AGTP Registry Server",
        epilog=(
            "Examples:\n"
            "  python -m registry 8080            # positional port\n"
            "  python -m registry --port 8080     # named port\n"
            "  python -m registry                 # default port 8080"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "port_pos",
        nargs="?",
        type=int,
        metavar="PORT",
        help=f"Port to listen on (defaults to {DEFAULT_PORT}).",
    )
    parser.add_argument("--port", type=int, dest="port_flag")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--store",
        default=REGISTRY_FILE_DEFAULT,
        help="Path to the registry JSON file",
    )
    parser.add_argument("--cert", help="TLS certificate file")
    parser.add_argument("--key", help="TLS private key file")
    args = parser.parse_args()

    if args.port_pos is not None and args.port_flag is not None:
        parser.error(
            "specify the port positionally or with --port, not both"
        )
    port = args.port_pos if args.port_pos is not None else (
        args.port_flag if args.port_flag is not None else DEFAULT_PORT
    )

    if bool(args.cert) != bool(args.key):
        print(
            "[registry] both --cert and --key are required for TLS",
            file=sys.stderr,
        )
        return 2

    run(args.host, port, normalize(args.store), args.cert, args.key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
