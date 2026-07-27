"""
Cross-scope resolution over the DHT (PDD §6.1 "Cross-scope resolution").

Two operations compose the DHT (by-key routing) with the rendezvous index:

  * :func:`publish_scopes` — a coordinator advertises the capability scopes
    it serves. For each capability it LOCATEs the scope's rendezvous key
    (``overlay_id({capability: C})``) and PUBLISHes itself to the closest
    node(s), which become that scope's rendezvous points.

  * :func:`cross_scope_discover` — a requester finds agents in a scope it
    has not joined. It LOCATEs the scope's rendezvous key, reads the
    provider coordinators from the rendezvous node, then DISCOVERs each and
    merges — all without ever gossiping with the providers.

Resolution cost is one O(log N) DHT walk to the rendezvous point plus a
query to the (usually few) provider coordinators.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from client.core_client import send_method
from core import wire
from dht.client import locate_over_wire
from presence import scopes as _scopes


def scope_key_for_capability(capability: str) -> str:
    """The rendezvous key for a capability scope: the overlay id of
    ``{capability: <c>}``."""
    return _scopes.overlay_id(_scopes.scope_tuple(capability=capability))


def _rendezvous_nodes(dht_node, scope_key, *, use_tls, insecure_skip_verify, replicas):
    nodes = locate_over_wire(
        dht_node, scope_key, use_tls=use_tls, insecure_skip_verify=insecure_skip_verify
    )
    return nodes[:replicas] if replicas else nodes


def publish_scopes(
    dht_node,
    provider_endpoint: str,
    capabilities: List[str],
    *,
    replicas: int = 2,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> int:
    """
    Advertise ``provider_endpoint`` as a provider of each capability scope.
    Returns the number of (scope, rendezvous-node) publications made.
    """
    published = 0
    for cap in capabilities:
        key = scope_key_for_capability(cap)
        body = json.dumps({
            "scope_key": key, "endpoint": provider_endpoint, "label": f"capability:{cap}",
        }).encode("utf-8")
        for node in _rendezvous_nodes(
            dht_node, key, use_tls=use_tls,
            insecure_skip_verify=insecure_skip_verify, replicas=replicas,
        ):
            try:
                resp = send_method(
                    None, node.host, node.port, "PUBLISH",
                    body=body, body_content_type="application/json",
                    use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
                )
                if resp.status_code == 200:
                    published += 1
            except (OSError, wire.WireFormatError):
                continue
    return published


def _providers_from(node, scope_key, *, use_tls, insecure_skip_verify) -> List[str]:
    import urllib.parse
    q = urllib.parse.quote(scope_key, safe="")
    try:
        resp = send_method(
            None, node.host, node.port, "DISCOVER",
            path=f"/providers?scope_key={q}",
            use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
        )
    except (OSError, wire.WireFormatError):
        return []
    if resp.status_code != 200 or not resp.body_bytes:
        return []
    try:
        return json.loads(resp.body_bytes.decode("utf-8")).get("providers") or []
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []


def cross_scope_discover(
    dht_node,
    capability: str,
    *,
    replicas: int = 2,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
    as_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolve agents in the ``capability`` scope across the federation, from a
    coordinator that has not joined that scope. Returns a merged result:
    ``{capability, providers, results}`` (results deduped by agent_id).
    """
    from presence.client import discover_population_json

    key = scope_key_for_capability(capability)
    # 1. LOCATE the scope's rendezvous point(s).
    rzv = _rendezvous_nodes(
        dht_node, key, use_tls=use_tls,
        insecure_skip_verify=insecure_skip_verify, replicas=replicas,
    )
    # 2. Read the provider coordinators from the rendezvous node(s).
    providers: List[str] = []
    for node in rzv:
        for ep in _providers_from(
            node, key, use_tls=use_tls, insecure_skip_verify=insecure_skip_verify
        ):
            if ep not in providers:
                providers.append(ep)
    # 3. DISCOVER each provider and merge.
    merged: Dict[str, Dict[str, Any]] = {}
    for ep in providers:
        host, _, port_s = ep.rpartition(":")
        if not host or not port_s.isdigit():
            continue
        body = discover_population_json(
            host, int(port_s), capability=capability, as_agent=as_agent,
            use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
        )
        for r in body.get("results") or []:
            merged[r.get("agent_id")] = r
    return {"capability": capability, "providers": providers,
            "results": list(merged.values())}
