"""
Server Manifest dataclasses (wire shape only).

These types describe the JSON wire form that server-level DISCOVER
returns. They are intentionally generation-free: a server populates
them via ``server.manifest.generate()``; clients (and elemen) read
them straight from a parsed JSON response.

Wire media type: ``application/vnd.agtp.manifest+json``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


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
class APIEndpoint:
    """A resource path the server exposes, with its applicable methods."""

    path: str
    methods: List[str]
    description: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError(
                f"APIEndpoint.path must start with '/', got {self.path!r}"
            )
        self.methods = list(self.methods)
        if not self.methods:
            raise ValueError(
                f"APIEndpoint at {self.path!r} must list at least one method"
            )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": self.path, "methods": list(self.methods)}
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class HostedProtocol:
    """A non-AGTP protocol the server bridges (MCP, OpenAPI, GraphQL, ...)."""

    protocol: str
    version: str
    endpoint: str
    catalog: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.protocol:
            raise ValueError("HostedProtocol.protocol is required")
        if not self.endpoint:
            raise ValueError(
                f"HostedProtocol {self.protocol!r} requires an endpoint"
            )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "protocol": self.protocol,
            "version": self.version,
            "endpoint": self.endpoint,
        }
        if self.catalog:
            out["catalog"] = self.catalog
        return out


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
    apis: List[APIEndpoint] = field(default_factory=list)
    hosts_protocols: List[HostedProtocol] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        # Build by hand so the key order is stable. Empty `apis` and
        # `hosts_protocols` are omitted from the wire shape so simple
        # servers stay terse.
        agents_block: Dict[str, Any] = {
            "disclosure": self.agents.disclosure,
            "list": self.agents.list,
        }
        if self.agents.notice is not None:
            agents_block["notice"] = self.agents.notice
        out: Dict[str, Any] = {
            "agtp_version": self.agtp_version,
            "document_version": self.document_version,
            "issued_at": self.issued_at,
            "server": asdict(self.server),
            "methods": asdict(self.methods),
            "agents": agents_block,
            "policy": asdict(self.policy),
        }
        if self.apis:
            out["apis"] = [a.to_dict() for a in self.apis]
        if self.hosts_protocols:
            out["hosts_protocols"] = [
                p.to_dict() for p in self.hosts_protocols
            ]
        return out

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))


__all__ = [
    "APIEndpoint",
    "AgentDisclosure",
    "HostedProtocol",
    "MethodsInventory",
    "PolicyBlock",
    "ServerInfoBlock",
    "ServerManifest",
]
