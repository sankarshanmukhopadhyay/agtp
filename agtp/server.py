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
import importlib
import json
import socket
import ssl
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from agtp import wire
from agtp._paths import normalize
from agtp.config import CONFIG_FILENAME, ServerConfig, default_config, load as load_config
from agtp.identity import (
    CONTENT_TYPE_MANIFEST_JSON,
    AgentDocument,
    from_dict,
)
from agtp.manifest import generate as generate_manifest
from agtp.methods import REGISTRY, dispatch, error_response


DEFAULT_PORT = 4480
DEFAULT_AGENTS_DIR = "agents"

# Hosts that bind to the loopback interface only. When the server is
# listening on one of these, plaintext is the convenient default for
# local development; non-loopback bindings still require explicit TLS
# or `--insecure`.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


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


def serve_manifest(
    request: wire.AGTPRequest,
    registry: AgentRegistry,
    config: ServerConfig,
) -> wire.AGTPResponse:
    """
    Build and return the Server Manifest for a server-level DISCOVER.

    Server-level DISCOVER does not require an agent target; it does not
    consult any per-agent capability list. The disclosure policy in the
    config decides how openly the agents.list reflects the server's
    hosted agents.
    """
    manifest = generate_manifest(config, registry.agents)
    body = manifest.to_json(pretty=True).encode("utf-8")
    return wire.AGTPResponse(
        status_code=200,
        status_text="OK",
        headers={
            "Content-Type": CONTENT_TYPE_MANIFEST_JSON,
            "Content-Length": str(len(body)),
        },
        body_bytes=body,
    )


def handle_connection(
    conn,
    registry: AgentRegistry,
    config: Optional[ServerConfig] = None,
) -> None:
    """Handle a single AGTP connection: read one request, write one response."""
    if config is None:
        config = default_config()
    try:
        reader = conn.makefile("rb")
        request = wire.parse_request(reader)
        method_name = request.method.upper()
        target_header = wire.header(request, "Target-Agent")

        # Server-level DISCOVER: no Target-Agent header, method is
        # DISCOVER. Returns the Server Manifest. Does not require any
        # agent to advertise DISCOVER in its requires.methods.
        if method_name == "DISCOVER" and not target_header:
            response = serve_manifest(request, registry, config)
            conn.sendall(response.serialize())
            return

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
    config: Optional[ServerConfig] = None,
) -> None:
    registry = AgentRegistry(agents_dir)
    if not registry.agents:
        print(
            f"[server] WARNING: no agents loaded from {agents_dir}",
            file=sys.stderr,
        )

    if config is None:
        config = default_config(host)
    if config.is_default:
        print(
            f"[server] no {CONFIG_FILENAME} found; using default manifest "
            f"identity (issuer={config.server.issuer!r})",
            file=sys.stderr,
        )
    else:
        print(
            f"[server] manifest identity: {config.server.issuer} "
            f"(operator: {config.server.operator})"
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
                target=handle_connection,
                args=(conn, registry, config),
                daemon=True,
            )
            t.start()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agtp-server",
        description="AGTP Agent Server",
        epilog=(
            "Examples:\n"
            "  python -m agtp.server 4480              # positional port\n"
            "  python -m agtp.server --port 4480       # named port\n"
            "  python -m agtp.server                   # default port 4480\n"
            "  python -m agtp.server --host 0.0.0.0 --cert c.pem --key k.pem"
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
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind. Loopback hosts default to plaintext.",
    )
    parser.add_argument(
        "--agents-dir",
        default=DEFAULT_AGENTS_DIR,
        help=(
            f"Directory containing *.agent.json files "
            f"(defaults to ./{DEFAULT_AGENTS_DIR}; created if missing)."
        ),
    )
    parser.add_argument("--cert", help="TLS certificate file")
    parser.add_argument("--key", help="TLS private key file")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Force plaintext on a non-loopback bind (development only).",
    )
    parser.add_argument(
        "--load-module",
        action="append",
        default=[],
        metavar="MODULE",
        help=(
            "Import a Python module before serving so it can register "
            "custom methods (repeatable). Example: "
            "--load-module agtp.examples.custom_methods"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "Path to an agtp-server.toml. If omitted, looks for "
            "./agtp-server.toml; if none is present, defaults are used."
        ),
    )
    args = parser.parse_args()

    if args.port_pos is not None and args.port_flag is not None:
        parser.error(
            "specify the port positionally or with --port, not both"
        )
    port = args.port_pos if args.port_pos is not None else (
        args.port_flag if args.port_flag is not None else DEFAULT_PORT
    )

    for mod_name in args.load_module:
        try:
            importlib.import_module(mod_name)
            print(f"[server] loaded custom-method module: {mod_name}")
        except ImportError as exc:
            print(
                f"[server] failed to load module {mod_name!r}: {exc}",
                file=sys.stderr,
            )
            return 2

    use_tls = bool(args.cert and args.key)
    loopback = _is_loopback(args.host)

    if not use_tls and not loopback and not args.insecure:
        print(
            f"[server] non-loopback bind ({args.host}) requires either "
            f"--cert/--key or --insecure",
            file=sys.stderr,
        )
        return 2

    if not use_tls and loopback and not args.insecure:
        print(
            f"[server] running plaintext on loopback ({args.host}); "
            f"production deployments must use TLS",
            file=sys.stderr,
        )

    agents_path = normalize(args.agents_dir)
    if not agents_path.exists():
        agents_path.mkdir(parents=True, exist_ok=True)
        print(
            f"[server] created empty agents directory: {agents_path}",
            file=sys.stderr,
        )

    try:
        config = load_config(
            Path(args.config) if args.config else None,
            host=args.host,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"[server] config error: {exc}", file=sys.stderr)
        return 2

    run(args.host, port, agents_path, args.cert, args.key, config=config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
