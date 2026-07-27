"""
Gossip anti-entropy between full-node coordinators (PDD §6.1 "Gossip").

Two coordinators reconcile their presence views so an agent announced at
one becomes discoverable at the other. The exchange is a single
``REPLICATE`` round-trip carrying the sender's records plus a digest;
the receiver merges and returns only the delta the sender is missing:

    A -> B  {records: [all A's live records], digest: {aid: epoch}}
    B       merges A's records (most-recent announce time wins),
            replies {records: [records A lacks or holds staler]}
    A       merges B's reply

Both sides converge on the union in one call. The request-side digest is
where the bandwidth optimization lives (send hashes, pull full records on
delta); M2 pushes full records because scope populations are small, and
notes the digest-diff refinement for later.

Trust: M2 assumes cooperating, non-hostile peer coordinators — records
are merged without verifying the announcing agent's JWS signature.
Signature verification of foreign records (so a relay cannot forge or
mutate an announcement) lands in M3 alongside the rest of the presence
crypto. Visibility is NOT applied during gossip: records propagate with
their posture intact and are filtered per-requester at query time.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from client.core_client import send_method
from core import wire
from presence.records import PresenceRecord


GOSSIP_VERB = "REPLICATE"


def build_replicate_request(store) -> Dict[str, Any]:
    """The body a coordinator sends to a peer: its full live record set
    plus a digest for the peer to diff against."""
    return {
        "records": [r.to_gossip_dict() for r in store.all_records()],
        "digest": store.digest(),
    }


def apply_replicate(store, body: Dict[str, Any], *, verify=None) -> Dict[str, Any]:
    """
    Receiver side: merge the sender's records, then compute the delta the
    sender needs (records we hold that they lack or hold staler). Returns
    the response body.

    ``verify`` is an optional ``record -> bool`` predicate; records that
    fail it (e.g. an unsigned or tampered record when signature
    verification is required) are dropped rather than merged, so a peer
    cannot inject a forged or mutated announcement.
    """
    merged = 0
    rejected = 0
    for raw in body.get("records") or []:
        try:
            record = PresenceRecord.from_gossip_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed entries rather than failing the round
        if verify is not None and not verify(record):
            rejected += 1
            continue
        if store.merge_record(record):
            merged += 1

    remote_digest = body.get("digest") or {}
    if not isinstance(remote_digest, dict):
        remote_digest = {}
    delta = store.records_peer_needs(remote_digest)
    return {
        "merged": merged,
        "rejected": rejected,
        "records": [r.to_gossip_dict() for r in delta],
    }


def gossip_once(
    store,
    host: str,
    port: int,
    *,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
    verify=None,
) -> Tuple[int, int]:
    """
    Run one anti-entropy round against a single peer. Returns
    ``(pushed, pulled)`` — records sent and records merged from the reply.
    Network/parse errors are swallowed (a peer being down must not break
    the local coordinator); the round simply reconciles nothing.

    ``verify`` (record -> bool) drops unverifiable records from the reply.
    """
    request_body = build_replicate_request(store)
    pushed = len(request_body["records"])
    body_bytes = json.dumps(request_body).encode("utf-8")
    try:
        resp = send_method(
            None,  # server-level: the peer's presence hook owns REPLICATE
            host,
            port,
            GOSSIP_VERB,
            body=body_bytes,
            body_content_type="application/json",
            use_tls=use_tls,
            insecure_skip_verify=insecure_skip_verify,
        )
    except (OSError, wire.WireFormatError):
        return (0, 0)

    if resp.status_code != 200 or not resp.body_bytes:
        return (pushed, 0)
    try:
        reply = json.loads(resp.body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return (pushed, 0)

    pulled = 0
    for raw in reply.get("records") or []:
        try:
            record = PresenceRecord.from_gossip_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if verify is not None and not verify(record):
            continue
        if store.merge_record(record):
            pulled += 1
    return (pushed, pulled)


def gossip_round(
    store,
    peers: List[str],
    *,
    fanout: int = 3,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
    verify=None,
    _select=None,
) -> int:
    """
    Gossip to up to ``fanout`` peers. ``peers`` are ``"host:port"``
    strings. Returns the number of peers successfully contacted (that
    merged or were merged from). ``_select`` is an injectable peer
    selector for deterministic tests; the default samples at random.
    """
    if not peers:
        return 0
    selector = _select or _default_select
    chosen = selector(peers, fanout)
    contacted = 0
    for endpoint in chosen:
        host, _, port_s = endpoint.rpartition(":")
        if not host or not port_s.isdigit():
            continue
        pushed, pulled = gossip_once(
            store, host, int(port_s),
            use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
            verify=verify,
        )
        if pushed or pulled:
            contacted += 1
    return contacted


def _default_select(peers: List[str], fanout: int) -> List[str]:
    import random
    if len(peers) <= fanout:
        return list(peers)
    return random.sample(peers, fanout)
