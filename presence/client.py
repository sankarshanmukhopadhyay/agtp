"""
Thin client helpers for the presence coordinator.

Built on :func:`client.core_client.send_method`, so they do no wire work
of their own. Each returns the parsed JSON body as a dict (or the raw
:class:`wire.AGTPResponse` via ``return_response=True``).

These target a coordinator (a ``python -m server --presence`` process) and
speak the same AGTP wire as any other client. In M1 the demo runs
plaintext on loopback (``use_tls=False``); production deployments use TLS
like the rest of AGTP.
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


def announce_signed_record(
    host: str,
    port: int,
    record,
    signing_service,
    *,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """
    Sign ``record`` with the announcing agent's key and submit it as a
    signed foreign ANNOUNCE. The coordinator verifies the signature (and,
    where it can resolve the agent's Genesis, the key→Agent-ID binding)
    before accepting — so a non-hosted agent can announce without the
    coordinator being able to forge or mutate the record.
    """
    from presence.recordsig import sign_record

    sign_record(record, signing_service)
    body = json.dumps({"record": record.to_gossip_dict()}).encode("utf-8")
    return send_method(
        None, host, port, "ANNOUNCE",
        body=body, body_content_type="application/json",
        use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
    )


def announce(
    host: str,
    port: int,
    agent_id: str,
    *,
    visibility: Optional[Dict[str, str]] = None,
    ttl_seconds: Optional[int] = None,
    presence_mode: Optional[str] = None,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """
    ANNOUNCE ``agent_id`` to the coordinator (idempotent).

    ``visibility`` (a ``{presence_mode, disclosure_mode, audience_scope}``
    dict) and ``ttl_seconds`` ride the body; ``presence_mode`` rides the
    ``Presence-Mode`` header (runtime reduction within the cert envelope).
    """
    body: Dict[str, Any] = {}
    if visibility is not None:
        body["visibility"] = visibility
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    body_bytes = json.dumps(body).encode("utf-8") if body else b""
    extra = {"Presence-Mode": presence_mode} if presence_mode else None
    return send_method(
        agent_id,  # subject rides the Agent-ID header
        host,
        port,
        "ANNOUNCE",
        body=body_bytes,
        body_content_type="application/json" if body_bytes else None,
        extra_headers=extra,
        use_tls=use_tls,
        insecure_skip_verify=insecure_skip_verify,
    )


def withdraw(
    host: str,
    port: int,
    agent_id: str,
    *,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """WITHDRAW ``agent_id`` from the coordinator."""
    return send_method(
        agent_id,
        host,
        port,
        "WITHDRAW",
        use_tls=use_tls,
        insecure_skip_verify=insecure_skip_verify,
    )


def probe(
    host: str,
    port: int,
    agent_id: str,
    *,
    as_agent: Optional[str] = None,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """
    PROBE the coordinator for ``agent_id`` liveness and posture.

    ``as_agent`` sets the caller's own identity (the ``Agent-Identity``
    header) so the coordinator can evaluate visibility; without it the
    probe is anonymous and sees only public/full-audience agents.
    """
    extra = {"Agent-Identity": as_agent} if as_agent else None
    return send_method(
        agent_id,
        host,
        port,
        "PROBE",
        extra_headers=extra,
        use_tls=use_tls,
        insecure_skip_verify=insecure_skip_verify,
    )


def discover_population(
    host: str,
    port: int,
    *,
    capability: Optional[str] = None,
    limit: Optional[int] = None,
    as_agent: Optional[str] = None,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    """
    DISCOVER /population against the coordinator, optionally filtered by
    ``capability``. Server-level (no Agent-ID header): the coordinator's
    presence hook answers from the visible population.

    ``as_agent`` sets the caller identity (``Agent-Identity`` header) so
    visibility scoping applies; without it the query is anonymous.
    """
    query: Dict[str, str] = {}
    if capability is not None:
        query["capability"] = capability
    if limit is not None:
        query["limit"] = str(limit)
    path = "/population"
    if query:
        path = path + "?" + "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in query.items()
        )
    extra = {"Agent-Identity": as_agent} if as_agent else None
    return send_method(
        None,  # server-level
        host,
        port,
        "DISCOVER",
        path=path,
        extra_headers=extra,
        use_tls=use_tls,
        insecure_skip_verify=insecure_skip_verify,
    )


# Dict-returning convenience wrappers -------------------------------------


def announce_json(host: str, port: int, agent_id: str, **kw) -> Dict[str, Any]:
    return _parse(announce(host, port, agent_id, **kw))


def probe_json(host: str, port: int, agent_id: str, **kw) -> Dict[str, Any]:
    return _parse(probe(host, port, agent_id, **kw))


def withdraw_json(host: str, port: int, agent_id: str, **kw) -> Dict[str, Any]:
    return _parse(withdraw(host, port, agent_id, **kw))


def discover_population_json(host: str, port: int, **kw) -> Dict[str, Any]:
    return _parse(discover_population(host, port, **kw))


def verify_discover_response(body: Dict[str, Any], public_key) -> bool:
    """
    Verify the ``ans_signature`` on a DISCOVER response against the
    responder's Ed25519 public key. Returns False if the response is
    unsigned or the signature does not cover the returned results — a
    conforming requester rejects such responses in a signed deployment.
    """
    from presence.envelope import verify_result_set

    results = body.get("results")
    if not isinstance(results, list):
        return False
    return verify_result_set(public_key, results, body.get("ans_signature"))
