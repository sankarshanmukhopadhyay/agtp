#!/usr/bin/env python3
"""
agtp_mcp_client: demo client for the AGTP-MCP gateway.

Constructs AGTP requests, optionally signs them with an Ed25519 key,
sends them over TLS, and pretty-prints the wire bytes both ways.

Examples:

    # List tools
    python3 client_demo.py --agent-id agtp.demo.001 list-tools

    # Call a tool
    python3 client_demo.py --agent-id agtp.demo.001 \\
        call-tool --name read_file --args '{"path":"/tmp/agtp-mcp-sandbox/greeting.txt"}'

    # Trigger 455 Scope Violation
    python3 client_demo.py --agent-id agtp.demo.001 --scope "" list-tools

    # Trigger 456 Budget Exceeded (estimated_cost > Budget-Limit)
    python3 client_demo.py --agent-id agtp.demo.001 --budget "usd=0.0001" \\
        call-tool --name read_file \\
        --args '{"path":"/tmp/agtp-mcp-sandbox/greeting.txt"}' \\
        --estimated-cost '{"usd":0.5}'
"""
from __future__ import annotations
import argparse
import base64
import json
import socket
import ssl
import sys

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


def load_key(path: str):
    if not HAVE_CRYPTO:
        sys.exit("cryptography library required for --key. pip install cryptography")
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def canonical_signed_payload(method, agent_id, scope, budget, task_id, body):
    head = f"{method}\n{agent_id}\n{scope}\n{budget}\n{task_id}\n"
    return head.encode() + body


def build_request(method, agent_id, scope, budget, task_id, body_dict, signing_key=None):
    body = json.dumps(body_dict).encode()
    payload = canonical_signed_payload(method, agent_id, scope, budget, task_id, body)
    sig_line = ""
    if signing_key is not None:
        sig = signing_key.sign(payload)
        sig_line = f"Signature: ed25519={base64.b64encode(sig).decode()}\r\n"
    req = (
        f"AGTP/1.0 {method}\r\n"
        f"Agent-ID: {agent_id}\r\n"
        f"Authority-Scope: {scope}\r\n"
        f"Budget-Limit: {budget}\r\n"
        f"Task-ID: {task_id}\r\n"
        f"Content-Type: application/vnd.agtp+json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{sig_line}"
        f"\r\n"
    ).encode() + body
    return req


def send_request(host, port, req_bytes, insecure=False):
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=15)
    s = ctx.wrap_socket(raw, server_hostname=host)
    try:
        s.sendall(req_bytes)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        status_line = lines[0]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        cl = int(headers.get("Content-Length", "0"))
        body = rest
        while len(body) < cl:
            chunk = s.recv(min(65536, cl - len(body)))
            if not chunk:
                break
            body += chunk
        return status_line, headers, body[:cl]
    finally:
        # Do not call shutdown(SHUT_WR) on TLS. Just close.
        s.close()


def main():
    ap = argparse.ArgumentParser(description="AGTP-MCP gateway demo client")
    ap.add_argument("--host", default="mcp.agtp.io")
    ap.add_argument("--port", type=int, default=4481)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate verification (dev only)")
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--key", help="path to Ed25519 private key PEM (optional)")
    ap.add_argument("--scope", default="tools:list, tools:call, resources:list, resources:read")
    ap.add_argument("--budget", default="usd=0.01")
    ap.add_argument("--task-id", default="task-demo")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-tools")
    sub.add_parser("list-resources")
    sub.add_parser("describe-server")
    p = sub.add_parser("call-tool")
    p.add_argument("--name", required=True)
    p.add_argument("--args", default="{}", help="JSON arguments")
    p.add_argument("--estimated-cost", default="{}",
                   help='JSON dict of estimated cost (e.g. {"usd":0.005})')
    p = sub.add_parser("read-resource")
    p.add_argument("--uri", required=True)

    args = ap.parse_args()
    signing_key = load_key(args.key) if args.key else None

    if args.cmd == "list-tools":
        method = "DESCRIBE"
        body = {"method": method, "task_id": args.task_id,
                "parameters": {"target": "tools"}}
    elif args.cmd == "describe-server":
        method = "DESCRIBE"
        body = {"method": method, "task_id": args.task_id,
                "parameters": {"target": "server"}}
    elif args.cmd == "list-resources":
        method = "DISCOVER"
        body = {"method": method, "task_id": args.task_id,
                "parameters": {"target": "resources"}}
    elif args.cmd == "call-tool":
        method = "EXECUTE"
        body = {"method": method, "task_id": args.task_id,
                "parameters": {
                    "target": "tools",
                    "tool": args.name,
                    "arguments": json.loads(args.args),
                    "estimated_cost": json.loads(args.estimated_cost),
                }}
    elif args.cmd == "read-resource":
        method = "QUERY"
        body = {"method": method, "task_id": args.task_id,
                "parameters": {"target": "resources", "uri": args.uri}}
    else:
        sys.exit(f"unknown command: {args.cmd}")

    req = build_request(method, args.agent_id, args.scope, args.budget,
                        args.task_id, body, signing_key)

    print("=" * 70)
    print("AGTP REQUEST")
    print("=" * 70)
    sys.stdout.write(req.decode())
    print()

    try:
        status, headers, resp_body = send_request(args.host, args.port, req, args.insecure)
    except (ConnectionError, ssl.SSLError, socket.gaierror) as e:
        sys.exit(f"connection failed: {e}")

    print("=" * 70)
    print("AGTP RESPONSE")
    print("=" * 70)
    print(status)
    for k, v in headers.items():
        print(f"{k}: {v}")
    print()
    try:
        print(json.dumps(json.loads(resp_body), indent=2))
    except json.JSONDecodeError:
        sys.stdout.write(resp_body.decode(errors="replace"))


if __name__ == "__main__":
    main()
