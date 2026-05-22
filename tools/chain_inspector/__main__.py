"""
tools.chain_inspector — CLI entry point.

Subcommands:

    python -m tools.chain_inspector serve [--port 4482]
    python -m tools.chain_inspector walk  URI AUDIT_ID [--insecure]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from tools.chain_inspector.server import serve
from tools.chain_inspector.walker import walk_chain


def _cmd_serve(args: argparse.Namespace) -> int:
    print(f"chain inspector listening on: http://{args.bind}:{args.port}/")
    serve(port=args.port, bind=args.bind)
    return 0


def _cmd_walk(args: argparse.Namespace) -> int:
    steps = walk_chain(
        agent_uri=args.uri,
        start_audit_id=args.audit_id,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
    )
    out = {"chain": [s.to_dict() for s in steps]}
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.chain_inspector",
        description="Walk and render AGTP Attribution-Record chains.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the web UI HTTP server.")
    p_serve.add_argument("--port", type=int, default=4482)
    p_serve.add_argument("--bind", default="0.0.0.0")
    p_serve.set_defaults(func=_cmd_serve)

    p_walk = sub.add_parser("walk", help="Walk a chain from the CLI.")
    p_walk.add_argument("uri", help="agtp:// URI of the agent's daemon.")
    p_walk.add_argument("audit_id", help="Starting audit_id (64-char hex).")
    p_walk.add_argument("--insecure", action="store_true",
                        help="Connect over plaintext (test fixtures only).")
    p_walk.add_argument("--insecure-skip-verify", action="store_true",
                        help="Keep TLS but skip cert chain validation.")
    p_walk.set_defaults(func=_cmd_walk)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
