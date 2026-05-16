#!/usr/bin/env python3
"""
agtp-mcp-gateway: AGTP -> MCP translation gateway.

Wraps any stdio-based MCP server with AGTP+TLS, adding agent identity,
Authority-Scope enforcement, Budget-Limit enforcement, and signed
Attribution-Records. Zero changes required to the backing MCP server.

Verb mapping (AGTP -> JSON-RPC):
    DESCRIBE + tools     -> tools/list
    EXECUTE  + tools     -> tools/call
    DISCOVER + resources -> resources/list
    QUERY    + resources -> resources/read
    DISCOVER + prompts   -> prompts/list
    QUERY    + prompts   -> prompts/get
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# ── Config ─────────────────────────────────────────────────────────────
LISTEN_HOST       = os.environ.get("AGTP_MCP_HOST", "0.0.0.0")
LISTEN_PORT       = int(os.environ.get("AGTP_MCP_PORT", "4481"))
CERT_PATH         = os.environ.get("AGTP_MCP_CERT", "/etc/letsencrypt/live/mcp.agtp.io/fullchain.pem")
KEY_PATH          = os.environ.get("AGTP_MCP_KEY",  "/etc/letsencrypt/live/mcp.agtp.io/privkey.pem")
REGISTRY_URL      = os.environ.get("AGTP_REGISTRY", "https://registry.agtp.io")
MCP_BACKEND_CMD   = os.environ.get(
    "AGTP_MCP_BACKEND",
    f"{sys.executable} /opt/agtp/mcp_gateway/mock_mcp_server.py",
).split()
GATEWAY_KEY_PATH  = os.environ.get("AGTP_GATEWAY_KEY",     "/var/lib/agtp/gateway_signing_key.pem")
ATTRIBUTION_LOG   = os.environ.get("AGTP_ATTRIBUTION_LOG", "/var/log/agtp/attribution.log")
REQUIRE_SIGNATURE = os.environ.get("AGTP_REQUIRE_SIGNATURE", "0") == "1"
PUBKEY_CACHE_TTL  = 300  # seconds

# ── Server identity document (served on DESCRIBE target=server) ────────
PUBLIC_HOST       = os.environ.get("AGTP_PUBLIC_HOST", "mcp.nomotic.ai")
OPERATOR_NAME     = os.environ.get("AGTP_OPERATOR_NAME",    "Nomotic, Inc.")
OPERATOR_URL     = os.environ.get("AGTP_OPERATOR_URL",     "https://nomotic.ai")
OPERATOR_CONTACT  = os.environ.get("AGTP_OPERATOR_CONTACT", "chris@nomotic.ai")
AGTP_VERSION      = os.environ.get("AGTP_VERSION",     "0.7")
AGTP_API_VERSION  = os.environ.get("AGTP_API_VERSION", "0.0")
SERVER_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# The AGTP 12-method protocol-level floor (v07): 6 cognitive + 6 mechanics.
AGTP_STANDARD_METHODS = [
    {"name": "QUERY",     "class": "cognitive"},
    {"name": "DISCOVER",  "class": "cognitive"},
    {"name": "DESCRIBE",  "class": "cognitive"},
    {"name": "SUMMARIZE", "class": "cognitive"},
    {"name": "PLAN",      "class": "cognitive"},
    {"name": "PROPOSE",   "class": "cognitive"},
    {"name": "EXECUTE",   "class": "mechanics"},
    {"name": "DELEGATE",  "class": "mechanics"},
    {"name": "ESCALATE",  "class": "mechanics"},
    {"name": "CONFIRM",   "class": "mechanics"},
    {"name": "SUSPEND",   "class": "mechanics"},
    {"name": "NOTIFY",    "class": "mechanics"},
]

# ── AGTP method + target -> MCP method, required scope ─────────────────
# DESCRIBE and DISCOVER are interchangeable for "list" operations.
# EXECUTE invokes; QUERY reads.
AGTP_TO_MCP = {
    ("DESCRIBE", "tools"):     ("tools/list",     "tools:list"),
    ("DISCOVER", "tools"):     ("tools/list",     "tools:list"),
    ("EXECUTE",  "tools"):     ("tools/call",     "tools:call"),
    ("DESCRIBE", "resources"): ("resources/list", "resources:list"),
    ("DISCOVER", "resources"): ("resources/list", "resources:list"),
    ("QUERY",    "resources"): ("resources/read", "resources:read"),
    ("DESCRIBE", "prompts"):   ("prompts/list",   "prompts:list"),
    ("DISCOVER", "prompts"):   ("prompts/list",   "prompts:list"),
    ("QUERY",    "prompts"):   ("prompts/get",    "prompts:get"),
}

# ── AGTP wire format ───────────────────────────────────────────────────
def read_agtp_request(sock: ssl.SSLSocket) -> dict | None:
    """Parse AGTP request with Content-Length framing only. No half-close."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 65536:
            raise ValueError("header section exceeds 64KB")
    header_bytes, _, rest = buf.partition(b"\r\n\r\n")
    lines = header_bytes.decode("utf-8", errors="strict").split("\r\n")
    status_line = lines[0]
    if not status_line.startswith("AGTP/"):
        raise ValueError(f"not an AGTP request: {status_line!r}")
    parts = status_line.split(" ", 1)
    method = parts[1].strip() if len(parts) > 1 else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    cl = int(headers.get("content-length", "0"))
    body = rest
    while len(body) < cl:
        chunk = sock.recv(min(65536, cl - len(body)))
        if not chunk:
            break
        body += chunk
    return {"method": method, "headers": headers, "body": body[:cl]}


def write_agtp_response(sock: ssl.SSLSocket, status: str, body_dict: dict,
                        extra_headers: dict | None = None,
                        content_type: str = "application/vnd.agtp+json") -> None:
    """Write AGTP response with Content-Length framing. No half-close on TLS."""
    body = json.dumps(body_dict).encode()
    head = f"AGTP/1.0 {status}\r\n"
    head += f"Content-Type: {content_type}\r\n"
    head += f"Content-Length: {len(body)}\r\n"
    if extra_headers:
        for k, v in extra_headers.items():
            head += f"{k}: {v}\r\n"
    head += "\r\n"
    sock.sendall(head.encode() + body)

# ── Agent pubkey resolution via registry ───────────────────────────────
_pubkey_cache: dict[str, tuple[Ed25519PublicKey, float]] = {}
_pubkey_lock  = threading.Lock()


def fetch_agent_pubkey(agent_id: str) -> Ed25519PublicKey | None:
    """Look up Ed25519 pubkey for an Agent-ID from the registry."""
    now = time.time()
    with _pubkey_lock:
        cached = _pubkey_cache.get(agent_id)
        if cached and now - cached[1] < PUBKEY_CACHE_TTL:
            return cached[0]
    try:
        # Strip URI scheme + host if Form 1a was passed
        bare = agent_id
        if "://" in bare:
            bare = bare.split("://", 1)[1]
        if "@" in bare:
            bare = bare.split("@", 1)[0]
        r = requests.get(f"{REGISTRY_URL}/agents/{bare}", timeout=5)
        if r.status_code != 200:
            logging.warning(f"registry returned {r.status_code} for {bare}")
            return None
        data = r.json()
        pk_b64 = data.get("public_key") or data.get("publicKey")
        if not pk_b64:
            return None
        pk_bytes = base64.b64decode(pk_b64)
        pk = Ed25519PublicKey.from_public_bytes(pk_bytes)
        with _pubkey_lock:
            _pubkey_cache[agent_id] = (pk, now)
        return pk
    except Exception as e:
        logging.warning(f"registry lookup failed for {agent_id}: {e}")
        return None


def canonical_signed_payload(method: str, agent_id: str, scope: str,
                              budget: str, task_id: str, body: bytes) -> bytes:
    """Deterministic bytes the client signs and the gateway verifies."""
    head = f"{method}\n{agent_id}\n{scope}\n{budget}\n{task_id}\n"
    return head.encode() + body


def verify_signature(agent_id: str, payload: bytes, sig_header: str) -> bool:
    pk = fetch_agent_pubkey(agent_id)
    if pk is None:
        return False
    sig_b64 = sig_header.split("=", 1)[1] if "=" in sig_header else sig_header
    try:
        pk.verify(base64.b64decode(sig_b64), payload)
        return True
    except (InvalidSignature, ValueError):
        return False

# ── Scope and budget enforcement ───────────────────────────────────────
def check_scope(authority_scope: str, required: str) -> tuple[bool, str | None]:
    scopes = {s.strip() for s in authority_scope.split(",") if s.strip()}
    if required in scopes:
        return True, None
    return False, f"scope `{required}` not present in Authority-Scope"


def check_budget(budget_limit_header: str, estimated_cost: dict) -> tuple[bool, str | None]:
    if not budget_limit_header or not estimated_cost:
        return True, None
    limits: dict[str, float] = {}
    for tok in budget_limit_header.split(","):
        if "=" in tok:
            k, v = tok.strip().split("=", 1)
            try:
                limits[k.strip()] = float(v)
            except ValueError:
                continue
    for unit, est in estimated_cost.items():
        if unit in limits and float(est) > limits[unit]:
            return False, f"estimated {unit}={est} exceeds Budget-Limit {limits[unit]}"
    return True, None

# ── MCP backend subprocess ─────────────────────────────────────────────
class MCPSubprocess:
    """Single-process MCP backend over stdio with JSON-RPC 2.0."""

    def __init__(self, cmd: list[str]) -> None:
        logging.info(f"spawning MCP backend: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.lock = threading.Lock()
        self.next_id = 1
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self._initialize()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for line in self.proc.stderr:
            logging.info(f"[mcp-backend] {line.rstrip()}")

    def _send(self, msg: dict) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("MCP backend stdin closed")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict | None:
        if self.proc.stdout is None:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def _initialize(self) -> None:
        self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agtp-mcp-gateway", "version": "0.1"},
            },
        })
        resp = self._read()
        if resp and "result" in resp:
            self.server_info = resp["result"].get("serverInfo", {}) or {}
            self.server_capabilities = resp["result"].get("capabilities", {}) or {}
        logging.info(f"MCP backend initialized: {self.server_info}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, method: str, params: dict) -> dict:
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return self._read() or {"jsonrpc": "2.0", "id": rid,
                                     "error": {"code": -32603, "message": "no response from backend"}}

# ── Attribution log ────────────────────────────────────────────────────
_gateway_key: Ed25519PrivateKey | None = None
_attribution_lock = threading.Lock()


def load_gateway_key() -> Ed25519PrivateKey:
    p = Path(GATEWAY_KEY_PATH)
    if p.exists():
        return serialization.load_pem_private_key(p.read_bytes(), password=None)  # type: ignore
    p.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    p.chmod(0o600)
    logging.info(f"generated new gateway signing key at {p}")
    return key


def write_attribution(record: dict) -> str:
    assert _gateway_key is not None
    record["id"] = f"ar-{uuid.uuid4().hex[:12]}"
    record["ts"] = int(time.time() * 1000)
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["signature"] = base64.b64encode(_gateway_key.sign(payload)).decode()
    log_path = Path(ATTRIBUTION_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _attribution_lock:
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    return record["id"]

# ── Request handler ────────────────────────────────────────────────────
def handle_connection(conn: ssl.SSLSocket, addr: tuple, mcp: MCPSubprocess) -> None:
    start = time.time()
    try:
        req = read_agtp_request(conn)
        if req is None:
            return

        method   = req["method"]
        headers  = req["headers"]
        body     = req["body"]
        agent_id = headers.get("agent-id", "")
        scope    = headers.get("authority-scope", "")
        budget   = headers.get("budget-limit", "")
        task_id  = headers.get("task-id", "")
        sig_hdr  = headers.get("signature", "")

        # 1. Signature verification
        if REQUIRE_SIGNATURE or sig_hdr:
            payload = canonical_signed_payload(method, agent_id, scope, budget, task_id, body)
            if not sig_hdr or not verify_signature(agent_id, payload, sig_hdr):
                write_agtp_response(conn, "551 Authority Chain Broken",
                                    {"error": "signature verification failed",
                                     "agent_id": agent_id})
                return

        # 2. Body parse
        try:
            body_json = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            write_agtp_response(conn, "400 Bad Request", {"error": f"invalid JSON: {e}"})
            return
        params = body_json.get("parameters", {})
        # When a browser navigates to a bare AGTP URL (no specific target),
        # treat it as a request for this server's identity document. This
        # makes `agtp://mcp.nomotic.ai` work like a homepage.
        target = params.get("target", "server")

        # 2a. Server Identity Document — header-first dispatch contract.
        # Any of DESCRIBE/DISCOVER/QUERY on target=server returns the
        # identity document. elemen's classifyDocument() reads:
        #
        #   1. X-AGTP-Document-Type header   (authoritative)
        #   2. X-AGTP-Application header     (the application kind)
        #   3. URI form                      (fallback)
        #   4. body.application.type         (last-resort body sniff)
        #
        # No HTTPS, no .well-known, no catalog URL, no vhost — everything
        # runs on AGTP 4480. Tools are carried inline in application.tools[].
        if target == "server" and method in ("DESCRIBE", "DISCOVER", "QUERY"):
            try:
                tools_resp = mcp.call("tools/list", {})
                tools = tools_resp.get("result", {}).get("tools", []) \
                    if "result" in tools_resp else []
            except Exception:
                tools = []
            assert _gateway_key is not None
            pk_bytes = _gateway_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )

            backend_caps = mcp.server_capabilities
            server_uri   = f"agtp://{PUBLIC_HOST}:{LISTEN_PORT}"
            now_iso      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            identity_doc = {
                "type": "agtp.server.identity",
                "server": {
                    "name": PUBLIC_HOST,
                    "uri": server_uri,
                    "operator": OPERATOR_NAME,
                    "operator_url": OPERATOR_URL,
                    "contact": OPERATOR_CONTACT,
                    "agtp_version": AGTP_VERSION,
                    "agtp_api_version": AGTP_API_VERSION,
                    "document_version": "v1",
                    "issued":  SERVER_STARTED_AT,
                    "updated": now_iso,
                },
                "tags": [
                    "embedded-methods/12",
                    "identity/v1",
                    "agtp-mcp",
                    "signed-attribution",
                    "scope-enforcement",
                    "budget-enforcement",
                ],
                "methods": {
                    "embedded": len(AGTP_STANDARD_METHODS),
                    "custom": 0,
                    "standard_methods": AGTP_STANDARD_METHODS,
                },
                "apis": [],
                "hosted_agents": [],
                "application": {
                    "type": "mcp",
                    "name": mcp.server_info.get("name", "unknown"),
                    "version": mcp.server_info.get("version", "unknown"),
                    "protocol_version": "2024-11-05",
                    "backend_transport": "stdio",
                    "capabilities": {
                        "tools":     "tools"     in backend_caps,
                        "resources": "resources" in backend_caps,
                        "prompts":   "prompts"   in backend_caps,
                    },
                    "tool_count": len(tools),
                    "tools": [{
                        "name": t.get("name"),
                        "title": t.get("title", t.get("name")),
                        "description": t.get("description", ""),
                        "annotations": t.get("annotations", {}),
                        "input_schema": t.get("inputSchema", {}),
                        "output_schema": t.get("outputSchema", {}),
                    } for t in tools],
                    "agtp_invocation": {
                        "list_tools": {"method": "DESCRIBE", "target": "tools"},
                        "call_tool":  {"method": "EXECUTE",  "target": "tools"},
                    },
                },
                "policy": {
                    "wildcards_accepted": False,
                    "anonymous_discovery": True,
                    "scope_required_for_invocation": True,
                    "signed_attribution": True,
                    "budget_enforcement": True,
                },
                "gateway": {
                    "public_key": base64.b64encode(pk_bytes).decode(),
                    "signature_alg": "ed25519",
                },
                "trust": {"tier": 1, "verification": "dns-anchored"},
                "spec": {
                    "agtp_draft":     "draft-hood-independent-agtp-07",
                    "agtp_api_draft": "draft-hood-agtp-api-00",
                },
            }
            ar_id = write_attribution({
                "agent_id": agent_id, "method": method, "target": target,
                "outcome": "success", "category": "server_identity",
                "tool_count": len(tools),
            })
            write_agtp_response(
                conn, "200 OK", identity_doc,
                content_type="application/vnd.agtp.identity+json",
                extra_headers={
                    "Attribution-Record-ID": ar_id,
                    "X-AGTP-Document-Type": "agtp.server.identity",
                    "X-AGTP-Application": "mcp",
                    "X-AGTP-Application-Version": "2024-11-05",
                },
            )
            return

        # 3. Verb mapping (405 Method Not Allowed for unmapped combos)
        mapping = AGTP_TO_MCP.get((method, target))
        if mapping is None:
            write_agtp_response(conn, "405 Method Not Allowed",
                                {"error": f"AGTP {method} not mapped for target `{target}`",
                                 "valid_targets": sorted({t for (_, t) in AGTP_TO_MCP})})
            return
        mcp_method, required_scope = mapping

        # 4. Scope check (455 Scope Violation)
        ok, reason = check_scope(scope, required_scope)
        if not ok:
            ar_id = write_attribution({
                "agent_id": agent_id, "method": method, "target": target,
                "mcp_method": mcp_method, "outcome": "scope_violation",
                "required_scope": required_scope, "presented_scope": scope, "reason": reason,
            })
            write_agtp_response(conn, "455 Scope Violation",
                                {"error": reason, "required_scope": required_scope},
                                extra_headers={"Attribution-Record-ID": ar_id})
            return

        # 5. Budget check (456 Budget Exceeded)
        ok, reason = check_budget(budget, params.get("estimated_cost", {}))
        if not ok:
            ar_id = write_attribution({
                "agent_id": agent_id, "method": method, "target": target,
                "mcp_method": mcp_method, "outcome": "budget_exceeded", "reason": reason,
            })
            write_agtp_response(conn, "456 Budget Exceeded", {"error": reason},
                                extra_headers={"Attribution-Record-ID": ar_id})
            return

        # 6. Translate AGTP params -> JSON-RPC params per target
        if method == "EXECUTE" and target == "tools":
            mcp_params = {"name": params.get("tool"),
                          "arguments": params.get("arguments", {})}
        elif method == "QUERY" and target == "resources":
            mcp_params = {"uri": params.get("uri")}
        elif method == "QUERY" and target == "prompts":
            mcp_params = {"name": params.get("name"),
                          "arguments": params.get("arguments", {})}
        else:
            mcp_params = {}

        # 7. Dispatch to MCP backend
        mcp_resp = mcp.call(mcp_method, mcp_params)
        latency_ms = int((time.time() - start) * 1000)

        if "error" in mcp_resp:
            err = mcp_resp["error"]
            code = err.get("code", -32603)
            # -32602 invalid params -> 460 Endpoint Violation
            # -32601 method not found -> 459 Method Violation
            if code == -32602:
                status = "460 Endpoint Violation"
            elif code == -32601:
                status = "459 Method Violation"
            else:
                status = "500 Server Error"
            ar_id = write_attribution({
                "agent_id": agent_id, "method": method, "target": target,
                "mcp_method": mcp_method, "outcome": "mcp_error",
                "mcp_error_code": code, "mcp_error_message": err.get("message"),
                "latency_ms": latency_ms,
            })
            write_agtp_response(conn, status,
                                {"error": err.get("message"), "jsonrpc_code": code},
                                extra_headers={"Attribution-Record-ID": ar_id})
            return

        # 8. Success (or tool-level error reported via isError)
        result = mcp_resp.get("result", {})
        # MCP convention: when the tool itself rejects (e.g., access denied),
        # JSON-RPC returns ok but the result carries isError=true. We surface
        # that as tool_error in attribution while keeping AGTP 200 OK so
        # MCP-aware clients can read result.content as they expect.
        is_tool_error = isinstance(result, dict) and result.get("isError") is True
        outcome = "tool_error" if is_tool_error else "success"
        tool_error_text = None
        if is_tool_error:
            try:
                tool_error_text = result.get("content", [{}])[0].get("text", "")[:200]
            except (IndexError, AttributeError):
                tool_error_text = None

        params_hash = hashlib.sha256(
            json.dumps(params, sort_keys=True).encode()
        ).hexdigest()[:16]
        record = {
            "agent_id": agent_id, "method": method, "target": target,
            "mcp_method": mcp_method, "outcome": outcome,
            "task_id": task_id, "params_hash": params_hash,
            "latency_ms": latency_ms,
        }
        if tool_error_text:
            record["tool_error"] = tool_error_text
        ar_id = write_attribution(record)
        extra = {"Attribution-Record-ID": ar_id}
        if is_tool_error:
            extra["X-Tool-Outcome"] = "error"
        write_agtp_response(conn, "200 OK",
                            {"status": outcome,
                             "mcp_method": mcp_method,
                             "result": result},
                            extra_headers=extra)

    except Exception as e:
        logging.exception(f"handler error from {addr}")
        try:
            write_agtp_response(conn, "500 Server Error", {"error": str(e)})
        except Exception:
            pass
    finally:
        # IMPORTANT: do not call sock.shutdown(SHUT_WR) on a TLS socket.
        # That sends close_notify and tears down the session before flush.
        try:
            conn.close()
        except Exception:
            pass

# ── Main ───────────────────────────────────────────────────────────────
def main() -> None:
    global _gateway_key
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    _gateway_key = load_gateway_key()
    logging.info(f"gateway key loaded from {GATEWAY_KEY_PATH}")

    mcp = MCPSubprocess(MCP_BACKEND_CMD)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    sock.listen(50)
    logging.info(f"AGTP-MCP gateway listening on {LISTEN_HOST}:{LISTEN_PORT}")
    logging.info(f"signatures required: {REQUIRE_SIGNATURE}")

    while True:
        raw, addr = sock.accept()
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
        except ssl.SSLError as e:
            logging.warning(f"TLS handshake failed from {addr}: {e}")
            raw.close()
            continue
        threading.Thread(target=handle_connection, args=(tls, addr, mcp), daemon=True).start()


if __name__ == "__main__":
    main()
