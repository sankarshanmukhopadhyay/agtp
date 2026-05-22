"""
tools.registrar.__main__ — CLI entry for the reference registrar.

Subcommands:

    python -m tools.registrar serve   [--port 4481] [--data-dir PATH]
    python -m tools.registrar issue   --name NAME --owner OWNER \\
                                       --public-key PATH \\
                                       [--principal PRINCIPAL] \\
                                       [--archetype ARCHETYPE] \\
                                       [--zone ZONE] \\
                                       [--tier {1,2,3}] \\
                                       [--out PATH]
    python -m tools.registrar pubkey  [--data-dir PATH]
    python -m tools.registrar list    [--data-dir PATH]

The ``serve`` subcommand starts an HTTP server exposing the
issuance API (see ``tools/registrar/server.py`` for endpoint
docs). The ``issue`` / ``pubkey`` / ``list`` subcommands are
offline equivalents — they read and write the same data
directory without standing up the server.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from core.genesis import VALID_ARCHETYPES, VALID_TRUST_TIERS
from tools.registrar.store import RegistrarStore, default_data_dir


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--data-dir",
        help="Registrar data directory (default: ~/.agtp/registrar/).",
    )
    p.add_argument(
        "--issuer-id", default="registrar.local",
        help="Issuer identifier for new Geneses. Default: registrar.local.",
    )


def _store(args: argparse.Namespace) -> RegistrarStore:
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    return RegistrarStore(data_dir, issuer_id=args.issuer_id)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _cmd_serve(args: argparse.Namespace) -> int:
    from tools.registrar.server import serve
    store = _store(args)
    print(f"registrar data dir: {store.data_dir}")
    print(f"issuer id:          {store.issuer_id}")
    print(f"listening on:       http://0.0.0.0:{args.port}/")
    print(f"public key:         http://0.0.0.0:{args.port}/pubkey")
    serve(store, port=args.port, bind=args.bind)
    return 0


# ---------------------------------------------------------------------------
# issue (offline)
# ---------------------------------------------------------------------------


def _cmd_issue(args: argparse.Namespace) -> int:
    store = _store(args)
    pub_pem = Path(args.public_key).read_text(encoding="utf-8")
    genesis = store.issue(
        name=args.name,
        owner_id=args.owner,
        principal_id=args.principal or args.owner,
        agent_public_key_pem=pub_pem,
        archetype=args.archetype,
        governance_zone=args.zone,
        trust_tier=args.tier,
        verification_path=args.verification_path,
    )
    out_path = (
        Path(args.out)
        if args.out
        else Path(f"{args.name}.genesis.json")
    )
    out_path.write_text(genesis.to_pretty_json() + "\n", encoding="utf-8")
    print(f"genesis:    {out_path}")
    print(f"agent_id:   {genesis.canonical_agent_id()}")
    print(f"issuer:     {genesis.issuer}")
    print(f"trust_tier: {genesis.trust_tier}")
    return 0


# ---------------------------------------------------------------------------
# pubkey
# ---------------------------------------------------------------------------


def _cmd_pubkey(args: argparse.Namespace) -> int:
    store = _store(args)
    sys.stdout.write(store.issuer_public_key_pem)
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    ids = store.list_issued()
    if not ids:
        print("(no Geneses issued)")
        return 0
    for aid in ids:
        print(aid)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.registrar",
        description="Reference AGTP registrar (Agent Genesis issuer).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="Run the registrar HTTP server.")
    _add_common_args(p_serve)
    p_serve.add_argument("--port", type=int, default=4481)
    p_serve.add_argument("--bind", default="0.0.0.0")
    p_serve.set_defaults(func=_cmd_serve)

    # issue (offline)
    p_issue = sub.add_parser(
        "issue",
        help="Issue a Genesis offline using the registrar's key.",
    )
    _add_common_args(p_issue)
    p_issue.add_argument("--name", required=True)
    p_issue.add_argument("--owner", required=True)
    p_issue.add_argument("--principal")
    p_issue.add_argument(
        "--public-key", required=True,
        help="Path to the agent's Ed25519 public key (PEM).",
    )
    p_issue.add_argument("--archetype", choices=sorted(VALID_ARCHETYPES))
    p_issue.add_argument("--zone")
    p_issue.add_argument("--tier", type=int, choices=list(VALID_TRUST_TIERS), default=2)
    p_issue.add_argument(
        "--verification-path", default="self-signed",
        choices=["dns-anchored", "log-anchored", "hybrid", "self-signed"],
    )
    p_issue.add_argument("--out", help="Output Genesis JSON path.")
    p_issue.set_defaults(func=_cmd_issue)

    # pubkey
    p_pub = sub.add_parser(
        "pubkey", help="Print the registrar's Ed25519 public key (PEM).",
    )
    _add_common_args(p_pub)
    p_pub.set_defaults(func=_cmd_pubkey)

    # list
    p_list = sub.add_parser(
        "list", help="Print the agent_ids of all issued Geneses.",
    )
    _add_common_args(p_list)
    p_list.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
