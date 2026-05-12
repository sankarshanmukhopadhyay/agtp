"""
Agent Document — the canonical identity record served by AGTP agents.

Reference: draft-hood-independent-agtp (v07 draft) plus the Interaction
Model design note. The Agent Document is the protocol's authoritative
representation of an agent's identity, the skills it offers, and the
methods it requires from peer agents and infrastructure.

Schema versions
---------------
``document_version: "v2"`` is the current schema (this file). The v2
shape replaces v1's ``capabilities`` field with two complementary
declarations:

  * ``skills``   - human-readable prose describing what the agent does.
  * ``requires`` - structured needs: methods it consumes, scopes it
                   needs, and a wildcards flag for orchestrators that
                   accept any method.

v1 documents continue to load. ``from_dict`` detects the older shape
and routes to ``from_dict_v1_compat``, which lifts ``capabilities``
into ``requires.methods`` and seeds ``skills`` from the description.
A migrated document carries ``document_version="v1-migrated"`` so
operators can choose to rewrite the source file.

Media types
-----------
    application/vnd.agtp.identity+json    canonical wire format
    application/vnd.agtp.identity+yaml    human-editable form
    application/vnd.agtp.manifest+json    server-level manifest
                                          (returned by DISCOVER without
                                           an Agent-ID header)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


CONTENT_TYPE_JSON = "application/vnd.agtp.identity+json"
CONTENT_TYPE_YAML = "application/vnd.agtp.identity+yaml"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"
CONTENT_TYPE_MANIFEST_JSON = "application/vnd.agtp.manifest+json"

DOCUMENT_VERSION_V2 = "v2"
DOCUMENT_VERSION_V1_MIGRATED = "v1-migrated"


# Canonical key order for serialization. Wire format follows this
# ordering so agent.json files are byte-stable across implementations.
FIELD_ORDER = [
    "agtp_version",
    "document_version",
    "agent_id",
    "name",
    "principal",
    "principal_id",
    "description",
    "status",
    "skills",
    "requires",
    "scopes_accepted",
    "issued_at",
    "issuer",
]


@dataclass
class RequiresDeclaration:
    """
    Methods, scopes, and wildcard policy the agent declares as
    inbound-handleable. ``methods`` is the dispatch surface; ``scopes``
    list the authority tokens the agent expects to be presented;
    ``wildcards`` is true for orchestrators that accept any method.
    """

    methods: List[str] = field(default_factory=list)
    scopes: List[str] = field(default_factory=list)
    wildcards: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "methods": list(self.methods),
            "scopes": list(self.scopes),
            "wildcards": bool(self.wildcards),
        }


@dataclass
class AgentDocument:
    """The v2 Agent Document."""

    agtp_version: str
    agent_id: str
    name: str
    principal: str
    principal_id: str
    description: str
    status: str                # "active" | "suspended" | "retired"
    skills: List[str]
    requires: RequiresDeclaration
    scopes_accepted: List[str]
    issued_at: str             # ISO 8601 UTC
    issuer: str
    document_version: str = DOCUMENT_VERSION_V2

    @property
    def is_migrated(self) -> bool:
        """True for documents auto-converted from v1 at load time."""
        return self.document_version == DOCUMENT_VERSION_V1_MIGRATED

    @property
    def capabilities(self) -> List[str]:
        """
        Backward-compatible alias for ``requires.methods``. Older code
        paths and downstream tools that read ``.capabilities`` continue
        to work; new code prefers ``requires.methods``.
        """
        return list(self.requires.methods)

    def accepts_method(self, method_name: str) -> bool:
        """
        True when this agent will dispatch ``method_name`` inbound.

        Wildcard agents accept anything. Strict agents accept only
        methods listed in ``requires.methods``.
        """
        if self.requires.wildcards:
            return True
        return method_name.upper() in {m.upper() for m in self.requires.methods}

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict in canonical field order. Always emits v2."""
        raw = asdict(self)
        # asdict converts the nested dataclass; rewrite using
        # RequiresDeclaration.to_dict for stable key order.
        raw["requires"] = self.requires.to_dict()
        out: Dict[str, Any] = {}
        for key in FIELD_ORDER:
            if key == "document_version":
                # Migrated documents are emitted as clean v2.
                out[key] = (
                    DOCUMENT_VERSION_V2
                    if self.is_migrated
                    else self.document_version
                )
            else:
                out[key] = raw[key]
        return out

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def to_yaml(self) -> str:
        """
        Compact YAML emitter (avoids the PyYAML dependency). Handles
        the nested ``requires`` mapping.
        """
        d = self.to_dict()
        lines: List[str] = []
        for key in FIELD_ORDER:
            value = d[key]
            if key == "requires":
                lines.append("requires:")
                lines.append(f"  methods: {_yaml_inline_list(value['methods'])}")
                lines.append(f"  scopes:  {_yaml_inline_list(value['scopes'])}")
                lines.append(f"  wildcards: {str(bool(value['wildcards'])).lower()}")
                continue
            if isinstance(value, list):
                if not value:
                    lines.append(f"{key}: []")
                else:
                    lines.append(f"{key}:")
                    for item in value:
                        lines.append(f"  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{key}: {_yaml_scalar(value)}")
        return "\n".join(lines) + "\n"


def _yaml_inline_list(items: List[Any]) -> str:
    if not items:
        return "[]"
    rendered = ", ".join(_yaml_scalar(i) for i in items)
    return f"[{rendered}]"


def _yaml_scalar(value: Any) -> str:
    """Emit a YAML scalar with appropriate quoting."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return '""'
    needs_quoting = (
        ":" in text
        or "," in text
        or text != text.strip()
        or text[0] in "!&*[{|>%@`"
        or text.lower() in ("yes", "no", "true", "false", "null")
    )
    if needs_quoting:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


_V2_REQUIRED_KEYS = {
    "agtp_version", "agent_id", "name", "principal", "principal_id",
    "description", "status", "skills", "requires", "scopes_accepted",
    "issued_at", "issuer",
}

_V1_SHAPE_KEYS = {"capabilities"}


def is_v1_document(data: Dict[str, Any]) -> bool:
    """Heuristic: v1 documents have ``capabilities`` and lack v2 fields."""
    if "capabilities" not in data:
        return False
    if "skills" in data and "requires" in data:
        return False
    return True


def _seed_skills_from_description(description: str) -> List[str]:
    """v1 didn't carry a skills array, so we synthesize a single entry."""
    description = (description or "").strip()
    if not description:
        return []
    return [description]


def from_dict(data: Dict[str, Any]) -> AgentDocument:
    """
    Construct an AgentDocument from parsed JSON.

    Detects v1 vs v2 by shape and dispatches. v2 documents are loaded
    as-is. v1 documents go through ``from_dict_v1_compat`` and emerge
    as in-memory v2 with ``document_version="v1-migrated"``.
    """
    if is_v1_document(data):
        return from_dict_v1_compat(data)

    missing = sorted(_V2_REQUIRED_KEYS - set(data.keys()))
    if missing:
        raise ValueError(
            f"Agent Document missing required fields: {', '.join(missing)}"
        )

    requires_block = data["requires"]
    if not isinstance(requires_block, dict):
        raise ValueError("'requires' must be a mapping with methods/scopes/wildcards")

    requires = RequiresDeclaration(
        methods=list(requires_block.get("methods", [])),
        scopes=list(requires_block.get("scopes", [])),
        wildcards=bool(requires_block.get("wildcards", False)),
    )

    return AgentDocument(
        agtp_version=str(data["agtp_version"]),
        document_version=str(data.get("document_version", DOCUMENT_VERSION_V2)),
        agent_id=str(data["agent_id"]),
        name=str(data["name"]),
        principal=str(data["principal"]),
        principal_id=str(data["principal_id"]),
        description=str(data["description"]),
        status=str(data["status"]),
        skills=list(data["skills"]),
        requires=requires,
        scopes_accepted=list(data["scopes_accepted"]),
        issued_at=str(data["issued_at"]),
        issuer=str(data["issuer"]),
    )


def from_dict_v1_compat(data: Dict[str, Any]) -> AgentDocument:
    """
    Convert a v1 Agent Document dict into the v2 in-memory shape.

    Mapping:
      capabilities -> requires.methods
      <none>       -> skills (seeded from description)
      <none>       -> requires.scopes (empty)
      <none>       -> requires.wildcards (false)

    The resulting document carries ``document_version="v1-migrated"``
    so callers can warn the operator that the source file is older.
    """
    legacy_required = {
        "agtp_version", "agent_id", "name", "principal", "principal_id",
        "description", "status", "capabilities", "scopes_accepted",
        "issued_at", "issuer",
    }
    missing = sorted(legacy_required - set(data.keys()))
    if missing:
        raise ValueError(
            f"v1 Agent Document missing required fields: {', '.join(missing)}"
        )

    requires = RequiresDeclaration(
        methods=list(data.get("capabilities", [])),
        scopes=[],
        wildcards=False,
    )
    skills = _seed_skills_from_description(data.get("description", ""))

    return AgentDocument(
        agtp_version=str(data["agtp_version"]),
        document_version=DOCUMENT_VERSION_V1_MIGRATED,
        agent_id=str(data["agent_id"]),
        name=str(data["name"]),
        principal=str(data["principal"]),
        principal_id=str(data["principal_id"]),
        description=str(data["description"]),
        status=str(data["status"]),
        skills=skills,
        requires=requires,
        scopes_accepted=list(data["scopes_accepted"]),
        issued_at=str(data["issued_at"]),
        issuer=str(data["issuer"]),
    )


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CONTENT_TYPE_JSON",
    "CONTENT_TYPE_YAML",
    "CONTENT_TYPE_HTML",
    "CONTENT_TYPE_MANIFEST_JSON",
    "DOCUMENT_VERSION_V1_MIGRATED",
    "DOCUMENT_VERSION_V2",
    "FIELD_ORDER",
    "AgentDocument",
    "RequiresDeclaration",
    "from_dict",
    "from_dict_v1_compat",
    "is_v1_document",
    "utc_now_iso",
]
