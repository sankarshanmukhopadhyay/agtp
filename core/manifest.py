"""
Server Manifest dataclasses (wire shape only).

These types describe the JSON wire form that server-level DISCOVER
returns. They are intentionally generation-free: a server populates
them via ``server.manifest.generate()``; clients (and elemen) read
them straight from a parsed JSON response.

Wire media type: ``application/vnd.agtp.manifest+json``.

The wire shape mirrors ``agtp-api §7``:

  * Three version fields at the top level:
      - ``agtp_version``      — wire protocol speak (e.g., ``"1.0"``)
      - ``agtp_api_version``  — contract layer (endpoint primitive,
                                semantic block, status codes) the
                                server implements
      - ``document_version``  — this specific manifest's version,
                                bumped by the server on every change
  * Server identity under ``server``: ``server_id`` (canonical
    identifier — agtp:// URI or domain), ``domain`` (operational
    hosting target), ``operator`` (human-readable org), ``contact``,
    plus ``issued`` (server provisioning time) and ``updated`` (this
    manifest's last regeneration time).
  * ``embedded_methods`` and optionally ``custom_methods`` arrays —
    the 12 protocol primitives plus any server-defined methods
    registered through the legacy ``@method`` decorator.
  * ``endpoints`` — the Phase-1+ endpoint-registry contents.
  * ``hosted_agents`` array plus ``agent_disclosure`` enum
    (``"public" | "limited" | "private"``) and optional
    ``agent_disclosure_notice``.
  * Optional ``apis`` (resource-scoped admissible-method declarations)
    and ``hosted_protocols`` (non-AGTP protocols this server bridges).
  * ``policies`` block — operational toggles: ``wildcards_accepted``,
    ``anonymous_discovery``, ``scope_required_for_invocation``,
    ``synthesis_enabled``, ``max_synthesis_depth``.
  * ``catalog_version`` / ``catalog_versions_supported`` (Phase-6).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ServerInfoBlock:
    """Server identity declared in the manifest.

    Fields:

      * ``server_id`` — the canonical identifier. Either an
        ``agtp://`` URI with a hash component or a domain
        (``acme.tld``) for domain-anchored servers.
      * ``domain`` — operational hosting target (``agents.acme.com``
        or ``acme.com:4480``). Optional; servers whose ``server_id``
        is already a domain may omit this.
      * ``operator`` — human-readable organization name
        (``"Acme Corporation"``).
      * ``contact`` — operator contact handle (email, URL).
      * ``supported_features`` — list of named protocol features
        the server speaks (``"embedded-methods/12"``,
        ``"form2-server-uris"``, etc.).
      * ``issued`` — when this server first came online. Identity
        timestamp; generally does not change across manifest
        emissions.
      * ``updated`` — when this manifest was last regenerated.
        Changes whenever the server updates its endpoint set,
        policies, hosted agents, etc.
    """

    server_id: str
    operator: str
    contact: str
    supported_features: List[str] = field(default_factory=list)
    domain: Optional[str] = None
    issued: str = ""
    updated: str = ""


@dataclass
class PolicyBlock:
    """Operational toggles advertised in the manifest.

    Five top-level fields plus the ``methods`` sub-block from
    ``agtp-api §8``:

      * ``wildcards_accepted`` — whether agents with
        ``wildcards: true`` can invoke arbitrary verbs.
      * ``anonymous_discovery`` — whether unauthenticated clients
        can fetch the manifest.
      * ``scope_required_for_invocation`` — whether all invocations
        require valid scopes.
      * ``synthesis_enabled`` — whether PROPOSE is accepted. When
        false, the dispatcher refuses PROPOSE with reason
        ``synthesis-disabled``.
      * ``max_synthesis_depth`` — maximum plan-step count permitted
        when composing a synthesis. The runtime refuses deeper
        plans. Default ``10``.
      * ``methods`` — per-server method admission policy (allow /
        disallow / legacy / redirects). Rendered shape::

            {
              "allow": "*" | [...],
              "disallow": [...],
              "legacy":   [...],
              "redirects": [
                {"from_method": "...", "to_method": "..."},
                {"from_method": "...", "from_path": "...",
                 "to_method": "...", "to_path": "..."}
              ]
            }
    """

    wildcards_accepted: bool
    anonymous_discovery: bool
    scope_required_for_invocation: bool
    synthesis_enabled: bool = True
    max_synthesis_depth: int = 10
    methods: Optional[Dict[str, Any]] = None


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
    """Top-level manifest. ``to_json`` produces the wire form.

    Per ``agtp-api §7`` the wire shape is::

        {
          "agtp_version": "...",
          "agtp_api_version": "...",
          "document_version": "...",
          "catalog_version": "...",
          "catalog_versions_supported": [...],
          "server": {...},
          "embedded_methods": [...],
          "custom_methods": [...],         // optional
          "endpoints": [...],              // optional
          "agent_disclosure": "...",
          "hosted_agents": [...],
          "agent_disclosure_notice": "...", // optional
          "apis": [...],                   // optional
          "hosted_protocols": [...],       // optional
          "policies": {...}
        }
    """

    agtp_version: str
    agtp_api_version: str
    document_version: str
    server: ServerInfoBlock
    embedded_methods: List[Dict[str, Any]]
    agent_disclosure: str                              # "public" | "limited" | "private"
    hosted_agents: List[Dict[str, Any]] = field(default_factory=list)
    custom_methods: List[Dict[str, Any]] = field(default_factory=list)
    policies: Optional[PolicyBlock] = None
    # Machine-readable disclosure of the live deployment posture.
    # Additive and safe for older clients to ignore.
    assurance: Optional[Dict[str, Any]] = None
    security: Optional[Dict[str, Any]] = None
    apis: List[APIEndpoint] = field(default_factory=list)
    hosted_protocols: List[HostedProtocol] = field(default_factory=list)
    #: Phase-2 endpoint inventory: each entry is the
    #: registry-rendered ``(method, path)`` declaration with full
    #: input / output / errors / handler contract. Empty list means
    #: the server has no endpoint registry (Phase-1 servers, or
    #: deployments that haven't authored TOML yet).
    endpoints: List[Dict[str, Any]] = field(default_factory=list)
    agent_disclosure_notice: Optional[str] = None
    #: Phase-6 catalog version this server validates against (semver
    #: form, e.g. ``"1.0.0"``). Clients SHOULD compare this to their
    #: own catalog version on first DISCOVER; major-version
    #: mismatches mean the client is speaking a vocabulary the
    #: server doesn't share. Empty when the manifest predates
    #: catalog versioning (Phase-5 and earlier servers).
    catalog_version: str = ""
    #: Phase-6 list of catalog versions this server can validate
    #: against. Today this is exactly ``[catalog_version]``;
    #: multi-version support is future work but the field rides
    #: on the wire now so clients can read it without breaking
    #: when that capability lands.
    catalog_versions_supported: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        # Build by hand so the key order is stable. Empty optional
        # blocks (apis, hosted_protocols, endpoints, custom_methods)
        # are omitted from the wire shape so simple servers stay
        # terse.
        out: Dict[str, Any] = {
            "agtp_version": self.agtp_version,
            "agtp_api_version": self.agtp_api_version,
            "document_version": self.document_version,
        }
        if self.catalog_version:
            out["catalog_version"] = self.catalog_version
        if self.catalog_versions_supported:
            out["catalog_versions_supported"] = list(
                self.catalog_versions_supported
            )
        out["server"] = asdict(self.server)
        out["embedded_methods"] = list(self.embedded_methods)
        if self.custom_methods:
            out["custom_methods"] = list(self.custom_methods)
        if self.endpoints:
            out["endpoints"] = list(self.endpoints)
        out["agent_disclosure"] = self.agent_disclosure
        out["hosted_agents"] = list(self.hosted_agents)
        if self.agent_disclosure_notice:
            out["agent_disclosure_notice"] = self.agent_disclosure_notice
        if self.apis:
            out["apis"] = [a.to_dict() for a in self.apis]
        if self.hosted_protocols:
            out["hosted_protocols"] = [
                p.to_dict() for p in self.hosted_protocols
            ]
        if self.security is not None:
            out["security"] = dict(self.security)
        if self.assurance is not None:
            out["assurance"] = dict(self.assurance)
        if self.policies is not None:
            policies_dict = asdict(self.policies)
            # ``methods`` is the §8 sub-block; emit it only when
            # the operator has actually populated something
            # non-trivial. ``None`` or an empty dict means "no
            # explicit policy authored" — keep the wire shape terse.
            if policies_dict.get("methods") in (None, {}):
                policies_dict.pop("methods", None)
            out["policies"] = policies_dict
        return out

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))


__all__ = [
    "APIEndpoint",
    "HostedProtocol",
    "PolicyBlock",
    "ServerInfoBlock",
    "ServerManifest",
]
