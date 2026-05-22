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
from typing import Any, Dict, List, Optional

import json as _json
from core import status as status_codes
from core import wire
from core._paths import normalize
from core.genesis import AgentGenesis
from server.config import CONFIG_FILENAME, ServerConfig, default_config, load as load_config
from core.identity import (
    CONTENT_TYPE_MANIFEST_JSON,
    DOC_TYPE_SERVER_MANIFEST,
    HEADER_DOCUMENT_TYPE,
    AgentDocument,
    from_dict,
)
from server.manifest import generate as generate_manifest
from server.methods import REGISTRY, dispatch, error_response
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

    from core.methods import EMBEDDED_VERBS
    is_embedded = method_upper in EMBEDDED_VERBS

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
DEFAULT_ENDPOINTS_DIR = "endpoints"

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
        # Phase 4: per-agent Agent Genesis documents loaded from
        # ``{name}.genesis.json`` files alongside each ``{name}.agent.json``.
        # Missing files are tolerated (the agent runs as transport-only
        # identity). Loaded Geneses are exposed via DISCOVER /genesis
        # so verifiers can fetch + verify the cert-binding.
        self.geneses: Dict[str, "AgentGenesis"] = {}
        self.synthesis_runtime: Optional[SynthesisRuntime] = (
            self._make_default_runtime()
        )
        # Gateway mode (M3 step b) is opt-in via --gateway-socket. When
        # this attribute is set, ``server.handler_resolution.resolve_handler``
        # routes registered_function bindings through a gateway-dispatch
        # closure instead of importing the function in-daemon. Composition
        # and external_service bindings continue to resolve in-daemon.
        # See docs/architecture/gateway-protocol-v1.md.
        self.gateway_server: Optional[Any] = None
        # M9 operational-module dispatch hooks. mod_cache, mod_audit,
        # and any future hook-based module register here during
        # --load-module boot. The dispatcher in ``server.methods._serve_endpoint``
        # consults this registry before and after each handler call.
        from server.hooks import HookRegistry as _HookRegistry
        self.hook_registry = _HookRegistry()
        # Ed25519 signing service (loaded lazily during run() when
        # [signing].enabled is true). Operational modules consume
        # this through the AgentRegistry; stays None when signing
        # is disabled, in which case Attribution-Record falls back
        # to the pre-§5 placeholder shape.
        self.signing_service: Optional[Any] = None
        # mTLS cert verifier (loaded during run() when [mtls].mode
        # is "optional" or "required"). handle_connection extracts
        # the peer cert from each accepted connection and runs it
        # through this verifier to populate the EndpointContext's
        # agent_verified / agent_cert_fingerprint fields.
        self.cert_verifier: Optional[Any] = None
        # Per-server method policy. ``configure_methods_policy()``
        # replaces it from a loaded ServerConfig at startup; the
        # default is allow-all so a fresh checkout boots without
        # operator intervention.
        from server.config import default_methods_policy as _default_methods_policy
        self.methods_policy = _default_methods_policy()
        # §7 asynchronous PROPOSE evaluation store. Always present so
        # the dispatcher / built-in lookup paths don't have to guard
        # against ``None``; whether the PROPOSE handler routes through
        # it depends on ``policies.synthesis.async_evaluation_enabled``.
        from server.proposal_store import ProposalStore as _ProposalStore
        self.proposal_store = _ProposalStore()
        # The dispatcher reads ``config`` for synthesis durations,
        # async opt-in, and audit-log routing. Boot fills this from
        # the actual config; default is None so unit-test fixtures
        # that build a bare AgentRegistry still work.
        self.config = None
        # Phase-2 endpoint registry. Empty by default; populated by
        # ``configure_endpoints()`` during startup when a directory of
        # ``*.toml`` endpoint declarations is present. Attached
        # directly so ``server.methods.dispatch`` and
        # ``serve_manifest`` can reach it through the ServerState.
        from server.endpoint_registry import EndpointRegistry as _ER
        self.endpoint_registry = _ER()
        self._load()

    def configure_methods_policy(self, policy: "MethodsPolicy") -> None:
        """
        Attach a :class:`MethodsPolicy` instance loaded from the
        server's ``[policies.methods]`` config block.

        Pre-§6 servers loaded this from a separate ``methods.txt``
        file; that file format is retired (see ``agtp-api §8``).
        The policy now lives in ``agtp-server.toml`` under
        ``[policies.methods]`` and is parsed by
        :func:`server.config.methods_policy_from_table`.
        """
        self.methods_policy = policy

    def configure_endpoints(self, endpoints_dir: Path) -> None:
        """
        Phase-2 startup hook: load every ``*.toml`` file in
        ``endpoints_dir``, resolve each declaration's handler, and
        register it on this server's :class:`EndpointRegistry`.

        Behavior on failure:

          * Loader errors (parse / validation / io) are logged to
            stderr; the offending files are skipped, the rest of
            the directory continues to load.
          * Handler resolution failures are logged similarly.
            ``composition`` and ``external_service`` bindings raise
            :class:`NotImplementedError` (Phases 3 & 4); we log the
            skip and continue rather than aborting startup so an
            operator authoring future-phase TOML against today's
            server gets a clear pointer instead of a crash.
          * Registry insertion failures (duplicates, validator
            refusals after handler resolution) are likewise logged.

        The boot sequence does NOT abort startup unless every
        endpoint failed AND there was at least one to begin with —
        the latter is almost always a misconfigured directory worth
        surfacing.
        """
        from server.endpoint_loader import load_endpoints
        from server.endpoint_registry import (
            DuplicateEndpointError, InvalidEndpointError,
        )
        from server.handler_resolution import (
            InvalidHandlerError, resolve_handler,
        )

        if not endpoints_dir.exists():
            print(
                f"[server] no endpoints directory at {endpoints_dir}; "
                f"endpoint registry remains empty",
                file=sys.stderr,
            )
            return

        specs, load_errors = load_endpoints(endpoints_dir)
        for err in load_errors:
            print(
                f"[server] endpoint load error ({err.error_type}) at "
                f"{err.file_path}: {err.message}",
                file=sys.stderr,
            )

        registered = 0
        skipped = 0
        for spec in specs:
            try:
                handler = resolve_handler(
                    spec.handler, server_state=self, spec=spec,
                )
            except NotImplementedError as exc:
                print(
                    f"[server] endpoint ({spec.name}, {spec.path}) skipped: "
                    f"{exc}",
                    file=sys.stderr,
                )
                skipped += 1
                continue
            except InvalidHandlerError as exc:
                print(
                    f"[server] endpoint ({spec.name}, {spec.path}) skipped: "
                    f"{exc}",
                    file=sys.stderr,
                )
                skipped += 1
                continue
            try:
                self.endpoint_registry.register(spec, handler)
                registered += 1
            except (InvalidEndpointError, DuplicateEndpointError) as exc:
                print(
                    f"[server] endpoint ({spec.name}, {spec.path}) "
                    f"refused at registration: {exc}",
                    file=sys.stderr,
                )
                skipped += 1

        total = len(specs) + len(load_errors)
        if total > 0 and registered == 0:
            print(
                f"[server] WARNING: every endpoint in {endpoints_dir} "
                f"failed to load — check the errors above",
                file=sys.stderr,
            )
        if registered:
            print(
                f"[server] loaded {registered} endpoint(s) from "
                f"{endpoints_dir}"
                + (f" ({skipped} skipped)" if skipped else ""),
                file=sys.stderr,
            )

    def register_builtins(self) -> None:
        """
        Register the server-internal built-in endpoints
        (``DISCOVER /methods``, ``QUERY /proposals/{proposal_id}``,
        and any future additions). Call this AFTER
        :meth:`configure_endpoints` so operator-authored TOML can
        override a built-in's ``(method, path)`` by declaring it
        first.
        """
        from server.builtins import register_builtins as _reg
        count = _reg(
            self.endpoint_registry,
            proposal_store=self.proposal_store,
        )
        if count:
            print(
                f"[server] registered {count} built-in endpoint(s) "
                f"(DISCOVER /methods, QUERY /proposals/{{proposal_id}}, ...)",
                file=sys.stderr,
            )

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
        # Mirror policies.max_synthesis_depth into the runtime so
        # the depth bound takes effect on every composition attempt.
        self.synthesis_runtime.max_synthesis_depth = int(
            config.policy.max_synthesis_depth
        )

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
                continue

            # Phase 4: optionally load the matching Agent Genesis.
            # Naming convention: ``{stem}.genesis.json`` next to
            # ``{stem}.agent.json``. Missing file is fine — Phase-1
            # agents predate Genesis.
            stem = json_path.name[: -len(".agent.json")]
            genesis_path = self.agents_dir / f"{stem}.genesis.json"
            if not genesis_path.exists():
                continue
            try:
                from core.genesis import load_genesis_json
                genesis = load_genesis_json(
                    genesis_path.read_text(encoding="utf-8"),
                )
                # Sanity check: Genesis hash MUST equal AgentDocument's
                # agent_id, otherwise the two artifacts disagree on
                # who this agent is. Skip the Genesis on mismatch
                # rather than crash boot.
                if genesis.canonical_agent_id() != doc.agent_id:
                    print(
                        f"[server] skipping {genesis_path.name}: "
                        f"Genesis hash {genesis.canonical_agent_id()[:12]}... "
                        f"does not match agent_id {doc.agent_id[:12]}...",
                        file=sys.stderr,
                    )
                    continue
                self.geneses[doc.agent_id] = genesis
                print(
                    f"[server]   genesis for {doc.name} "
                    f"(issuer={genesis.issuer}, tier={genesis.trust_tier})"
                )
                # Phase 5: when the AgentDocument doesn't declare its
                # own trust posture, lift it from the Genesis. The
                # Genesis is the governance-layer source of truth, so
                # an undeclared AgentDocument inherits whatever the
                # registrar issued. Explicit AgentDocument values win
                # — an operator who hard-codes a tier in the .json
                # file is opting out of the auto-derivation.
                _derive_trust_from_genesis(doc, genesis, data)
                # And the owner_id, similarly.
                if not doc.owner_id and genesis.owner_id:
                    doc.owner_id = genesis.owner_id
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[server] skipping {genesis_path.name}: {exc}",
                    file=sys.stderr,
                )

    def lookup_genesis(self, agent_id: str) -> Optional["AgentGenesis"]:
        """Return the Agent Genesis for ``agent_id``, or ``None`` when
        the agent has no Genesis loaded (transport-only identity)."""
        return self.geneses.get(agent_id)

    def lookup(self, agent_id: str) -> Optional[AgentDocument]:
        return self.agents.get(agent_id)

    def list_ids(self) -> List[str]:
        return list(self.agents.keys())


def _derive_trust_from_genesis(
    doc: AgentDocument,
    genesis: AgentGenesis,
    source_data: Dict[str, Any],
) -> None:
    """Lift trust_tier / verification_path from a Genesis when the
    AgentDocument JSON didn't declare them explicitly.

    Detection rule: presence in the source JSON wins, even when the
    declared value equals the default. The dataclass can't tell
    "operator set tier=2" from "operator omitted tier"; the raw dict
    from disk can.
    """
    from core.identity import TIER_2_TRUST_WARNING
    declared_tier = "trust_tier" in source_data
    declared_path = "verification_path" in source_data
    if not declared_tier:
        doc.trust_tier = int(genesis.trust_tier)
    if not declared_path:
        doc.verification_path = str(genesis.verification_path)
    # The dataclass __post_init__ auto-populated trust_warning at
    # construction time based on the *default* trust_tier. If the
    # tier changed during derivation, re-evaluate.
    if doc.trust_tier == 2 and not doc.trust_warning:
        doc.trust_warning = TIER_2_TRUST_WARNING
    elif doc.trust_tier != 2 and not source_data.get("trust_warning"):
        # Lifting to tier 1 / 3 invalidates the auto-tier-2 warning.
        doc.trust_warning = ""


def _select_target(
    request: wire.AGTPRequest, registry: AgentRegistry
) -> tuple[Optional[AgentDocument], Optional[wire.AGTPResponse]]:
    """
    Resolve which AgentDocument the request is addressing.

    Returns (doc, None) on success or (None, error_response) on failure.
    The ``Agent-ID`` header (legacy ``Target-Agent``) names the agent.
    With neither header set, a single-agent server selects its sole
    agent for caller convenience.
    """
    target = wire.read_agent_id(request)
    if not target:
        ids = registry.list_ids()
        if len(ids) == 1:
            return registry.lookup(ids[0]), None
        return None, error_response(
            400,
            "Bad Request",
            "missing-agent-id",
            "Agent-ID header required when server hosts multiple agents",
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
    config decides how openly the ``hosted_agents`` field reflects the
    server's hosted agents.

    Phase-2 servers expose their endpoint registry under the
    manifest's ``endpoints`` key; the embedded methods continue to
    surface under ``embedded_methods`` so older readers keep working.
    """
    manifest = generate_manifest(
        config,
        registry.agents,
        endpoint_registry=getattr(registry, "endpoint_registry", None),
    )
    body = manifest.to_json(pretty=True).encode("utf-8")
    # X-AGTP-Document-Type lets a header-first renderer (elemen)
    # dispatch on the document kind without parsing the body. The
    # main server emits the canonical "agtp.server.manifest" type;
    # application-typed servers (e.g., the MCP-on-AGTP gateway) emit
    # "agtp.server.identity" + an X-AGTP-Application discriminator
    # of their own.
    return wire.AGTPResponse(
        status_code=200,
        status_text="OK",
        headers={
            "Content-Type": CONTENT_TYPE_MANIFEST_JSON,
            "Content-Length": str(len(body)),
            HEADER_DOCUMENT_TYPE: DOC_TYPE_SERVER_MANIFEST,
        },
        body_bytes=body,
    )


def _finalize_response(
    response: wire.AGTPResponse,
    request: Optional[wire.AGTPRequest],
    config: Optional[ServerConfig],
    *,
    attribution_extra: Optional[Dict[str, Any]] = None,
    owner_id: str = "",
    principal_id: str = "",
) -> None:
    """Apply §10 response-header policy to every outbound response.

    Concerns:

      * **Server-ID** (mandatory) — every response identifies which
        server produced it. Value comes from
        ``config.server.server_id``.
      * **Task-ID** echo — when the request carried a ``Task-ID``
        header the response echoes it back so clients can correlate.
      * **Request-ID** echo — same pattern for ``Request-ID``.
      * **Response-ID** (synthesized) — every response carries a
        fresh ``Response-ID`` so the caller can correlate this
        specific response among multiple parallel requests.
      * **Audit-ID** — when Attribution-Records are enabled,
        ``sha256(jws)`` of the Attribution-Record JWS. Anchors the
        per-agent hash chain via ``previous_audit_id`` on the next
        record.
      * **Owner-ID** — when the agent's owner is known, stamped on
        the response so clients see the legal entity responsible for
        the agent's behavior.
      * **Attribution-Record** — when
        ``[audit] attribution_records_enabled = true``, a JWS Compact
        Serialization (RFC 7515 §3.1) signed receipt. When signing
        is disabled the daemon still emits an ``alg: none`` unsecured
        JWS of the same shape so consumers see one format.

    ``attribution_extra`` is opt-in handler-supplied data that rides
    in the JWS payload's ``extra`` block. Used for handler-specific
    governance metadata (action_id, evaluation_id, intent_assertion_jti)
    that the daemon doesn't itself produce.

    ``owner_id`` and ``principal_id`` are passed by the dispatcher
    after it resolves the Agent Document; they're not derivable from
    the request headers alone.
    """
    if response is None:
        return
    headers = dict(response.headers or {})

    # Mandatory Server-ID.
    server_id = ""
    if config is not None and getattr(config, "server", None) is not None:
        server_id = getattr(config.server, "server_id", "") or ""
        if server_id and "Server-ID" not in headers:
            headers["Server-ID"] = server_id

    # Inbound correlation echoes (Task-ID, Request-ID) and synthesis
    # (Response-ID, Agent-ID readback).
    task_id = ""
    request_id = ""
    agent_id = ""
    session_id = ""
    if request is not None:
        task_id = wire.header(request, "Task-ID")
        if task_id and "Task-ID" not in headers:
            headers["Task-ID"] = task_id
        request_id = wire.header(request, "Request-ID")
        if request_id and "Request-ID" not in headers:
            headers["Request-ID"] = request_id
        session_id = wire.header(request, "Session-ID")
        agent_id = wire.read_agent_id(request)

    # Response-ID: fresh every time. Independent of any inbound id.
    if "Response-ID" not in headers:
        import uuid as _uuid
        response_id = f"resp-{_uuid.uuid4().hex[:12]}"
        headers["Response-ID"] = response_id
    else:
        response_id = headers["Response-ID"]

    # Owner-ID surfacing. The agent's owner is a property of the
    # agent's identity (registered at Genesis time); stamped on the
    # response so callers see the legal entity responsible for this
    # agent's behavior without parsing the body.
    if owner_id and "Owner-ID" not in headers:
        headers["Owner-ID"] = owner_id

    # Attribution-Record + Audit-ID. When attribution_records_enabled
    # is true the daemon stamps both headers. The JWS is signed when
    # [signing].enabled is true; otherwise an alg:none unsecured JWS
    # carries the same payload shape so verifiers see one format.
    audit = getattr(config, "audit", None) if config is not None else None
    if audit is not None and getattr(
        audit, "attribution_records_enabled", False
    ):
        from datetime import datetime as _dt, timezone as _tz
        from server.audit_chain import (
            AuditChainStore as _AuditChainStore,
            default_chain_head_root as _default_chain_head_root,
        )
        from server.audit_records import (
            AuditRecordStore as _AuditRecordStore,
            default_records_root as _default_records_root,
        )

        issued_at = _dt.now(tz=_tz.utc).isoformat().replace("+00:00", "Z")
        signing_service = (
            getattr(config, "signing_service", None) if config is not None else None
        )

        # Per-agent chain head: read predecessor before signing,
        # write the new head after. Store path is configurable; the
        # platform default keeps a vanilla install operator-free.
        chain_head_root = getattr(audit, "chain_head_root", "") or ""
        chain_store = _AuditChainStore(
            Path(chain_head_root).expanduser() if chain_head_root
            else _default_chain_head_root()
        )
        prior_head = chain_store.head(agent_id) if agent_id else None
        previous_audit_id = prior_head.audit_id if prior_head is not None else ""

        builder = (
            signing_service.build_attribution_record
            if signing_service is not None
            else _build_unsigned_attribution_record
        )
        record = builder(
            agent_id=agent_id,
            owner_id=owner_id,
            principal_id=principal_id,
            server_id=server_id,
            session_id=session_id,
            task_id=task_id,
            request_id=request_id,
            response_id=response_id,
            issued_at=issued_at,
            status=response.status_code,
            previous_audit_id=previous_audit_id,
            extra=attribution_extra,
        )
        headers["Attribution-Record"] = record.jws
        headers["Audit-ID"] = record.audit_id

        # Phase 6: persist the JWS under its audit_id so INSPECT can
        # serve it later. Independent of chain-head update — even
        # server-level operations (empty agent_id, no chain head)
        # produce a JWS that the INSPECT surface can return.
        records_root = getattr(audit, "records_root", "") or ""
        record_store = _AuditRecordStore(
            Path(records_root).expanduser() if records_root
            else _default_records_root()
        )
        try:
            record_store.write(record.audit_id, record.jws)
        except OSError as exc:  # pragma: no cover - defensive
            import sys as _sys
            print(
                f"[server] audit record write failed for "
                f"{record.audit_id[:12]}...: {exc}",
                file=_sys.stderr,
            )

        # Chain head update — best-effort. A failure (disk full,
        # permission denied) is logged but doesn't fail the response.
        if agent_id:
            try:
                chain_store.write(agent_id, record.audit_id, issued_at)
            except OSError as exc:  # pragma: no cover - defensive
                import sys as _sys
                print(
                    f"[server] chain head write failed for {agent_id}: {exc}",
                    file=_sys.stderr,
                )

    response.headers = headers


def _build_unsigned_attribution_record(**kwargs):
    """Standalone alg:none builder used when no signing service is loaded.

    Constructs a SigningService-less instance just to call into the
    same canonical-encoding helpers. Cheap — no key material is
    handled.
    """
    from server.signing import (
        SigningService as _SigningService,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _Ed25519PrivateKey,
    )
    # alg:none doesn't actually sign anything; use a throwaway key
    # so we can call into the unsigned builder. The key bytes never
    # leave this function and never sign anything.
    throwaway = _Ed25519PrivateKey.generate()
    svc = _SigningService(private_key=throwaway)
    return svc.build_unsigned_attribution_record(**kwargs)


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
        # ``Agent-ID`` is the §10 canonical name; ``Target-Agent`` is
        # accepted for back-compat (read_agent_id emits a deprecation
        # warning when it falls through).
        target_header = wire.read_agent_id(request)

        # Phase B: when mTLS is enabled, verify the peer cert and
        # stash the result on the request for downstream consumers
        # (gateway trust block, EndpointContext.agent_verified). The
        # TLS library already validated the chain; the verifier
        # extracts the Ed25519 public key, derives the Agent-ID,
        # computes the cert fingerprint.
        request.verified_cert = None  # runtime attribute; not on the wire
        cert_verifier = getattr(registry, "cert_verifier", None)
        if cert_verifier is not None:
            try:
                peer_der = conn.getpeercert(binary_form=True)
            except (AttributeError, OSError):
                peer_der = None
            if peer_der:
                from server.mtls import CertVerificationError
                try:
                    request.verified_cert = cert_verifier.verify_peer_cert(peer_der)
                except CertVerificationError as exc:
                    response = error_response(
                        401, "Unauthorized",
                        "cert-verification-failed",
                        f"client cert verification failed: {exc}",
                        extra={"detail": exc.detail},
                    )
                    _finalize_response(response, request, config)
                    conn.sendall(response.serialize())
                    return
                # Cross-check against the Agent-ID header. When
                # [mtls].require_agent_id_match is on (default true),
                # mismatched identities refuse the request. When the
                # header is empty, the cert-derived identity becomes
                # authoritative and gets written back into the
                # request so downstream code sees it.
                mtls_cfg = getattr(config, "mtls", None)
                if (
                    mtls_cfg is not None
                    and mtls_cfg.require_agent_id_match
                    and target_header
                ):
                    try:
                        cert_verifier.cross_check_agent_id_header(
                            request.verified_cert, target_header,
                        )
                    except CertVerificationError as exc:
                        response = error_response(
                            401, "Unauthorized",
                            "agent-id-mismatch",
                            str(exc),
                            extra={"detail": exc.detail},
                        )
                        _finalize_response(response, request, config)
                        conn.sendall(response.serialize())
                        return
                if not target_header:
                    target_header = request.verified_cert.agent_id
                    request.headers["Agent-ID"] = request.verified_cert.agent_id

        # §10 delegation-chain gate. The header is reserved for v01;
        # v00 implementations refuse with 501 Not Implemented before
        # any other dispatch logic so the rejection cost is uniform
        # across endpoints.
        if wire.header(request, "Delegation-Chain"):
            from core import status as _status
            from core.wire import AGTPResponse as _Resp
            import json as _json
            body = _json.dumps({
                "error": {
                    "code": "delegation-not-supported",
                    "message": (
                        "the Delegation-Chain header is reserved for "
                        "future AGTP revisions; this server (v00) does "
                        "not support delegated authority. The header "
                        "is documented in agtp §10 Future Work."
                    ),
                }
            }, indent=2).encode("utf-8")
            response = _Resp(
                status_code=501,
                status_text="Not Implemented",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
                body_bytes=body,
            )
            _finalize_response(response, request, config)
            conn.sendall(response.serialize())
            return

        # §11 Forms 3 / 4: domain-anchored agent addressing. When the
        # request path matches ``/agents/{name}``, the server looks
        # the local name up against ``registry.agents`` (by the
        # AgentDocument's ``name`` field) and resolves to the
        # canonical Agent-ID. The path is then rewritten to ``/`` so
        # downstream dispatch sees the effective resource path. If
        # the request also carries an ``Agent-ID`` header that
        # disagrees, the server refuses with 400
        # ``agent-identity-mismatch``.
        import re as _re
        _FORM_3_4_RE = _re.compile(
            r"^/agents/(?P<handle>[A-Za-z0-9][A-Za-z0-9._\-]*)$",
        )
        path_match = _FORM_3_4_RE.match(getattr(request, "path", "") or "")
        if path_match:
            handle = path_match.group("handle")
            resolved = None
            for doc in registry.agents.values():
                if doc.name.lower() == handle.lower():
                    resolved = doc
                    break
            if resolved is None:
                resp = error_response(
                    404,
                    "Not Found",
                    "agent-handle-not-found",
                    f"no agent with name {handle!r} hosted at this server",
                    extra={"handle": handle},
                )
                _finalize_response(resp, request, config)
                conn.sendall(resp.serialize())
                return
            if target_header and target_header != resolved.agent_id:
                resp = error_response(
                    400,
                    "Bad Request",
                    "agent-identity-mismatch",
                    (
                        f"Agent-ID header {target_header!r} does not "
                        f"match the agent resolved from path "
                        f"/agents/{handle} ({resolved.agent_id!r})"
                    ),
                )
                _finalize_response(resp, request, config)
                conn.sendall(resp.serialize())
                return
            # Inject the resolved Agent-ID and rewrite the path so
            # downstream dispatch follows the standard agent-targeting
            # flow. The mutation is contained to this connection.
            target_header = resolved.agent_id
            request.headers["Agent-ID"] = resolved.agent_id
            request.path = "/"

        # Server-level DISCOVER: no Agent-ID header, method is
        # DISCOVER. Returns the Server Manifest. Does not require any
        # agent to advertise DISCOVER in its requires.methods.
        #
        # §7 anonymous-discovery gate: if the server's
        # ``policies.anonymous_discovery`` is false and the request
        # carries no agent identity, refuse with 262
        # Authorization Required (type=anonymous-discovery-disabled).
        # This is the dispatcher's authoritative enforcement of the
        # config flag the manifest already advertises.
        if method_name == "DISCOVER" and not target_header:
            agent_identity_header = wire.header(request, "Agent-Identity")
            if (
                not config.policy.anonymous_discovery
                and not agent_identity_header
            ):
                from core import status as _status
                response = _status.anonymous_discovery_disabled()
                _finalize_response(response, request, config)
                conn.sendall(response.serialize())
                return
            response = serve_manifest(request, registry, config)
            _finalize_response(response, request, config)
            conn.sendall(response.serialize())
            return

        agent_doc, target_err = _select_target(request, registry)
        if target_err is not None:
            _finalize_response(target_err, request, config)
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
                _finalize_response(denial, request, config)
                conn.sendall(denial.serialize())
                return

        response = dispatch(request, registry, agent_doc, config=config)
        # Handler may have stashed attribution_extra on the wire response
        # via _translate_endpoint_result; pop it for the finalizer.
        attribution_extra = getattr(response, "_attribution_extra", None)
        if attribution_extra is not None:
            try:
                delattr(response, "_attribution_extra")
            except AttributeError:
                pass
        _finalize_response(
            response,
            request,
            config,
            attribution_extra=attribution_extra,
            owner_id=getattr(agent_doc, "owner_id", "") or "",
            principal_id=getattr(agent_doc, "principal_id", "") or "",
        )
        conn.sendall(response.serialize())
    except wire.WireFormatError as exc:
        try:
            err_resp = error_response(
                400, "Bad Request", "invalid-wire-format", str(exc),
            )
            _finalize_response(err_resp, None, config)
            conn.sendall(err_resp.serialize())
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
    endpoints_dir: Optional[Path] = None,
    gateway_socket: Optional[str] = None,
    load_modules: Optional[List[str]] = None,
) -> None:
    registry = AgentRegistry(agents_dir)

    # M9 hook-aware module loading. Each --load-module is imported
    # AFTER the AgentRegistry is constructed so operational modules
    # (mod_cache, mod_audit, etc.) can register dispatch hooks via
    # their optional ``install(server_state)`` function. Modules
    # without ``install`` still load — they're custom-method modules
    # that register at import time, the older pattern.
    if load_modules:
        for mod_name in load_modules:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError as exc:
                print(
                    f"[server] failed to load module {mod_name!r}: {exc}",
                    file=sys.stderr,
                )
                continue
            installer = getattr(mod, "install", None)
            if callable(installer):
                try:
                    installer(registry)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[server] {mod_name}.install() raised "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                print(
                    f"[server] loaded operational module {mod_name} "
                    f"(install() ok)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[server] loaded module: {mod_name}",
                    file=sys.stderr,
                )

    # Load the Ed25519 signing service if configured. Operators who
    # enable signing in [signing] but lack a valid key file get a
    # clear boot-time refusal — silently falling back to "no
    # signatures" would be a security regression for a deployment
    # that asked for it.
    if config is not None and getattr(config, "signing", None) is not None:
        signing_cfg = config.signing
        if signing_cfg.enabled:
            from server.signing import KeyLoadError, SigningService
            try:
                registry.signing_service = SigningService.from_key_path(
                    signing_cfg.key_path, key_id=signing_cfg.key_id,
                )
                # Stash on config too so _finalize_response (which
                # only sees config) can sign Attribution-Record
                # without a separate thread-through.
                config.signing_service = registry.signing_service
                print(
                    f"[server] signing enabled (kid={registry.signing_service.key_id})",
                    file=sys.stderr,
                )
            except KeyLoadError as exc:
                print(
                    f"[server] signing is enabled in config but the key "
                    f"could not be loaded: {exc}",
                    file=sys.stderr,
                )
                return

    # M3 step (b): gateway mode is opt-in. When the operator passes
    # --gateway-socket, the daemon binds a Unix socket / TCP loopback
    # endpoint, accepts runtime-module connections, and routes
    # registered_function endpoints through the gateway instead of
    # importing them in-daemon. Composition / external_service / the
    # 12 embedded methods stay in-daemon regardless.
    gateway_server = None
    if gateway_socket:
        from server.gateway import GatewayServer
        gateway_server = GatewayServer(
            socket_path=gateway_socket,
            server_id=(
                config.server.server_id
                if config is not None and getattr(config, "server", None) is not None
                else ""
            ),
            daemon_version="agtpd",
            catalog_version="",
        )
        registry.gateway_server = gateway_server
        # Phase C: the gateway's optional capabilities pull from
        # daemon state. sign_request lights up when [signing] is
        # loaded; outbound_call defaults on (operators disable via
        # a future config flag when there's a reason to).
        gateway_server.signing_service = registry.signing_service
        print(
            f"[server] gateway mode enabled (socket={gateway_socket})",
            file=sys.stderr,
        )
    if not registry.agents:
        print(
            f"[server] WARNING: no agents loaded from {agents_dir}",
            file=sys.stderr,
        )

    if config is None:
        config = default_config(host)
    # The registry holds a config reference so the dispatcher and
    # PROPOSE handler can read synthesis durations, audit-log
    # routing, etc. without an extra plumbing argument.
    registry.config = config
    # Mirror the configured async-evaluation timeout into the
    # ProposalStore so 261 responses carry the correct deadline
    # bound and the sweep_expired pass uses the same value.
    try:
        from server.synthesis_duration import parse_duration
        registry.proposal_store.max_evaluation_seconds = parse_duration(
            config.synthesis.max_evaluation_duration
        )
    except (ValueError, AttributeError):  # pragma: no cover - defensive
        pass
    if config.is_default:
        print(
            f"[server] no {CONFIG_FILENAME} found; using default manifest "
            f"identity (server_id={config.server.server_id!r})",
            file=sys.stderr,
        )
    else:
        print(
            f"[server] manifest identity: {config.server.server_id} "
            f"(operator: {config.server.operator})"
        )

    # Configure the synthesis runtime per [synthesis] in the config.
    registry.configure_synthesis(config)

    # Per-§6 the method policy lives in the config object under
    # ``policies.methods``; no separate file to load.
    registry.configure_methods_policy(config.policy.methods)
    mp = config.policy.methods
    print(
        f"[server] method policy: "
        f"allow={'*' if mp.allow_all else len(mp.allow)}, "
        f"disallow={len(mp.disallow)}, "
        f"legacy={len(mp.legacy)}, "
        f"redirects={len(mp.redirects)}",
        file=sys.stderr,
    )

    # Phase-2 endpoint registry. Resolved relative to the config's
    # source path. ``--endpoints-dir`` overrides the default for
    # ad-hoc deployments.
    if endpoints_dir is None:
        endpoints_dir = (
            config.source_path.parent / DEFAULT_ENDPOINTS_DIR
            if config.source_path is not None
            else Path(DEFAULT_ENDPOINTS_DIR)
        )
    registry.configure_endpoints(endpoints_dir)

    # Server-internal built-ins (DISCOVER /methods exposing the
    # lightweight method+path inventory). Registered after operator
    # TOML so an operator can override a built-in's (method, path) by
    # declaring it themselves.
    registry.register_builtins()

    # Phase-6 catalog-evolution invalidation. If the catalog
    # changed since the last boot and an in-memory synthesis
    # references a removed verb, expire it cleanly here rather
    # than failing mid-execution at first traffic.
    if registry.synthesis_runtime is not None:
        expired = registry.synthesis_runtime.invalidate_against_catalog()
        if expired:
            print(
                f"[server] catalog-evolution invalidation expired "
                f"{len(expired)} synthesis/syntheses referencing "
                f"removed verbs",
                file=sys.stderr,
            )

    # Register gateway-bound endpoints AFTER configure_endpoints loaded
    # them. We iterate the endpoint registry rather than reaching into
    # endpoint_loader so each registered_function endpoint (with its
    # resolved schemas) is picked up exactly once.
    if gateway_server is not None:
        from server.schema_validation import (
            spec_to_input_schema, spec_to_output_schema,
        )
        gateway_count = 0
        for spec in registry.endpoint_registry.all_endpoints():
            if spec.handler is None or spec.handler.type != "registered_function":
                continue
            gateway_server.register_endpoint(
                spec,
                input_schema=spec_to_input_schema(spec),
                output_schema=spec_to_output_schema(spec),
            )
            gateway_count += 1
        print(
            f"[server] gateway will route {gateway_count} endpoint(s) "
            f"to connected runtime modules",
            file=sys.stderr,
        )
        gateway_server.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(64)

    # Build the cert verifier when mTLS is configured. The verifier
    # itself doesn't re-validate chains — the TLS library does that
    # when verify_mode is CERT_REQUIRED / CERT_OPTIONAL — but it
    # extracts the Ed25519 public key and derives Agent-ID from each
    # accepted connection's peer cert.
    cert_verifier = None
    if config is not None and getattr(config, "mtls", None) is not None:
        mtls_cfg = config.mtls
        if mtls_cfg.mode != "disabled":
            if not certfile or not keyfile:
                print(
                    f"[server] [mtls].mode = {mtls_cfg.mode!r} requires "
                    f"--cert and --key (TLS must be enabled)",
                    file=sys.stderr,
                )
                return
            if not mtls_cfg.ca_bundle_path:
                print(
                    f"[server] [mtls].mode = {mtls_cfg.mode!r} requires "
                    f"a ca_bundle_path",
                    file=sys.stderr,
                )
                return
            from server.mtls import CertVerifier
            cert_verifier = CertVerifier(ca_bundle_path=mtls_cfg.ca_bundle_path)
            registry.cert_verifier = cert_verifier
            print(
                f"[server] mTLS enabled "
                f"(mode={mtls_cfg.mode}, ca_bundle={mtls_cfg.ca_bundle_path})",
                file=sys.stderr,
            )

    if certfile and keyfile:
        from server.mtls import build_server_ssl_context
        mtls_cfg = (
            config.mtls
            if config is not None and getattr(config, "mtls", None) is not None
            else None
        )
        require_cert = mtls_cfg is not None and mtls_cfg.mode == "required"
        ca_bundle = mtls_cfg.ca_bundle_path if mtls_cfg and mtls_cfg.mode != "disabled" else None
        try:
            ctx = build_server_ssl_context(
                certfile=certfile,
                keyfile=keyfile,
                ca_bundle_path=ca_bundle,
                require_client_cert=require_cert,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[server] TLS context build failed: {exc}", file=sys.stderr)
            return
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
        if gateway_server is not None:
            gateway_server.stop()


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
    parser.add_argument(
        "--endpoints-dir",
        metavar="PATH",
        help=(
            f"Directory of *.toml endpoint declarations to load at "
            f"startup. Defaults to ./{DEFAULT_ENDPOINTS_DIR} "
            f"resolved relative to the config file (or cwd when no "
            f"config is loaded). Pass an empty string or a path "
            f"that doesn't exist to skip endpoint loading."
        ),
    )
    parser.add_argument(
        "--gateway-socket",
        metavar="PATH",
        help=(
            "Enable gateway mode (M3 step b). Binds a Unix-domain socket "
            "at PATH and accepts connections from runtime modules "
            "(mod_python, mod_php, ...). When set, registered_function "
            "endpoints are routed through the gateway instead of being "
            "imported in-daemon; composition and external_service "
            "bindings continue to resolve in-daemon. Pass 'host:port' "
            "instead of a path to use TCP loopback. See "
            "docs/architecture/gateway-protocol-v1.md."
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

    # Module loading now happens inside run() so the operational-module
    # install(server_state) convention has access to the registry.
    # See M9 hook-aware module loading in run().

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

    endpoints_path: Optional[Path] = None
    if args.endpoints_dir is not None:
        endpoints_path = normalize(args.endpoints_dir)

    # Gateway socket precedence: --gateway-socket flag wins; otherwise
    # fall back to [gateway].socket in the config. Empty means
    # gateway mode is off.
    gateway_socket = args.gateway_socket
    if not gateway_socket and config is not None:
        gateway_socket = (
            config.gateway.socket if getattr(config, "gateway", None) else ""
        ) or None

    run(
        args.host,
        port,
        agents_path,
        args.cert,
        args.key,
        config=config,
        soft_deny_enabled=not args.no_soft_deny,
        endpoints_dir=endpoints_path,
        gateway_socket=gateway_socket,
        load_modules=args.load_module,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
