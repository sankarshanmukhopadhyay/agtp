"""
Thin client helpers for an ANS server, built on
:func:`client.core_client.send_method`.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, Optional

from client.core_client import send_method
from core import wire


def _parse(response: wire.AGTPResponse) -> Dict[str, Any]:
    if not response.body_bytes:
        return {}
    try:
        return json.loads(response.body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def resolve(
    host: str, port: int, name: str, *,
    use_tls: bool = True, insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """RESOLVE a human-readable name to its Agent-ID binding."""
    q = urllib.parse.quote(name, safe="")
    return send_method(
        None, host, port, "RESOLVE", path=f"/?name={q}",
        use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
    )


def register(
    host: str, port: int, agent_id: str, name: str,
    manifest: Optional[Dict[str, Any]] = None, *,
    use_tls: bool = True, insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """REGISTER a name → Agent-ID binding with a manifest summary."""
    body = json.dumps({
        "agent_id": agent_id, "name": name, "manifest": manifest or {},
    }).encode("utf-8")
    return send_method(
        None, host, port, "REGISTER",
        body=body, body_content_type="application/json",
        use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
    )


def deregister(
    host: str, port: int, agent_id: str, *,
    use_tls: bool = True, insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """DEREGISTER an agent's binding."""
    body = json.dumps({"agent_id": agent_id}).encode("utf-8")
    return send_method(
        None, host, port, "DEREGISTER",
        body=body, body_content_type="application/json",
        use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
    )


def resolve_json(host: str, port: int, name: str, **kw) -> Dict[str, Any]:
    return _parse(resolve(host, port, name, **kw))


def register_json(host: str, port: int, agent_id: str, name: str,
                  manifest: Optional[Dict[str, Any]] = None, **kw) -> Dict[str, Any]:
    return _parse(register(host, port, agent_id, name, manifest, **kw))


def deregister_json(host: str, port: int, agent_id: str, **kw) -> Dict[str, Any]:
    return _parse(deregister(host, port, agent_id, **kw))
