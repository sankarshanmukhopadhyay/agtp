"""
Server configuration loaded from ``agtp-server.toml``.

The config declares the server's identity (issuer, operator, contact),
its policy posture (wildcards, anonymous discovery, scope enforcement),
and how openly it discloses the agents it hosts. This data feeds the
Server Manifest returned by server-level DISCOVER.

A missing config file is fine for local development. Defaults are
chosen so that ``python -m agtp.server 4480`` against an empty
directory produces a usable, public-disclosure manifest.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agtp._paths import normalize


CONFIG_FILENAME = "agtp-server.toml"

DISCLOSURE_LEVELS = {"public", "limited", "private"}


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
    )

    agents_block = data.get("agents", {})
    agents = AgentsConfig(
        disclosure=agents_block.get("disclosure", "public"),
    )

    return ServerConfig(
        server=server,
        policy=policy,
        agents=agents,
        source_path=candidate,
    )


__all__ = [
    "AgentsConfig",
    "DISCLOSURE_LEVELS",
    "ServerConfig",
    "ServerInfo",
    "ServerPolicy",
    "default_config",
    "load",
    "CONFIG_FILENAME",
]
