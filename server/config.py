"""
Server configuration loaded from ``agtp-server.toml``.

The config declares the server's identity (issuer, operator, contact),
its policy posture (wildcards, anonymous discovery, scope enforcement),
and how openly it discloses the agents it hosts. This data feeds the
Server Manifest returned by server-level DISCOVER.

A missing config file is fine for local development. Defaults are
chosen so that ``python -m server 4480`` against an empty
directory produces a usable, public-disclosure manifest.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from core._paths import normalize


CONFIG_FILENAME = "agtp-server.toml"

DISCLOSURE_LEVELS = {"public", "limited", "private"}


def _load_apis(blocks: list) -> list:
    """Convert raw TOML [[apis]] tables into APIEndpoint objects."""
    from server.manifest import APIEndpoint
    out = []
    for block in blocks:
        out.append(APIEndpoint(
            path=str(block.get("path", "")),
            methods=list(block.get("methods", [])),
            description=block.get("description"),
        ))
    return out


def _load_hosts_protocols(blocks: list) -> list:
    """Convert raw TOML [[hosts_protocols]] tables into HostedProtocol."""
    from server.manifest import HostedProtocol
    out = []
    for block in blocks:
        out.append(HostedProtocol(
            protocol=str(block.get("protocol", "")),
            version=str(block.get("version", "")),
            endpoint=str(block.get("endpoint", "")),
            catalog=block.get("catalog"),
        ))
    return out


@dataclass
class ServerInfo:
    """Identity declared by the server in its manifest."""

    issuer: str
    operator: str
    contact: str
    amg_version: str = "1.0"


@dataclass
class ServerPolicy:
    """Operational policy advertised in the manifest."""

    wildcards_accepted: bool = True
    anonymous_discovery: bool = True
    scope_required_for_invocation: bool = True
    negotiable: bool = True


@dataclass
class SynthesisConfig:
    """
    PROPOSE-time composition policy configuration.

    ``policies`` lists composition strategies in evaluation order.
    Today only ``"recipes"`` is shipping; future deployments may add
    ``"graph"`` or ``"llm"`` once those policies land. The runtime
    always appends a final ``"passthrough"`` fallback so the v1
    accept-on-exact-match behavior is preserved.

    ``recipes_file`` resolves relative to the server's working
    directory if the path is not absolute.
    """

    policies: List[str] = field(default_factory=lambda: ["recipes"])
    recipes_file: str = "agtp-recipes.toml"


@dataclass
class AgentsConfig:
    """How openly the server lists the agents it hosts."""

    disclosure: str = "public"

    def __post_init__(self) -> None:
        if self.disclosure not in DISCLOSURE_LEVELS:
            raise ValueError(
                f"agents.disclosure must be one of {sorted(DISCLOSURE_LEVELS)}, "
                f"got {self.disclosure!r}"
            )


@dataclass
class ServerConfig:
    """Top-level configuration object."""

    server: ServerInfo
    policy: ServerPolicy = field(default_factory=ServerPolicy)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)
    apis: list = field(default_factory=list)
    hosts_protocols: list = field(default_factory=list)
    source_path: Optional[Path] = None

    @property
    def is_default(self) -> bool:
        return self.source_path is None


def _default_issuer(host: Optional[str]) -> str:
    """Pick a reasonable issuer string for a missing config."""
    if host and host not in ("0.0.0.0", "::", ""):
        return host
    return "localhost"


def default_config(host: Optional[str] = None) -> ServerConfig:
    """Construct a sensible default config when no file is present."""
    return ServerConfig(
        server=ServerInfo(
            issuer=_default_issuer(host),
            operator="local development",
            contact="",
            amg_version="1.0",
        ),
        policy=ServerPolicy(),
        agents=AgentsConfig(disclosure="public"),
        source_path=None,
    )


def load(path: Optional[Path], *, host: Optional[str] = None) -> ServerConfig:
    """
    Load a TOML config from ``path`` if given, else look for
    ``agtp-server.toml`` in the current working directory. Falls back
    to ``default_config(host)`` when no file exists.
    """
    candidate = (
        normalize(path) if path is not None
        else (Path.cwd() / CONFIG_FILENAME).resolve()
    )

    if not candidate.exists():
        if path is not None:
            raise FileNotFoundError(f"config file not found: {candidate}")
        return default_config(host)

    with candidate.open("rb") as f:
        data = tomllib.load(f)

    server_block = data.get("server", {})
    if not server_block.get("issuer"):
        raise ValueError(
            f"{candidate}: [server].issuer is required when a config file "
            f"is present"
        )

    server = ServerInfo(
        issuer=server_block["issuer"],
        operator=server_block.get("operator", "unspecified"),
        contact=server_block.get("contact", ""),
        amg_version=server_block.get("amg_version", "1.0"),
    )

    policy_block = data.get("policy", {})
    policy = ServerPolicy(
        wildcards_accepted=bool(policy_block.get("wildcards_accepted", True)),
        anonymous_discovery=bool(
            policy_block.get("anonymous_discovery", True)
        ),
        scope_required_for_invocation=bool(
            policy_block.get("scope_required_for_invocation", True)
        ),
        negotiable=bool(policy_block.get("negotiable", True)),
    )

    agents_block = data.get("agents", {})
    agents = AgentsConfig(
        disclosure=agents_block.get("disclosure", "public"),
    )

    synthesis_block = data.get("synthesis", {})
    synthesis = SynthesisConfig(
        policies=list(synthesis_block.get("policies") or ["recipes"]),
        recipes_file=str(synthesis_block.get("recipes_file") or "agtp-recipes.toml"),
    )

    apis = _load_apis(data.get("apis", []) or [])
    hosts_protocols = _load_hosts_protocols(data.get("hosts_protocols", []) or [])

    return ServerConfig(
        server=server,
        policy=policy,
        synthesis=synthesis,
        agents=agents,
        apis=apis,
        hosts_protocols=hosts_protocols,
        source_path=candidate,
    )


__all__ = [
    "AgentsConfig",
    "DISCLOSURE_LEVELS",
    "ServerConfig",
    "ServerInfo",
    "ServerPolicy",
    "SynthesisConfig",
    "default_config",
    "load",
    "CONFIG_FILENAME",
]
