"""
AGTP client wrapper.

Refactored from agtp.py for use as a library by the Ovoara browser. The
fetch() entry point returns a structured dict so the UI can render
success and failure uniformly.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# AGTP v1 reference library lives outside this project. Allow override via
# AGTP_LIB_PATH env var; otherwise try two common layouts:
#   1. elemen is a subdirectory of the agtp repo  -> ../v1
#   2. elemen is a sibling of the agtp repo       -> ../agtp/v1
_DEFAULT_LIB_CANDIDATES = [
    Path(os.environ.get("AGTP_LIB_PATH", "")),
    Path(__file__).resolve().parent.parent / "v1",
    Path(__file__).resolve().parent.parent / "agtp" / "v1",
]

for _candidate in _DEFAULT_LIB_CANDIDATES:
    if _candidate and _candidate.is_dir() and (_candidate / "wire_v2.py").exists():
        sys.path.insert(0, str(_candidate))
        break
else:
    raise RuntimeError(
        "Could not locate AGTP v1 library (agent_id.py, agent_document.py, "
        "wire_v2.py). Set AGTP_LIB_PATH env var to its directory."
    )

from agent_id import parse_uri, ParsedURI, AgentIDError  # noqa: E402
from agent_document import (  # noqa: E402
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
)
import wire_v2 as wire  # noqa: E402


DEFAULT_REGISTRY_URL = "https://registry.agtp.io"

FORMAT_TO_ACCEPT = {
    "json": CONTENT_TYPE_JSON,
    "yaml": CONTENT_TYPE_YAML,
    "html": CONTENT_TYPE_HTML,
}


class ResolutionError(Exception):
    pass


def lookup_registry(agent_id: str, registry_url: str) -> tuple[str, int]:
    url = f"{registry_url.rstrip('/')}/registry/{agent_id}"
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ResolutionError(
                f"agent {agent_id} is not registered at {registry_url}"
            ) from exc
        raise ResolutionError(f"registry lookup failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ResolutionError(
            f"could not reach registry at {registry_url}: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ResolutionError(f"registry returned invalid JSON: {exc}") from exc

    host = data.get("host")
    port = data.get("port")
    if not host or not port:
        raise ResolutionError(f"registry response missing host/port: {data!r}")
    return host, int(port)


def resolve_target(parsed: ParsedURI, registry_url: str) -> tuple[str, int]:
    if parsed.has_explicit_host:
        return parsed.host, parsed.effective_port
    return lookup_registry(parsed.agent_id, registry_url)


def send_method(
    agent_id: Optional[str],
    host: str,
    port: int,
    method_name: str,
    *,
    accept: str = "application/json",
    body: bytes = b"",
    body_content_type: Optional[str] = None,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
    extra_headers: Optional[dict] = None,
) -> wire.AGTPResponse:
    """
    Generalized method send. DESCRIBE, DISCOVER, QUERY... all reuse this.

    ``agent_id`` is None for server-level requests (Form 2 URIs); the
    Target-Agent header is then omitted. A non-empty body must come
    with a body_content_type. ``extra_headers`` (e.g.,
    ``Synthesis-Id``) merge into the request headers last so the
    caller can override.
    """
    sock = socket.create_connection((host, port), timeout=10.0)

    if use_tls:
        ctx = ssl.create_default_context()
        if insecure_skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)

    headers = {
        "Accept": accept,
        "Host": host,
    }
    if agent_id is not None:
        headers["Target-Agent"] = agent_id
    if body and body_content_type:
        headers["Content-Type"] = body_content_type
    if extra_headers:
        for k, v in extra_headers.items():
            headers[str(k)] = str(v)

    try:
        request = wire.AGTPRequest(
            method=method_name,
            headers=headers,
            body_bytes=body,
        )
        sock.sendall(request.serialize())
        # Don't half-close on TLS sockets; close_notify ends the session.
        reader = sock.makefile("rb")
        return wire.parse_response(reader)
    finally:
        try:
            sock.close()
        except OSError:
            pass


# Backwards-compatible alias. Older code in elemen called the helper
# fetch_agent_document; that's preserved as a thin wrapper.
def fetch_agent_document(
    agent_id: str,
    host: str,
    port: int,
    accept: str,
    *,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> wire.AGTPResponse:
    return send_method(
        agent_id,
        host,
        port,
        "DESCRIBE",
        accept=accept,
        use_tls=use_tls,
        insecure_skip_verify=insecure_skip_verify,
    )


def fetch(
    uri: str,
    fmt: str = "json",
    registry: str = DEFAULT_REGISTRY_URL,
    *,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
) -> dict:
    """
    Resolve an agtp:// URI and return a result dict for UI rendering.

    URI form determines the wire call and the result shape:

      * Form 1 / 1a  (agent_id present)
        Sends DESCRIBE. Returns ``kind="agent"`` plus the standard
        ``{agent_id, body, content_type, format, ...}`` shape.

      * Form 2  (no agent_id)
        Sends server-level DISCOVER. Returns ``kind="manifest"`` plus
        a parsed ``manifest`` field for the UI to render directly.

    Both kinds carry the same transport metadata (host, port,
    status_code, status_text, headers).
    """
    try:
        parsed = parse_uri(uri)
    except AgentIDError as exc:
        return {"ok": False, "error": str(exc), "stage": "parse"}

    if parsed.is_server_level:
        return _fetch_manifest(
            parsed,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )

    accept = FORMAT_TO_ACCEPT.get(fmt, CONTENT_TYPE_JSON)

    try:
        host, port = resolve_target(parsed, registry)
    except ResolutionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "stage": "resolve",
            "agent_id": parsed.agent_id,
        }

    try:
        response = fetch_agent_document(
            parsed.agent_id,
            host,
            port,
            accept,
            use_tls=not insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
    except (OSError, wire.WireFormatError) as exc:
        return {
            "ok": False,
            "error": f"connection failed: {exc}",
            "stage": "fetch",
            "agent_id": parsed.agent_id,
            "host": host,
            "port": port,
        }

    body = response.body_bytes.decode("utf-8", errors="replace")

    return {
        "ok": True,
        "kind": "agent",
        "agent_id": parsed.agent_id,
        "host": host,
        "port": port,
        "status_code": response.status_code,
        "status_text": response.status_text,
        "headers": dict(response.headers),
        "body": body,
        "content_type": response.headers.get("Content-Type", ""),
        "format": fmt,
    }


def _fetch_manifest(
    parsed,
    *,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
) -> dict:
    """
    Server-level DISCOVER (no Target-Agent). Returns the Server
    Manifest as both the raw text body and a parsed dict so the UI
    can render structured views without re-parsing.
    """
    host = parsed.host
    port = parsed.effective_port
    try:
        response = send_method(
            agent_id=None,
            host=host,
            port=port,
            method_name="DISCOVER",
            accept="application/json",
            use_tls=not insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
    except (OSError, wire.WireFormatError) as exc:
        return {
            "ok": False,
            "error": f"connection failed: {exc}",
            "stage": "fetch",
            "host": host,
            "port": port,
        }

    body_text = response.body_bytes.decode("utf-8", errors="replace")
    manifest_obj: Optional[dict] = None
    if response.status_code == 200:
        try:
            parsed_obj = json.loads(body_text)
            if isinstance(parsed_obj, dict):
                manifest_obj = parsed_obj
        except json.JSONDecodeError:
            pass

    return {
        "ok": True,
        "kind": "manifest",
        "host": host,
        "port": port,
        "status_code": response.status_code,
        "status_text": response.status_text,
        "headers": dict(response.headers),
        "body": body_text,
        "content_type": response.headers.get("Content-Type", ""),
        "manifest": manifest_obj,
        "format": "json",
    }


def fetch_manifest(
    host: str,
    port: int,
    *,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
) -> dict:
    """
    Public helper for the UI to fetch the manifest by host:port (used
    by the matching handshake when an agent URI is loaded). Returns
    the same shape as ``fetch`` for a server-level URI.
    """
    from agtp.ids import ParsedURI
    parsed = ParsedURI(agent_id=None, host=host, port=port)
    return _fetch_manifest(
        parsed,
        insecure=insecure,
        insecure_skip_verify=insecure_skip_verify,
    )


def discover_methods(
    uri: str,
    *,
    registry: str = DEFAULT_REGISTRY_URL,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
) -> dict:
    """
    Send DISCOVER target=methods to the server hosting the URI's agent.

    Result shape:
        ok=True:  {ok, agent_id, host, port, status_code, embedded,
                   custom, summary, raw}
        ok=False: {ok, error, stage}
    """
    try:
        parsed = parse_uri(uri)
    except AgentIDError as exc:
        return {"ok": False, "error": str(exc), "stage": "parse"}

    try:
        host, port = resolve_target(parsed, registry)
    except ResolutionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "stage": "resolve",
            "agent_id": parsed.agent_id,
        }

    body = json.dumps({"target": "methods"}).encode("utf-8")
    try:
        response = send_method(
            parsed.agent_id,
            host,
            port,
            "DISCOVER",
            accept="application/json",
            body=body,
            body_content_type="application/json",
            use_tls=not insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
    except (OSError, wire.WireFormatError) as exc:
        return {
            "ok": False,
            "error": f"connection failed: {exc}",
            "stage": "discover",
            "agent_id": parsed.agent_id,
            "host": host,
            "port": port,
        }

    raw = response.body_bytes.decode("utf-8", errors="replace")
    if response.status_code != 200:
        return {
            "ok": False,
            "error": f"DISCOVER returned {response.status_code} {response.status_text}",
            "stage": "discover",
            "status_code": response.status_code,
            "agent_id": parsed.agent_id,
            "host": host,
            "port": port,
            "raw": raw,
        }

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"DISCOVER body was not JSON: {exc}",
            "stage": "discover",
            "agent_id": parsed.agent_id,
            "host": host,
            "port": port,
            "raw": raw,
        }

    return {
        "ok": True,
        "agent_id": parsed.agent_id,
        "host": host,
        "port": port,
        "status_code": response.status_code,
        "embedded": payload.get("embedded", []),
        "custom": payload.get("custom", []),
        "summary": payload.get("summary", {}),
        "raw": raw,
    }


def invoke_method(
    uri: str,
    method_name: str,
    body_dict: Optional[dict] = None,
    *,
    registry: str = DEFAULT_REGISTRY_URL,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
    synthesis_id: Optional[str] = None,
) -> dict:
    """
    Send an arbitrary method with an optional JSON body. Used by the
    elemen "Try it" form so the UI can invoke any verb DISCOVER reports.

    Result shape mirrors fetch(): {ok, agent_id, host, port, status_code,
    status_text, headers, body, content_type}.
    """
    try:
        parsed = parse_uri(uri)
    except AgentIDError as exc:
        return {"ok": False, "error": str(exc), "stage": "parse"}

    try:
        host, port = resolve_target(parsed, registry)
    except ResolutionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "stage": "resolve",
            "agent_id": parsed.agent_id,
        }

    body_bytes = b""
    body_content_type = None
    if body_dict is not None and body_dict != {}:
        body_bytes = json.dumps(body_dict).encode("utf-8")
        body_content_type = "application/json"

    extra_headers = {}
    if synthesis_id:
        extra_headers["Synthesis-Id"] = synthesis_id

    try:
        response = send_method(
            parsed.agent_id,
            host,
            port,
            method_name.upper(),
            accept="application/json",
            body=body_bytes,
            body_content_type=body_content_type,
            use_tls=not insecure,
            insecure_skip_verify=insecure_skip_verify,
            extra_headers=extra_headers or None,
        )
    except (OSError, wire.WireFormatError) as exc:
        return {
            "ok": False,
            "error": f"connection failed: {exc}",
            "stage": "invoke",
            "agent_id": parsed.agent_id,
            "host": host,
            "port": port,
        }

    body = response.body_bytes.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "agent_id": parsed.agent_id,
        "host": host,
        "port": port,
        "method": method_name.upper(),
        "status_code": response.status_code,
        "status_text": response.status_text,
        "headers": dict(response.headers),
        "body": body,
        "content_type": response.headers.get("Content-Type", ""),
    }
