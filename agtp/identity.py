"""
Agent Document — the canonical identity record served by AGTP agents.

Reference: draft-hood-independent-agtp (v07 draft). The Agent Document is
the protocol's authoritative representation of an agent's identity and
capabilities. It is served in response to bare-URI lookups
(`agtp://{agent-id}`) and via the DESCRIBE method.

Media types:
    application/vnd.agtp.identity+json    canonical wire format
    application/vnd.agtp.identity+yaml    human-editable form

The eleven fields in this v1 schema are deliberately minimal. Future
revisions will add: signature, trust_score, certificate_chain,
delegation_policy, and audit_endpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional


CONTENT_TYPE_JSON = "application/vnd.agtp.identity+json"
CONTENT_TYPE_YAML = "application/vnd.agtp.identity+yaml"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"

# Field ordering for canonical serialization. Wire format uses this order
# so agent.json files are byte-stable across implementations.
FIELD_ORDER = [
    "agtp_version",
    "agent_id",
    "name",
    "principal",
    "principal_id",
    "description",
    "status",
    "capabilities",
    "scopes_accepted",
    "issued_at",
    "issuer",
]


@dataclass
class AgentDocument:
    """The eleven-field v1 Agent Document."""

    agtp_version: str
    agent_id: str
    name: str
    principal: str
    principal_id: str
    description: str
    status: str  # "active" | "suspended" | "retired"
    capabilities: List[str]
    scopes_accepted: List[str]
    issued_at: str  # ISO 8601 UTC
    issuer: str

    def to_dict(self) -> dict:
        """Return a dict in canonical field order."""
        raw = asdict(self)
        return {k: raw[k] for k in FIELD_ORDER}

    def to_json(self, *, pretty: bool = True) -> str:
        """Serialize to JSON in canonical field order."""
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def to_yaml(self) -> str:
        """
        Serialize to YAML. We avoid the PyYAML dependency by writing a
        small dedicated emitter; the schema is shallow and well-known.
        """
        lines = []
        for key in FIELD_ORDER:
            value = getattr(self, key)
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


def _yaml_scalar(value) -> str:
    """Emit a YAML scalar with appropriate quoting."""
    if value is None:
        return "null"
    text = str(value)
    if not text:
        return '""'
    needs_quoting = (
        ":" in text
        or text != text.strip()
        or text[0] in "!&*[{|>%@`"
        or text.lower() in ("yes", "no", "true", "false", "null")
    )
    if needs_quoting:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def from_dict(data: dict) -> AgentDocument:
    """
    Construct an AgentDocument from a parsed dict (e.g., loaded JSON).
    Raises ValueError if any required field is missing.
    """
    missing = [f for f in FIELD_ORDER if f not in data]
    if missing:
        raise ValueError(
            f"Agent Document missing required fields: {', '.join(missing)}"
        )
    return AgentDocument(
        agtp_version=data["agtp_version"],
        agent_id=data["agent_id"],
        name=data["name"],
        principal=data["principal"],
        principal_id=data["principal_id"],
        description=data["description"],
        status=data["status"],
        capabilities=list(data["capabilities"]),
        scopes_accepted=list(data["scopes_accepted"]),
        issued_at=data["issued_at"],
        issuer=data["issuer"],
    )


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
