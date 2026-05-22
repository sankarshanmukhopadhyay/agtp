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
    known_agents = {}
    if args.known_agents:
        try:
            with open(args.known_agents, "r", encoding="utf-8") as f:
                known_agents = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"could not read --known-agents: {exc}", file=sys.stderr)
            return 2
        if not isinstance(known_agents, dict):
            print(
                "--known-agents must be a JSON object {agent_id: agent_uri}",
                file=sys.stderr,
            )
            return 2
    steps = walk_chain(
        agent_uri=args.uri,
        start_audit_id=args.audit_id,
        known_agents={str(k).lower(): str(v) for k, v in known_agents.items()},
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
    p_walk.add_argument(
        "--known-agents",
        help=(
            "Path to a JSON file mapping {agent_id: agent_uri}. The "
            "walker uses this to resolve cross-agent prior_actions "
            "references that don't carry an inline agent_uri."
        ),
    )
    p_walk.add_argument("--insecure", action="store_true",
                        help="Connect over plaintext (test fixtures only).")
    p_walk.add_argument("--insecure-skip-verify", action="store_true",
                        help="Keep TLS but skip cert chain validation.")
    p_walk.set_defaults(func=_cmd_walk)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
