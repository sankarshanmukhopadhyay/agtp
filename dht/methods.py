"""
DHT wire methods: LOCATE (Kademlia FIND_NODE) and PING.

Dispatched by the AGTP server only when a :class:`dht.kademlia.KademliaNode`
is attached (``registry.dht_node``). :func:`maybe_handle_dht` is the single
entry point the connection handler calls.

  * ``LOCATE`` — body ``{key, node?}``: return the k nodes this coordinator
    knows closest to ``key``. The optional ``node`` is the requester's own
    :class:`NodeInfo`, observed to keep routing tables fresh (this is what
    makes convergence work — peers learn of requesters as queries flow).
  * ``PING``   — liveness + identity: return this node's id and endpoint, so
    a joining node can learn a seed's node id before adding it to its table.
"""

from __future__ import annotations

from typing import Any, Optional

from core import wire
from dht.routing import NodeInfo
from server.methods import error_response, json_response, parse_body


DHT_METHODS = frozenset({"LOCATE", "PING"})


def maybe_handle_dht(request: wire.AGTPRequest, registry: Any) -> Optional[wire.AGTPResponse]:
    node = getattr(registry, "dht_node", None)
    if node is None:
        return None
    method = request.method.upper()
    if method == "LOCATE":
        return _handle_locate(request, node)
    if method == "PING":
        return _handle_ping(node)
    return None


def _params(request: wire.AGTPRequest) -> dict:
    merged = dict(getattr(request, "query", {}) or {})
    try:
        body = parse_body(request)
    except ValueError:
        body = {}
    if isinstance(body, dict):
        merged.update(body)
    return merged


def _handle_locate(request, node) -> wire.AGTPResponse:
    params = _params(request)
    key = params.get("key")
    if not isinstance(key, str) or not key.strip():
        return error_response(
            400, "Bad Request", "locate-missing-key",
            "LOCATE requires a 'key' (64-hex target id).",
        )
    from_node = None
    raw = params.get("node")
    if isinstance(raw, dict):
        try:
            from_node = NodeInfo.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            from_node = None
    try:
        closest = node.handle_locate(key.strip().lower(), from_node=from_node)
    except Exception as exc:  # noqa: BLE001 - a bad key must not 500
        return error_response(400, "Bad Request", "locate-bad-key", str(exc))
    return json_response(
        200, "OK",
        {
            "method": "LOCATE",
            "key": key.strip().lower(),
            "node_id": node.node_id,
            "nodes": [n.to_dict() for n in closest],
        },
        method_name="LOCATE",
    )


def _handle_ping(node) -> wire.AGTPResponse:
    return json_response(
        200, "OK",
        {
            "method": "PING",
            "alive": True,
            "node": node.info.to_dict(),
        },
        method_name="PING",
    )
