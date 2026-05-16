"""
``python -m mod_python`` entry point.

Connects to an ``agtpd`` gateway socket, loads any modules named on
the command line so their ``@endpoint``-decorated handlers register
themselves, and serves request frames until the daemon disconnects.

Example::

    python -m mod_python \\
        --gateway-socket /var/run/agtpd/gateway.sock \\
        --load-module samples.gateway_demo
"""

from __future__ import annotations

import argparse
import sys

from mod_python.client import GatewayClient, ModuleError


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mod_python",
        description="Python runtime module for AGTP",
    )
    parser.add_argument(
        "--gateway-socket",
        required=True,
        metavar="PATH",
        help=(
            "Path to the agtpd gateway socket. Pass 'host:port' "
            "for TCP loopback."
        ),
    )
    parser.add_argument(
        "--load-module",
        action="append",
        default=[],
        metavar="MODULE",
        help=(
            "Dotted Python module path to import before connecting; the "
            "module's @endpoint-decorated handlers register themselves "
            "in agtp.registry. Repeatable."
        ),
    )
    parser.add_argument(
        "--module-id",
        default="mod_python",
        help="Identifier reported to agtpd in the hello frame.",
    )
    parser.add_argument(
        "--module-version",
        default="0.1.0",
        help="Module version reported to agtpd in the hello frame.",
    )
    args = parser.parse_args()

    client = GatewayClient(
        socket_path=args.gateway_socket,
        module_id=args.module_id,
        module_version=args.module_version,
    )
    for mod_name in args.load_module:
        try:
            client.load_module(mod_name)
            print(f"[mod_python] loaded {mod_name}", file=sys.stderr)
        except ImportError as exc:
            print(
                f"[mod_python] failed to import {mod_name!r}: {exc}",
                file=sys.stderr,
            )
            return 2

    try:
        client.run()
    except ModuleError as exc:
        print(f"[mod_python] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[mod_python] shutting down", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
