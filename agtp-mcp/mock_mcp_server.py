#!/usr/bin/env python3
"""
mock_mcp_server: minimal stdio MCP backend for the AGTP-MCP gateway demo.

Speaks JSON-RPC 2.0 over stdin/stdout per the MCP transport spec.
Implements: initialize, tools/list, tools/call, resources/list, resources/read.
Tools: read_file, list_dir (sandboxed to /tmp/agtp-mcp-sandbox).
"""
import json
import os
import sys

SANDBOX = "/tmp/agtp-mcp-sandbox"

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a text file from the demo sandbox.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List entries in a sandbox directory.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
]

RESOURCES = [
    {"uri": "demo://hello", "name": "hello", "mimeType": "text/plain"},
    {"uri": "demo://about", "name": "about", "mimeType": "text/plain"},
]

RESOURCE_DATA = {
    "demo://hello": "Hello from the mock MCP server, served via AGTP.",
    "demo://about": "AGTP-MCP gateway demo backend. Replace with a real MCP server in production.",
}


def err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def handle(req):
    method = req.get("method")
    params = req.get("params", {}) or {}
    rid = req.get("id")

    if method == "initialize":
        return ok(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "mock-mcp", "version": "0.1"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return ok(rid, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if name == "read_file":
            path = args.get("path", "")
            if not path.startswith(SANDBOX):
                return err(rid, -32602, f"path must start with {SANDBOX}")
            try:
                with open(path) as f:
                    content = f.read(10000)
                return ok(rid, {"content": [{"type": "text", "text": content}]})
            except OSError as e:
                return err(rid, -32000, str(e))
        if name == "list_dir":
            path = args.get("path", SANDBOX)
            if not path.startswith(SANDBOX):
                return err(rid, -32602, f"path must start with {SANDBOX}")
            try:
                entries = sorted(os.listdir(path))
                return ok(rid, {"content": [{"type": "text", "text": "\n".join(entries)}]})
            except OSError as e:
                return err(rid, -32000, str(e))
        return err(rid, -32601, f"unknown tool: {name}")

    if method == "resources/list":
        return ok(rid, {"resources": RESOURCES})

    if method == "resources/read":
        uri = params.get("uri", "")
        if uri in RESOURCE_DATA:
            return ok(rid, {"contents": [{
                "uri": uri, "mimeType": "text/plain", "text": RESOURCE_DATA[uri],
            }]})
        return err(rid, -32602, f"unknown resource: {uri}")

    return err(rid, -32601, f"unknown method: {method}")


def main():
    os.makedirs(SANDBOX, exist_ok=True)
    seed = os.path.join(SANDBOX, "greeting.txt")
    if not os.path.exists(seed):
        with open(seed, "w") as f:
            f.write("Hello from the AGTP-MCP demo sandbox.\n")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
