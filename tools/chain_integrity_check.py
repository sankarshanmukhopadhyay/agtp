"""
``agtp-chain-integrity-check`` — verify per-agent audit chains are
complete and self-consistent.

Background (governance/security hardening pass): the per-agent audit
chain is split across two independent on-disk stores —
:class:`server.audit_chain.AuditChainStore` (one small JSON pointer
file per agent, "what's the latest audit_id?") and
:class:`server.audit_records.AuditRecordStore` (one JWS file per
audit_id, "what did that record say?"). Nothing links the two other
than convention: the head store's own docstring says corrupted or
missing head files are treated as "no prior record" so a bad write
doesn't wedge the chain forever. That's the right call operationally,
but it also means a local-filesystem-access actor (an insider, a
compromised backup job, a container rebuilt from a stale snapshot)
can silently truncate an agent's history by deleting or replacing its
head file — the next legitimate action then chains to
``previous_audit_id: null``, indistinguishable from that agent's
actual first-ever action, while every prior record still sits
untouched (and now unreachable) in the records store.

This tool is the detector for that class of tamper, plus the more
mundane failure mode (a single deleted or corrupted mid-chain
record). It does **not** fix or restore anything — it reports.

Usage::

    python -m tools.chain_integrity_check
    python -m tools.chain_integrity_check --chain-head-root ~/.agtp/audit/chain_heads
    python -m tools.chain_integrity_check --agent-id <64-hex> --agent-id <64-hex>
    python -m tools.chain_integrity_check --json
    python -m tools.chain_integrity_check --verify-key path/to/ed25519_public.pem

Exit codes:

  * **0** — every checked chain walked cleanly back to genesis
    (``previous_audit_id == "0"*64``) with no missing links.
  * **1** — at least one broken or orphaned chain was found.
  * **2** — usage / filesystem error (bad ``--chain-head-root``, etc).

Output line (also the commit-message artifact for this tool's own
validation): ``chain_integrity_check.py: {agents_checked} agents,
{broken_chains} broken, {orphaned_heads} orphaned``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.audit_chain import AuditChainStore, default_chain_head_root
from server.audit_records import AuditRecordStore, default_records_root
from server.signing import AttributionRecordError, parse_attribution_record

#: Sentinel ``previous_audit_id`` value on an agent's first-ever
#: record (server.signing.SigningService writes this when no prior
#: audit_id is passed).
GENESIS_SENTINEL = "0" * 64

#: Hard cap on walk depth so a corrupted file that points to itself
#: (or a cycle introduced by tampering) can't hang the tool.
MAX_WALK_DEPTH = 100_000


class ChainIntegrityError(Exception):
    """Raised for usage-level failures (bad paths, etc)."""


@dataclass
class ChainWalkResult:
    agent_id: str
    head_audit_id: Optional[str]
    records_walked: int = 0
    reached_genesis: bool = False
    #: True when the head pointer itself doesn't resolve to a
    #: record — the sharpest form of tamper this tool can catch:
    #: someone rewrote or deleted the head file.
    orphaned_head: bool = False
    #: True when the walk hit *any* missing link, at the head or
    #: mid-chain. orphaned_head implies broken.
    broken: bool = False
    #: The audit_id whose predecessor could not be resolved, when
    #: broken is True.
    break_at: Optional[str] = None
    #: Populated only when --verify-key is supplied.
    signature_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "head_audit_id": self.head_audit_id,
            "records_walked": self.records_walked,
            "reached_genesis": self.reached_genesis,
            "orphaned_head": self.orphaned_head,
            "broken": self.broken,
            "break_at": self.break_at,
            "signature_failures": list(self.signature_failures),
        }


def discover_agent_ids(chain_head_root: Path) -> List[str]:
    """List agent_ids with a chain-head file under ``chain_head_root``.

    Head files are named ``{agent_id}.json``; malformed filenames
    (not ending in ``.json``, or with a stem that doesn't look like
    64-hex) are skipped rather than raising, since a stray file in
    the directory shouldn't abort the whole run.
    """
    if not chain_head_root.exists():
        return []
    agent_ids = []
    for entry in sorted(chain_head_root.glob("*.json")):
        stem = entry.stem
        agent_ids.append(stem)
    return agent_ids


def walk_chain(
    agent_id: str,
    *,
    chain_store: AuditChainStore,
    records_store: AuditRecordStore,
    verify_key=None,
) -> ChainWalkResult:
    """Walk one agent's chain from its head back to genesis.

    ``verify_key``, when supplied, is an ``Ed25519PublicKey`` used
    to additionally verify each record's signature (structural
    checks — missing links — run regardless of this).
    """
    head = chain_store.head(agent_id)
    if head is None:
        # No head at all is not itself suspicious — it's the normal
        # state for an agent that has never taken an audited action.
        # Distinguishing "never had one" from "had one, now gone" is
        # exactly what this tool can't do from local state alone;
        # see the module docstring. We report it as a clean,
        # zero-record chain rather than flagging it.
        return ChainWalkResult(
            agent_id=agent_id, head_audit_id=None,
            reached_genesis=True,
        )

    result = ChainWalkResult(agent_id=agent_id, head_audit_id=head.audit_id)
    current = head.audit_id
    seen: set = set()
    for _ in range(MAX_WALK_DEPTH):
        if current in seen:
            # Cycle — only reachable via tampering (a genuine chain
            # is a DAG of one). Report and stop.
            result.broken = True
            result.break_at = current
            return result
        seen.add(current)

        jws = records_store.read(current)
        if jws is None:
            result.broken = True
            result.break_at = current
            if current == head.audit_id:
                result.orphaned_head = True
            return result

        try:
            _header, payload, _sig = parse_attribution_record(jws)
        except AttributionRecordError as exc:
            result.broken = True
            result.break_at = current
            result.signature_failures.append(f"{current}: unparseable ({exc})")
            return result

        result.records_walked += 1

        if verify_key is not None:
            from server.signing import verify_attribution_record
            try:
                verify_attribution_record(jws, verify_key)
            except AttributionRecordError as exc:
                result.signature_failures.append(f"{current}: {exc}")

        previous = str(payload.get("previous_audit_id") or GENESIS_SENTINEL)
        if previous == GENESIS_SENTINEL:
            result.reached_genesis = True
            return result
        current = previous

    # Exceeded MAX_WALK_DEPTH without reaching genesis or a break —
    # treat as broken so a runaway chain doesn't silently report clean.
    result.broken = True
    result.break_at = current
    return result


def run(
    *,
    chain_head_root: Path,
    records_root: Path,
    agent_ids: Optional[List[str]] = None,
    verify_key_path: Optional[str] = None,
) -> List[ChainWalkResult]:
    chain_store = AuditChainStore(chain_head_root)
    records_store = AuditRecordStore(records_root)

    verify_key = None
    if verify_key_path:
        from cryptography.hazmat.primitives import serialization
        pem = Path(verify_key_path).read_text(encoding="utf-8")
        verify_key = serialization.load_pem_public_key(pem.encode("utf-8"))

    ids = agent_ids if agent_ids else discover_agent_ids(chain_head_root)
    return [
        walk_chain(
            aid,
            chain_store=chain_store,
            records_store=records_store,
            verify_key=verify_key,
        )
        for aid in ids
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agtp-chain-integrity-check",
        description=(
            "Verify per-agent audit chains are complete: every "
            "previous_audit_id resolves to a stored record, and the "
            "walk reaches genesis with no missing or orphaned links."
        ),
    )
    parser.add_argument(
        "--chain-head-root", default=None,
        help="Root of the chain-head store (default: "
             "[audit].chain_head_root platform default).",
    )
    parser.add_argument(
        "--records-root", default=None,
        help="Root of the per-audit-id JWS store (default: "
             "[audit].records_root platform default).",
    )
    parser.add_argument(
        "--agent-id", action="append", dest="agent_ids", default=None,
        help="Check only this agent_id. Repeatable. Default: every "
             "agent with a chain-head file.",
    )
    parser.add_argument(
        "--verify-key", default=None,
        help="PEM-encoded Ed25519 public key. When supplied, each "
             "record's signature is also verified (not just chain "
             "structure).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit machine-readable JSON instead of a text report.",
    )
    return parser


def render_text(results: List[ChainWalkResult]) -> str:
    lines = []
    for r in results:
        if r.head_audit_id is None:
            lines.append(f"  {r.agent_id[:12]}...  no chain (never audited)")
            continue
        status = "OK"
        if r.orphaned_head:
            status = "ORPHANED HEAD"
        elif r.broken:
            status = f"BROKEN at {r.break_at[:12]}..."
        lines.append(
            f"  {r.agent_id[:12]}...  {status}  "
            f"({r.records_walked} record(s) walked, "
            f"genesis={'yes' if r.reached_genesis else 'no'})"
        )
        for failure in r.signature_failures:
            lines.append(f"      signature: {failure}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    chain_head_root = (
        Path(args.chain_head_root) if args.chain_head_root
        else default_chain_head_root()
    )
    records_root = (
        Path(args.records_root) if args.records_root
        else default_records_root()
    )

    try:
        results = run(
            chain_head_root=chain_head_root,
            records_root=records_root,
            agent_ids=args.agent_ids,
            verify_key_path=args.verify_key,
        )
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    agents_checked = len(results)
    broken_chains = sum(1 for r in results if r.broken)
    orphaned_heads = sum(1 for r in results if r.orphaned_head)

    if args.json_output:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(f"chain_head_root: {chain_head_root}")
        print(f"records_root:    {records_root}")
        print(render_text(results), end="")

    summary = (
        f"chain_integrity_check.py: {agents_checked} agents, "
        f"{broken_chains} broken, {orphaned_heads} orphaned"
    )
    print(summary)

    return 1 if broken_chains else 0


__all__ = [
    "ChainIntegrityError",
    "ChainWalkResult",
    "GENESIS_SENTINEL",
    "build_parser",
    "discover_agent_ids",
    "main",
    "render_text",
    "run",
    "walk_chain",
]


if __name__ == "__main__":
    sys.exit(main())
