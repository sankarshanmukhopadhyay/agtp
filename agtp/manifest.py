"""
Server Manifest generation.

The manifest is the response to a server-level DISCOVER (no
Target-Agent header). It tells a client three things:

  1. Who is running this server (server.issuer / operator / contact).
  2. Which methods this server supports (methods.embedded + custom).
  3. Which agents the server hosts, subject to the disclosure policy.

Wire format: ``application/vnd.agtp.manifest+json``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from agtp.config import ServerConfig
from agtp.identity import AgentDocument, utc_now_iso
from agtp.methods import REGISTRY, MethodSpec, spec_to_dict


@dataclass
class ServerInfoBlock:
    issuer: str
    operator: str
    contact: str
    amg_version: str
    supported_features: List[str]


@dataclass
class MethodsInventory:
    embedded: List[Dict[str, Any]]
    custom: List[Dict[str, Any]]
    summary: Dict[str, int]


@dataclass
class AgentDisclosure:
    disclosure: str            # "public" | "limited" | "private"
    list: List[Dict[str, Any]]
    notice: Optional[str] = None


@dataclass
class PolicyBlock:
    wildcards_accepted: bool
    anonymous_discovery: bool
    scope_required_for_invocation: bool


@dataclass
class ServerManifest:
    """Top-level manifest. ``to_json`` produces the wire form."""

    agtp_version: str
    document_version: str
    issued_at: str
    server: ServerInfoBlock
    methods: MethodsInventory
    agents: AgentDisclosure
    policy: PolicyBlock

    def to_dict(self) -> Dict[str, Any]:
        # Build by hand so the key order is stable (asdict on nested
        # dataclasses preserves insertion order, but explicit is safer).
        agents_block: Dict[str, Any] = {
            "disclosure": self.agents.disclosure,
            "list": self.agents.list,
        }
        if self.agents.notice is not None:
            agents_block["notice"] = self.agents.notice
        return {
            "agtp_version": self.agtp_version,
            "document_version": self.document_version,
            "issued_at": self.issued_at,
            "server": asdict(self.server),
            "methods": asdict(self.methods),
            "agents": agents_block,
            "policy": asdict(self.policy),
        }

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))


def _summarize_skills(skills: List[str], *, limit: int = 140) -> str:
    """
    Compact one-line skills summary for the agents.list entry.

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
    """Per-agent entry in the manifest's agents.list."""
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
            "request directly via DESCRIBE with a known Target-Agent."
        )
    if level == "limited":
        return (
            "This server lists publicly-disclosed agents only. "
            "Additional agents may be reachable via direct DESCRIBE."
        )
    return None


def _bucket_methods() -> MethodsInventory:
    embedded: List[Dict[str, Any]] = []
    custom: List[Dict[str, Any]] = []
    for spec in REGISTRY.values():
        entry = spec_to_dict(spec)
        if spec.source == "agtp/1.0":
            embedded.append(entry)
        else:
            custom.append(entry)
    embedded.sort(key=lambda e: e["name"])
    custom.sort(key=lambda e: e["name"])
    return MethodsInventory(
        embedded=embedded,
        custom=custom,
        summary={
            "embedded_count": len(embedded),
            "custom_count": len(custom),
            "total": len(embedded) + len(custom),
        },
    )


def generate(
    config: ServerConfig,
    agents: Dict[str, AgentDocument],
    *,
    supported_features: Optional[List[str]] = None,
) -> ServerManifest:
    """
    Build a Server Manifest from the server's loaded state.

    ``agents`` is the agent-id -> AgentDocument map kept by the server.
    The disclosure policy in ``config`` determines whether (and how)
    agent details flow into the manifest.
    """
    methods = _bucket_methods()

    disclosure = config.agents.disclosure
    if disclosure == "private":
        agent_list: List[Dict[str, Any]] = []
    elif disclosure == "limited":
        # The "limited" tier becomes meaningful when agents declare a
        # public/private flag of their own. For now it lists the same
        # agents as "public"; the disclosure_notice tells callers.
        agent_list = [_agent_entry(doc) for doc in agents.values()]
    else:
        agent_list = [_agent_entry(doc) for doc in agents.values()]

    return ServerManifest(
        agtp_version="1.0",
        document_version="v2",
        issued_at=utc_now_iso(),
        server=ServerInfoBlock(
            issuer=config.server.issuer,
            operator=config.server.operator,
            contact=config.server.contact,
            amg_version=config.server.amg_version,
            supported_features=list(
                supported_features or _DEFAULT_SUPPORTED_FEATURES
            ),
        ),
        methods=methods,
        agents=AgentDisclosure(
            disclosure=disclosure,
            list=agent_list,
            notice=_disclosure_notice(disclosure),
        ),
        policy=PolicyBlock(
            wildcards_accepted=config.policy.wildcards_accepted,
            anonymous_discovery=config.policy.anonymous_discovery,
            scope_required_for_invocation=(
                config.policy.scope_required_for_invocation
            ),
        ),
    )


_DEFAULT_SUPPORTED_FEATURES: List[str] = [
    "embedded-methods/12",
    "manifest/v2",
    "amg-custom-methods",
    "form2-server-uris",
]


__all__ = [
    "AgentDisclosure",
    "MethodsInventory",
    "PolicyBlock",
    "ServerInfoBlock",
    "ServerManifest",
    "generate",
]
