"""
Wire transport for the DHT: a LOCATE rpc and a PING-based bootstrap, built
on :func:`client.core_client.send_method`.

:func:`make_wire_rpc` returns the ``rpc(node, key)`` callable that
:meth:`dht.kademlia.KademliaNode.iterative_locate` drives — it sends LOCATE
to a peer (carrying the local node's identity so the peer learns of us) and
parses the returned NodeInfo list.
"""

from __future__ import annotations

import json
import time
from typing import Callable, List, Optional

from client.core_client import send_method
from core import wire
from dht.routing import NodeInfo


def _send_retry(host, port, method, *, body=b"", body_content_type=None,
                use_tls=True, insecure_skip_verify=False, attempts=3):
    """send_method with a couple of retries on transient connection errors —
    a DHT RPC is expected to tolerate a dropped/refused connection (a busy or
    briefly-unreachable peer) rather than treating it as a hard failure."""
    last = None
    for i in range(attempts):
        try:
            return send_method(
                None, host, port, method,
                body=body, body_content_type=body_content_type,
                use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
            )
        except (OSError, wire.WireFormatError) as exc:
            last = exc
            time.sleep(0.02 * (i + 1))
    raise last if last is not None else OSError("send failed")


def make_wire_rpc(
    local: NodeInfo, *, use_tls: bool = True, insecure_skip_verify: bool = False
) -> Callable[[NodeInfo, str], List[NodeInfo]]:
    """Build the LOCATE rpc a KademliaNode uses for iterative lookup."""

    def rpc(peer: NodeInfo, key: str) -> List[NodeInfo]:
        body = json.dumps({"key": key, "node": local.to_dict()}).encode("utf-8")
        try:
            resp = _send_retry(
                peer.host, peer.port, "LOCATE",
                body=body, body_content_type="application/json",
                use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
            )
        except (OSError, wire.WireFormatError):
            return []
        if resp.status_code != 200 or not resp.body_bytes:
            return []
        try:
            data = json.loads(resp.body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        out = []
        for raw in data.get("nodes") or []:
            try:
                out.append(NodeInfo.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    return rpc


def ping(host: str, port: int, *, use_tls: bool = True,
         insecure_skip_verify: bool = False) -> Optional[NodeInfo]:
    """PING a peer endpoint; return its NodeInfo (learning its node id) or
    None if unreachable."""
    try:
        resp = _send_retry(
            host, port, "PING",
            use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
        )
    except (OSError, wire.WireFormatError):
        return None
    if resp.status_code != 200 or not resp.body_bytes:
        return None
    try:
        data = json.loads(resp.body_bytes.decode("utf-8"))
        return NodeInfo.from_dict(data["node"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def bootstrap_over_wire(
    dht_node, seed_endpoints: List[str], *,
    use_tls: bool = True, insecure_skip_verify: bool = False, refresh: int = 8,
) -> int:
    """
    Join the DHT via ``seed_endpoints`` ("host:port"). Each seed is PINGed
    to learn its node id (you cannot add a peer to a routing table without
    its id), then a normal Kademlia bootstrap runs over the wire.
    """
    seeds = []
    for endpoint in seed_endpoints:
        host, _, port_s = endpoint.rpartition(":")
        if not host or not port_s.isdigit():
            continue
        info = ping(host, int(port_s), use_tls=use_tls,
                    insecure_skip_verify=insecure_skip_verify)
        if info is not None:
            seeds.append(info)
    if not seeds:
        return len(dht_node.table)
    rpc = make_wire_rpc(dht_node.info, use_tls=use_tls,
                        insecure_skip_verify=insecure_skip_verify)
    return dht_node.bootstrap(seeds, rpc, refresh=refresh)


def locate_over_wire(
    dht_node, key: str, *, disjoint: int = 1,
    use_tls: bool = True, insecure_skip_verify: bool = False,
) -> List[NodeInfo]:
    """Run an iterative LOCATE for ``key`` over the wire, returning the
    closest nodes found. ``disjoint>1`` uses S/Kademlia disjoint paths."""
    rpc = make_wire_rpc(dht_node.info, use_tls=use_tls,
                        insecure_skip_verify=insecure_skip_verify)
    if disjoint > 1:
        return dht_node.iterative_locate_disjoint(key, rpc, disjoint=disjoint)
    return dht_node.iterative_locate(key, rpc)
