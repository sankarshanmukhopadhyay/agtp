"""
Shared protocol-client logic for the AGTP client package.

Both frontends (the ``client.cli`` terminal commands and the
``client.elemen`` GUI bridge) call into this module rather than
re-implementing connection handling and response parsing. The CLI
adds argparse + output formatting on top; the bridge adds
JS-friendly serialization on top. Neither does any wire work itself.

Public surface
--------------

* ``FetchResult``        result envelope returned by every entry point
* ``ResolutionError``    raised when registry lookup fails
* ``lookup_registry``    bare-ID -> (host, port) HTTPS lookup
* ``resolve_target``     ParsedURI -> (host, port), via registry if needed
* ``send_method``        open a connection, send one method, return wire.AGTPResponse
* ``fetch``              high-level entry: agent URI -> Agent Doc; server URI -> Manifest
* ``fetch_manifest``     server-level DISCOVER given (host, port)
* ``invoke_method``      build + send a method invocation, structured params/body
* ``fetch_mcp_catalog``  HTTPS-side fetch for MCP tool catalogs
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from core import wire
from core.identity import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
)
from core.ids import AgentIDError, ParsedURI, parse_uri


DEFAULT_REGISTRY_URL = "https://registry.agtp.io"


# Format-to-Accept-header mapping used by callers that want
# content-negotiated DESCRIBE responses.
FORMAT_TO_ACCEPT: Dict[str, str] = {
    "json": CONTENT_TYPE_JSON,
    "yaml": CONTENT_TYPE_YAML,
    "html": CONTENT_TYPE_HTML,
}


# ---------------------------------------------------------------------------
# Result envelope.
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """
    Unified result for every core_client entry point.

    Success cases set ``ok=True``, populate ``status_code``,
    ``status_text``, ``headers``, ``body_bytes``, and (where
    applicable) ``parsed`` with the JSON dict. ``kind`` records the
    high-level shape so consumers can branch:

      * ``"agent"``           Agent Document from DESCRIBE
      * ``"manifest"``        Server Manifest from server-level DISCOVER
      * ``"method-response"`` any other method invocation
      * ``"mcp_catalog"``     MCP tool catalog (HTTPS-side)

    Failure cases set ``ok=False`` and populate ``error`` and
    ``stage`` (one of ``"parse"`` / ``"resolve"`` / ``"fetch"``).
    Status fields may still be populated when the failure happened
    after a successful HTTP-level response (e.g., a non-200 status).
    """

    ok: bool
    kind: Optional[str] = None
    status_code: Optional[int] = None
    status_text: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    body_bytes: bytes = b""
    parsed: Any = None
    resolved_endpoint: Optional[str] = None
    agent_id: Optional[str] = None
    error: Optional[str] = None
    stage: Optional[str] = None

    @property
    def body_text(self) -> str:
        """UTF-8 decode of body_bytes (replacement for invalid bytes)."""
        return self.body_bytes.decode("utf-8", errors="replace")

    @property
    def host(self) -> Optional[str]:
        if not self.resolved_endpoint:
            return None
        return self.resolved_endpoint.rsplit(":", 1)[0]

    @property
    def port(self) -> Optional[int]:
        if not self.resolved_endpoint:
            return None
        try:
            return int(self.resolved_endpoint.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return None


# ---------------------------------------------------------------------------
# Registry resolution.
# ---------------------------------------------------------------------------


class ResolutionError(Exception):
    """Raised when a Form 1 URI fails registry lookup."""


def lookup_registry(
    agent_id: str,
    registry_url: str,
    *,
    verbose: bool = False,
) -> Tuple[str, int]:
    """Query the registry for ``agent_id``; return ``(host, port)``."""
    url = f"{registry_url.rstrip('/')}/registry/{agent_id}"
    if verbose:
        import sys
        print(f"[client] registry lookup: {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ResolutionError(
                f"agent {agent_id} is not registered at {registry_url}"
            ) from exc
        raise ResolutionError(
            f"registry lookup failed: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ResolutionError(
            f"could not reach registry at {registry_url}: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ResolutionError(
            f"registry returned invalid JSON: {exc}"
        ) from exc

    host = data.get("host")
    port = data.get("port")
    if not host or not port:
        raise ResolutionError(
            f"registry response missing host/port: {data!r}"
        )
    return host, int(port)


def resolve_target(
    parsed: ParsedURI,
    registry_url: str,
    *,
    verbose: bool = False,
) -> Tuple[str, int]:
    """
    Map a ParsedURI to a (host, port).

    Form 1a / Form 2 (host present) use the embedded host directly;
    Form 1 (bare agent ID) goes through the configured registry.
    """
    if parsed.has_explicit_host:
        if verbose:
            import sys
            print(
                f"[client] direct: {parsed.host}:{parsed.effective_port}",
                file=__import__("sys").stderr,
            )
        return parsed.host, parsed.effective_port
    if parsed.agent_id is None:
        # The URI grammar guarantees one of (agent_id, host) is set.
        raise ResolutionError("URI has neither agent ID nor host")
    return lookup_registry(parsed.agent_id, registry_url, verbose=verbose)


# ---------------------------------------------------------------------------
# Wire-level send.
# ---------------------------------------------------------------------------


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
    extra_headers: Optional[Dict[str, str]] = None,
    path: str = "/",
    verbose: bool = False,
) -> wire.AGTPResponse:
    """
    Open a TCP/TLS connection, send one AGTP method, return the response.

    ``agent_id`` is None for server-level requests (Form 2 URIs); the
    Agent-ID header is then omitted, which is what server.main uses
    to route DISCOVER to the manifest path. ``extra_headers``
    (e.g., ``Synthesis-Id``) merge in last and may override defaults.

    ``path`` rides on the request line as the third token (Phase-2
    extension). The default ``"/"`` preserves the pre-Phase-2
    two-token wire form on the network, so older servers and
    transcripts keep working byte-identically.
    """
    if verbose:
        import sys
        scheme = "agtps" if use_tls else "agtp"
        print(
            f"[client] connecting: {scheme}://{host}:{port} "
            f"(method {method_name})",
            file=sys.stderr,
        )

    sock = socket.create_connection((host, port), timeout=10.0)
    if use_tls:
        ctx = ssl.create_default_context()
        if insecure_skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)

    headers: Dict[str, str] = {"Accept": accept, "Host": host}
    if agent_id is not None:
        # §10 canonical header. Pre-§10 servers expected
        # ``Target-Agent``; the server-side back-compat fallback
        # reads either, so this client always emits the §10 name.
        headers["Agent-ID"] = agent_id
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
            path=path,
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


# ---------------------------------------------------------------------------
# High-level fetch (auto-routes agent vs manifest).
# ---------------------------------------------------------------------------


def fetch(
    uri: str,
    *,
    fmt: str = "json",
    registry_url: str = DEFAULT_REGISTRY_URL,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
    verbose: bool = False,
) -> FetchResult:
    """
    Resolve an ``agtp://`` URI and fetch the document it points at.

    URI form determines the wire call and the result kind:

      * Form 1 / 1a (agent ID present)
        Sends DESCRIBE with the requested ``fmt``. Returns
        ``kind="agent"``.

      * Form 2 (no agent ID)
        Sends server-level DISCOVER. Returns ``kind="manifest"`` and
        populates ``parsed`` with the manifest dict.
    """
    try:
        parsed = parse_uri(uri)
    except AgentIDError as exc:
        return FetchResult(ok=False, error=str(exc), stage="parse")

    if parsed.is_server_level:
        return _fetch_manifest_via_parsed(
            parsed,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
            verbose=verbose,
        )

    accept = FORMAT_TO_ACCEPT.get(fmt, CONTENT_TYPE_JSON)

    try:
        host, port = resolve_target(parsed, registry_url, verbose=verbose)
    except ResolutionError as exc:
        return FetchResult(
            ok=False,
            error=str(exc),
            stage="resolve",
            agent_id=parsed.agent_id,
        )

    try:
        response = send_method(
            parsed.agent_id,
            host,
            port,
            "DESCRIBE",
            accept=accept,
            use_tls=not insecure,
            insecure_skip_verify=insecure_skip_verify,
            verbose=verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        return FetchResult(
            ok=False,
            error=f"connection failed: {exc}",
            stage="fetch",
            agent_id=parsed.agent_id,
            resolved_endpoint=f"{host}:{port}",
        )

    parsed_body = _maybe_parse_json(response.body_bytes)
    return FetchResult(
        ok=True,
        kind="agent",
        status_code=response.status_code,
        status_text=response.status_text,
        headers=dict(response.headers),
        body_bytes=response.body_bytes,
        parsed=parsed_body,
        resolved_endpoint=f"{host}:{port}",
        agent_id=parsed.agent_id,
    )


def fetch_manifest(
    host: str,
    port: int,
    *,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
    verbose: bool = False,
) -> FetchResult:
    """
    Direct manifest fetch given a known (host, port). Used by the
    matching handshake when the caller already holds an agent's
    endpoint and needs the server's manifest alongside.
    """
    parsed = ParsedURI(agent_id=None, host=host, port=port)
    return _fetch_manifest_via_parsed(
        parsed,
        insecure=insecure,
        insecure_skip_verify=insecure_skip_verify,
        verbose=verbose,
    )


def _fetch_manifest_via_parsed(
    parsed: ParsedURI,
    *,
    insecure: bool,
    insecure_skip_verify: bool,
    verbose: bool,
) -> FetchResult:
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
            verbose=verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        return FetchResult(
            ok=False,
            error=f"connection failed: {exc}",
            stage="fetch",
            resolved_endpoint=f"{host}:{port}",
        )

    parsed_body = (
        _maybe_parse_json(response.body_bytes)
        if response.status_code == 200
        else None
    )
    return FetchResult(
        ok=True,
        kind="manifest",
        status_code=response.status_code,
        status_text=response.status_text,
        headers=dict(response.headers),
        body_bytes=response.body_bytes,
        parsed=parsed_body,
        resolved_endpoint=f"{host}:{port}",
    )


# ---------------------------------------------------------------------------
# Method invocation.
# ---------------------------------------------------------------------------


def invoke_method(
    uri: str,
    method_name: str,
    *,
    body: Optional[Dict[str, Any]] = None,
    registry_url: str = DEFAULT_REGISTRY_URL,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
    synthesis_id: Optional[str] = None,
    path: str = "/",
    verbose: bool = False,
) -> FetchResult:
    """
    Build a method invocation request and dispatch it.

    ``body`` is an optional JSON-serializable dict. ``synthesis_id``
    when set adds a ``Synthesis-Id`` header so the server rewrites the
    request onto the synthesis's underlying method.

    ``path`` is the URI path the server should route on. Defaults to
    ``"/"``, which matches the wire shape pre-Phase-2 servers expect;
    callers targeting a Phase-2 endpoint registry pass the bound
    path (e.g. ``"/room"``).

    There is no separate probe header for verb admission. The catalog
    gate at the top of the dispatcher does the admission check
    unconditionally, so callers that want to probe a verb just send
    an ordinary request and read the status code (200/400 → admitted,
    459 → catalog refusal, 405 → no handler, 403 → forbidden).
    """
    try:
        parsed = parse_uri(uri)
    except AgentIDError as exc:
        return FetchResult(ok=False, error=str(exc), stage="parse")

    try:
        host, port = resolve_target(parsed, registry_url, verbose=verbose)
    except ResolutionError as exc:
        return FetchResult(
            ok=False,
            error=str(exc),
            stage="resolve",
            agent_id=parsed.agent_id,
        )

    body_bytes = b""
    body_content_type: Optional[str] = None
    if body is not None and body != {}:
        body_bytes = json.dumps(body).encode("utf-8")
        body_content_type = "application/json"

    extra: Dict[str, str] = {}
    if synthesis_id:
        extra["Synthesis-Id"] = synthesis_id

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
            extra_headers=extra or None,
            path=path,
            verbose=verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        return FetchResult(
            ok=False,
            error=f"connection failed: {exc}",
            stage="fetch",
            agent_id=parsed.agent_id,
            resolved_endpoint=f"{host}:{port}",
        )

    parsed_body = _maybe_parse_json(response.body_bytes)
    return FetchResult(
        ok=True,
        kind="method-response",
        status_code=response.status_code,
        status_text=response.status_text,
        headers=dict(response.headers),
        body_bytes=response.body_bytes,
        parsed=parsed_body,
        resolved_endpoint=f"{host}:{port}",
        agent_id=parsed.agent_id,
    )


# ---------------------------------------------------------------------------
# MCP catalog (HTTPS-side, not AGTP wire).
# ---------------------------------------------------------------------------


def fetch_mcp_catalog(
    catalog_url: str,
    *,
    insecure_skip_verify: bool = False,
) -> FetchResult:
    """
    Fetch an MCP tool catalog over HTTPS. MCP itself runs on HTTPS,
    not on AGTP, so this side-steps the wire layer.
    """
    if not catalog_url or not catalog_url.startswith(("http://", "https://")):
        return FetchResult(
            ok=False,
            error=f"catalog URL must be HTTP(S): {catalog_url!r}",
            stage="parse",
        )

    ctx = None
    if insecure_skip_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(
            catalog_url, timeout=10.0, context=ctx
        ) as resp:
            status = resp.getcode()
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        return FetchResult(
            ok=False,
            error=f"catalog HTTP {exc.code}",
            stage="fetch",
            resolved_endpoint=catalog_url,
        )
    except (urllib.error.URLError, OSError) as exc:
        return FetchResult(
            ok=False,
            error=f"catalog unreachable: {exc}",
            stage="fetch",
            resolved_endpoint=catalog_url,
        )

    parsed_body = _maybe_parse_json(body_bytes)
    return FetchResult(
        ok=True,
        kind="mcp_catalog",
        status_code=status,
        body_bytes=body_bytes,
        parsed=parsed_body,
        resolved_endpoint=catalog_url,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _maybe_parse_json(body_bytes: bytes) -> Any:
    """Best-effort JSON decode. Returns None when body is empty or non-JSON."""
    if not body_bytes:
        return None
    try:
        return json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


__all__ = [
    "DEFAULT_REGISTRY_URL",
    "FORMAT_TO_ACCEPT",
    "FetchResult",
    "ResolutionError",
    "fetch",
    "fetch_manifest",
    "fetch_mcp_catalog",
    "invoke_method",
    "lookup_registry",
    "resolve_target",
    "send_method",
]
