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
  python -m server --insecure --port 4480 --agents-dir agents/
  python -m server --port 4480 --cert cert.pem --key key.pem
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

import json as _json
from core import status as status_codes
from core import wire
from core._paths import normalize
from server.config import CONFIG_FILENAME, ServerConfig, default_config, load as load_config
from core.identity import (
    CONTENT_TYPE_MANIFEST_JSON,
    AgentDocument,
    from_dict,
)
from server.manifest import generate as generate_manifest
from server.methods import REGISTRY, dispatch, error_response, spec_to_amg_spec
from server.negotiation import SYNTHESES
from server.synthesis import (
    PassthroughPolicy,
    RecipeBasedPolicy,
    RecipeFileError,
    SynthesisRuntime,
    load_recipes,
)


# Methods exempt from soft-deny / wildcards refusal. These are protocol
# primitives: every reachable agent must respond to them regardless of
# what its requires.methods declares. The set covers DISCOVER, DESCRIBE,
# and the embedded mechanics.
SOFT_DENY_EXEMPT_METHODS = frozenset({
    "DISCOVER", "DESCRIBE",
    "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
})


def _maybe_redirect_via_synthesis(
    request: wire.AGTPRequest,
) -> tuple[wire.AGTPRequest, bool]:
    """
    Synthesis-Id requests are now routed by :class:`SynthesisRuntime`
    inside :func:`server.methods.dispatch`. The handler walks the
    associated :class:`SynthesisPlan` and dispatches each step
    individually, preserving the v1 accept-on-exact-match wire shape
    via the runtime's :class:`PassthroughPolicy` and adding multi-step
    plan support.

    This shim is preserved as a no-op so callers that imported it
    directly keep linking; the soft-deny gate continues to skip
    synthesis-driven requests by checking
    :data:`SYNTHESES` membership separately.
    """
    syn_id = wire.header(request, "Synthesis-Id")
    via_synthesis = bool(syn_id and SYNTHESES.get(syn_id) is not None)
    return request, via_synthesis


def _load_recipe_policy(config: ServerConfig) -> Optional[RecipeBasedPolicy]:
    """
    Resolve and load the recipes file relative to the config's source
    path (or current directory if defaults). Logs failures to stderr
    and returns None so the server can keep starting under
    passthrough-only synthesis.
    """
    from server.synthesis import RecipeBasedPolicy as _RBP

    rel = config.synthesis.recipes_file
    base = (
        config.source_path.parent if config.source_path is not None else Path.cwd()
    )
    candidate = Path(rel)
    if not candidate.is_absolute():
        candidate = (base / rel).resolve()
    if not candidate.exists():
        print(
            f"[server] recipes file not found: {candidate}",
            file=sys.stderr,
        )
        return None
    try:
        recipes = load_recipes(candidate)
    except RecipeFileError as exc:
        print(f"[server] {exc}", file=sys.stderr)
        return None
    print(
        f"[server] loaded {len(recipes)} synthesis recipe(s) from {candidate}",
        file=sys.stderr,
    )
    return _RBP(recipes)


def soft_deny_check(
    method_name: str,
    agent_doc: AgentDocument,
    config: ServerConfig,
) -> Optional[wire.AGTPResponse]:
    """
    Apply the v2 inbound gate before dispatch.

    Precedence (documented; do not reorder without updating the design
    note and the tests in test_methods.py):

      1. 403 Wildcards Refused  -- agent declares wildcards: true and
         the server policy says wildcards_accepted: false. Applies to
         non-embedded methods only; embedded methods (including the
         four cognitive primitives that are otherwise subject to
         soft-deny) flow through. Body carries
         error.code="wildcards-refused".
      2. 403 Method Not Permitted -- the method is not in the agent's
         requires.methods and wildcards is false. Body carries
         error.code="method-not-permitted-for-agent".
      3. 455 Scope Violation    -- handler-local check, runs after
         soft-deny passes.

    Methods in SOFT_DENY_EXEMPT_METHODS bypass this gate entirely.

    TODO: when remote agent documents become first-class, the soft-deny
    check needs a "document-pull vs document-presented" distinction
    (see Interaction Model design note section 6). For now, this
    server only soft-denies on its own hosted agents.
    """
    method_upper = method_name.upper()
    if method_upper in SOFT_DENY_EXEMPT_METHODS:
        return None

    # Unknown methods bypass soft-deny so the dispatcher can return
    # 501. Saying "you don't declare X" for a verb the server itself
    # has never heard of would be misleading.
    if method_upper not in REGISTRY:
        return None

    is_embedded = REGISTRY[method_upper].source == "agtp/1.0"

    # Step 1: wildcards refusal (highest precedence).
    if (
        agent_doc.requires.wildcards
        and not config.policy.wildcards_accepted
        and not is_embedded
    ):
        return status_codes.wildcards_refused(agent_doc.agent_id)

    # Step 2: soft-deny when not permitted and wildcards is false.
    # 452 reframed under v07: agents are users, the call is a refusal
    # of permission rather than a missing capability declaration.
    declared = method_upper in {m.upper() for m in agent_doc.requires.methods}
    if not declared and not agent_doc.requires.wildcards:
        return status_codes.method_not_permitted_for_agent(
            method_upper, agent_doc.agent_id
        )

    # Step 3: scope-violation lives inside the per-method handler. Pass.
    return None


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
    """In-memory map of agent_id -> AgentDocument, loaded from disk.

    Also serves as the ``ServerState`` passed into method handlers and
    the synthesis runtime; the latter attaches itself via the
    ``synthesis_runtime`` attribute so PROPOSE handling and
    Synthesis-Id execution can reach it from inside ``dispatch``.
    """

    def __init__(self, agents_dir: Path):
        self.agents_dir = Path(agents_dir)
        self.agents: Dict[str, AgentDocument] = {}
        self.synthesis_runtime: Optional[SynthesisRuntime] = (
            self._make_default_runtime()
        )
        self._load()

    def _make_default_runtime(self) -> SynthesisRuntime:
        """
        Build a runtime with the v1-compatible passthrough policy
        only. Production servers extend this via
        :meth:`configure_synthesis` once the config is loaded; the
        default is enough for tests and for the
        accept-on-exact-match path that v1 PROPOSE relied on.
        """

        def _step_dispatch(req, state, agent_doc):
            return dispatch(req, state, agent_doc)

        return SynthesisRuntime(step_dispatcher=_step_dispatch)

    def configure_synthesis(self, config: ServerConfig) -> None:
        """
        Reconfigure the synthesis runtime per the supplied server
        config. Loads recipes from disk, builds the policy chain in
        the configured order, and replaces the default runtime.
        Errors loading recipes are surfaced to stderr but do not
        crash startup — the server falls back to passthrough-only.
        """
        if self.synthesis_runtime is None:
            return
        policies: list = []
        for name in (config.synthesis.policies or []):
            if name == "recipes":
                policies.append(_load_recipe_policy(config))
            elif name == "passthrough":
                policies.append(PassthroughPolicy())
            else:
                print(
                    f"[server] unknown synthesis policy {name!r} — skipped",
                    file=sys.stderr,
                )
        # The runtime appends PassthroughPolicy automatically when it
        # isn't already in the chain, so a config of just ["recipes"]
        # still preserves v1 accept-on-exact-match behavior.
        self.synthesis_runtime.policies = [p for p in policies if p is not None]
        # Re-run the auto-append shim by going through __init__-style
        # logic: ensure passthrough is last unless explicitly present.
        if not any(
            getattr(p, "name", "") == "passthrough"
            for p in self.synthesis_runtime.policies
        ):
            self.synthesis_runtime.policies.append(PassthroughPolicy())

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
    *,
    soft_deny_enabled: bool = True,
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

        # Synthesis redirect: requests carrying Synthesis-Id are
        # rewritten to the underlying method and bypass the soft-deny
        # gate. The accepted PROPOSE that produced the synthesis is the
        # contract that authorizes the rewritten method.
        request, via_synthesis = _maybe_redirect_via_synthesis(request)
        method_name = request.method.upper()

        # Soft-deny gate: 462 / 452 before per-handler dispatch.
        # See soft_deny_check() for the precedence contract.
        if soft_deny_enabled and not via_synthesis:
            denial = soft_deny_check(method_name, agent_doc, config)
            if denial is not None:
                conn.sendall(denial.serialize())
                return

        response = dispatch(request, registry, agent_doc, config=config)
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
    *,
    soft_deny_enabled: bool = True,
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

    # Configure the synthesis runtime per [synthesis] in the config.
    registry.configure_synthesis(config)

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
                kwargs={"soft_deny_enabled": soft_deny_enabled},
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
            "  python -m server 4480              # positional port\n"
            "  python -m server --port 4480       # named port\n"
            "  python -m server                   # default port 4480\n"
            "  python -m server --host 0.0.0.0 --cert c.pem --key k.pem"
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
    parser.add_argument(
        "--no-soft-deny",
        action="store_true",
        help=(
            "Disable the v2 inbound gate (452 / 462). For legacy "
            "compatibility and isolated testing only; production "
            "deployments should leave this on."
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

    run(
        args.host,
        port,
        agents_path,
        args.cert,
        args.key,
        config=config,
        soft_deny_enabled=not args.no_soft_deny,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
