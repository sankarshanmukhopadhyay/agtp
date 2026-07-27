"""
AGTP-Presence M3 — presence-record signing, runnable.

A non-hosted ("foreign") agent signs its own announcement and submits it;
the coordinator verifies the signature before accepting. A relay that
tampers with the record in transit is rejected, and under signature-
verifying gossip a peer that injects a mutated record is dropped.

Run::

    python -m samples.presence_signing_demo
"""

from __future__ import annotations

import json
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.core_client import send_method
from presence import client as pc
from presence import gossip as g
from presence import recordsig as rs
from presence.records import PresenceRecord, Visibility
from presence.store import PresenceStore
from presence.testing import InProcessCoordinator
from server.signing import SigningService


def _record(agent_id, name):
    return PresenceRecord(
        agent_id=agent_id,
        result_entry={
            "agent_id": agent_id, "manifest_uri": f"agtp://{agent_id}",
            "name": name, "supported_methods": ["VALIDATE"],
            "capabilities": ["validate"], "trust_tier": 2,
        },
        visibility=Visibility(),
    )


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M3 demo - presence-record signing")
    print("=" * 68)

    coord = InProcessCoordinator([]).start()  # hosts nothing
    agent_key = SigningService(private_key=Ed25519PrivateKey.generate())

    try:
        # 1. Foreign agent signs and announces itself.
        rec = _record("f" * 64, "foreign-auditor")
        r = pc.announce_signed_record(coord.host, coord.port, rec, agent_key, use_tls=False)
        print(f"\nsigned foreign ANNOUNCE -> {r.status_code}")
        check("signed foreign announce accepted", r.status_code == 200)
        d = pc.discover_population_json(coord.host, coord.port, capability="validate", use_tls=False)
        check("foreign agent is discoverable", d["total_matches"] == 1)

        # 2. A relay mutates the signed record in transit -> rejected.
        rec2 = _record("h" * 64, "victim")
        rs.sign_record(rec2, agent_key)
        gossip_dict = rec2.to_gossip_dict()
        gossip_dict["result_entry"]["trust_tier"] = 1  # relay promotes the agent
        body = json.dumps({"record": gossip_dict}).encode("utf-8")
        r2 = send_method(
            None, coord.host, coord.port, "ANNOUNCE",
            body=body, body_content_type="application/json", use_tls=False,
        )
        print(f"relay-mutated ANNOUNCE -> {r2.status_code}")
        check("relay-mutated announcement rejected (403)", r2.status_code == 403)

        # 3. Signature-verifying gossip drops a peer's forged record.
        src = PresenceStore()
        good = _record("a" * 64, "good")
        rs.sign_record(good, agent_key)
        src.announce(good)
        req = g.build_replicate_request(src)
        req["records"][0]["result_entry"]["trust_tier"] = 1  # peer tampers
        dst = PresenceStore()
        reply = g.apply_replicate(dst, req, verify=rs.verify_record)
        print(f"gossip with a tampered record -> merged={reply['merged']} "
              f"rejected={reply['rejected']}")
        check("verifying gossip drops the tampered record",
              reply["merged"] == 0 and reply["rejected"] == 1)
    finally:
        coord.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - signatures make announcements tamper-evident end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
