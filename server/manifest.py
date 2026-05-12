"""
Server Manifest generation.

The manifest is the response to a server-level DISCOVER (no
Agent-ID header). It tells a client three things:

  1. Who is running this server (server.server_id / operator / contact).
  2. Which methods this server supports (embedded_methods + endpoints).
  3. Which agents the server hosts, subject to the disclosure policy.

The data shape lives in ``core.manifest``; this module is the
generator that fills the dataclasses from a server's loaded state.
The dataclasses are re-exported below so older imports such as
``from server.manifest import ServerManifest`` keep working.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Avoid a hard import cycle (server.endpoint_registry may import
# from core, but it isn't required for manifest generation when no
# registry is supplied).

from core.identity import AgentDocument, utc_now_iso
from core.manifest import (
    APIEndpoint,
    HostedProtocol,
    PolicyBlock,
    ServerInfoBlock,
    ServerManifest,
)
from server.config import ServerConfig
from server.methods import REGISTRY, MethodSpec, spec_to_dict


#: AGTP-API contract version this build implements. Bump alongside
#: the contract-layer spec (endpoint primitive shape, semantic block,
#: status-code vocabulary). Distinct from the wire protocol's
#: ``agtp_version`` and from a deployment's ``document_version``.
AGTP_API_VERSION: str = "1.0"


def _summarize_skills(skills: List[str], *, limit: int = 140) -> str:
    """
    Compact one-line skills summary for the hosted-agents entry.

    Joins the first sentence of each skill (everything up to the first
    period or newline) and truncates to ``limit`` chars.
    """
    snippets: List[str] = []
    for skill in skills:
        text = (skill or "").strip()
        if not text:
            continue
        first = text.split(".", 1)[0].split("\n", 1)[0].strip()
        if first:
            snippets.append(first)
    joined = ", ".join(snippets)
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "…"
    return joined


def _agent_entry(doc: AgentDocument) -> Dict[str, Any]:
    """Per-agent entry in the manifest's ``hosted_agents`` array."""
    return {
        "agent_id": doc.agent_id,
        "name": doc.name,
        "skills_summary": _summarize_skills(doc.skills),
        "methods_count": len(doc.requires.methods),
    }


def _disclosure_notice(level: str) -> Optional[str]:
    if level == "private":
        return (
            "This server hides its agent roster. Authenticate and "
            "request directly via DESCRIBE with a known Agent-ID."
        )
    if level == "limited":
        return (
            "This server lists publicly-disclosed agents only. "
            "Additional agents may be reachable via direct DESCRIBE."
        )
    return None


def _bucket_methods() -> Dict[str, List[Dict[str, Any]]]:
    """Split the legacy ``REGISTRY`` into embedded vs. custom buckets.

    Returns a dict with two keys: ``embedded`` (the 12 protocol
    primitives, identified by membership in
    :data:`core.methods.EMBEDDED_VERBS`) and ``custom`` (anything
    else registered via the ``@method`` decorator). Phase 1+
    deployments register custom behavior through endpoint TOMLs and
    the endpoint registry; ``custom`` stays an empty list for those
    servers.
    """
    from core.methods import EMBEDDED_VERBS
    embedded: List[Dict[str, Any]] = []
    custom: List[Dict[str, Any]] = []
    for spec in REGISTRY.values():
        entry = spec_to_dict(spec)
        if spec.name in EMBEDDED_VERBS:
            embedded.append(entry)
        else:
            custom.append(entry)
    embedded.sort(key=lambda e: e["name"])
    custom.sort(key=lambda e: e["name"])
    return {"embedded": embedded, "custom": custom}


def generate(
    config: ServerConfig,
    agents: Dict[str, AgentDocument],
    *,
    supported_features: Optional[List[str]] = None,
    apis: Optional[List[APIEndpoint]] = None,
    hosted_protocols: Optional[List[HostedProtocol]] = None,
    endpoint_registry: Optional[Any] = None,
) -> ServerManifest:
    """
    Build a Server Manifest from the server's loaded state.

    ``agents`` is the agent-id -> AgentDocument map kept by the server.
    The disclosure policy in ``config`` determines whether (and how)
    agent details flow into the manifest. ``apis`` and
    ``hosted_protocols`` come either from the server config or from
    explicit arguments; either way, an empty list is omitted from the
    wire form.

    ``endpoint_registry``, when supplied, is the
    :class:`server.endpoint_registry.EndpointRegistry` whose contents
    populate the manifest's ``endpoints`` array. The
    ``embedded_methods`` array still surfaces every embedded
    primitive registered through :data:`server.methods.REGISTRY`;
    ``custom_methods`` carries any legacy ``@method`` registrations.
    """
    buckets = _bucket_methods()

    disclosure = config.agents.disclosure
    if disclosure == "private":
        agent_list: List[Dict[str, Any]] = []
    elif disclosure == "limited":
        # The "limited" tier becomes meaningful when agents declare a
        # public/private flag of their own. For now it lists the same
        # agents as "public"; the agent_disclosure_notice tells callers.
        agent_list = [_agent_entry(doc) for doc in agents.values()]
    else:
        agent_list = [_agent_entry(doc) for doc in agents.values()]

    if apis is None:
        apis = list(config.apis or [])
    if hosted_protocols is None:
        hosted_protocols = list(config.hosted_protocols or [])

    endpoints_section: List[Dict[str, Any]] = []
    if endpoint_registry is not None:
        # The registry's render method emits the Phase-1 manifest
        # shape (method/path/input/output/errors/handler/...)
        # already; we just hand it to the dataclass.
        endpoints_section = list(endpoint_registry.render_manifest_section())

    # Phase-6 catalog version + supported list. Read from
    # ``core.methods`` at generate time so monkey-patched test
    # catalogs flow through cleanly.
    from core.methods import (
        catalog_version as _catalog_version,
        catalog_versions_supported as _catalog_versions_supported,
    )

    now = utc_now_iso()
    return ServerManifest(
        agtp_version="1.0",
        agtp_api_version=AGTP_API_VERSION,
        document_version="v2",
        server=ServerInfoBlock(
            server_id=config.server.server_id,
            domain=config.server.domain,
            operator=config.server.operator,
            contact=config.server.contact,
            supported_features=list(
                supported_features or _DEFAULT_SUPPORTED_FEATURES
            ),
            issued=config.server.issued or now,
            updated=now,
        ),
        embedded_methods=buckets["embedded"],
        custom_methods=buckets["custom"],
        endpoints=endpoints_section,
        agent_disclosure=disclosure,
        hosted_agents=agent_list,
        agent_disclosure_notice=_disclosure_notice(disclosure),
        policies=PolicyBlock(
            wildcards_accepted=config.policy.wildcards_accepted,
            anonymous_discovery=config.policy.anonymous_discovery,
            scope_required_for_invocation=(
                config.policy.scope_required_for_invocation
            ),
            synthesis_enabled=config.policy.synthesis_enabled,
            max_synthesis_depth=config.policy.max_synthesis_depth,
            # §8 method-policy sub-block. ``to_wire()`` renders the
            # MethodsPolicy as the manifest-shaped dict (allow /
            # disallow / legacy / redirects). ``None`` when the
            # operator hasn't authored an explicit policy, in which
            # case the manifest emit omits the sub-block entirely.
            methods=(
                config.policy.methods.to_wire()
                if config.policy.methods is not None
                else None
            ),
        ),
        apis=list(apis),
        hosted_protocols=list(hosted_protocols),
        catalog_version=_catalog_version(),
        catalog_versions_supported=_catalog_versions_supported(),
    )


_DEFAULT_SUPPORTED_FEATURES: List[str] = [
    "embedded-methods/12",
    "manifest/v2",
    "form2-server-uris",
]


__all__ = [
    # Re-exported dataclasses (canonical home is core.manifest)
    "APIEndpoint",
    "HostedProtocol",
    "PolicyBlock",
    "ServerInfoBlock",
    "ServerManifest",
    # Generation
    "AGTP_API_VERSION",
    "generate",
]
