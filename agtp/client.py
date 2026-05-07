"""
AGTP CLI client.

Resolves `agtp://...` URIs and invokes methods on the target agent.
With no method argument, behaves like the v1 client: DESCRIBE the agent.
With an explicit method, sends the named method with parameters drawn
from --param / -d / --params-file.

Usage:
  agtp agtp://{id}                                        # DESCRIBE (default)
  agtp agtp://{id} --html                                 # DESCRIBE -> browser
  agtp agtp://{id} --yaml                                 # DESCRIBE -> YAML
  agtp agtp://{id} QUERY --param intent="hello"
  agtp agtp://{id} DISCOVER --param target=methods
  agtp agtp://{id} SUMMARIZE -d '{"source":"long text..."}'
  agtp agtp://{id} EXECUTE --params-file plan.json

  agtp agtp://{id}@agents.agtp.io                         # bypasses registry
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from agtp import DEFAULT_REGISTRY_URL, wire
from agtp.identity import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
)
from agtp.ids import parse_uri, ParsedURI, AgentIDError


DEFAULT_METHOD = "DESCRIBE"


class ResolutionError(Exception):
    """Raised when registry lookup or connection fails."""


def lookup_registry(
    agent_id: str, registry_url: str, *, verbose: bool = False
) -> Tuple[str, int]:
    url = f"{registry_url.rstrip('/')}/registry/{agent_id}"
    if verbose:
        print(f"[client] registry lookup: {url}", file=sys.stderr)
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


def resolve_target(
    parsed: ParsedURI, registry_url: str, *, verbose: bool = False
) -> Tuple[str, int]:
    if parsed.has_explicit_host:
        if verbose:
            print(
                f"[client] direct: {parsed.host}:{parsed.effective_port}",
                file=sys.stderr,
            )
        return parsed.host, parsed.effective_port
    return lookup_registry(parsed.agent_id, registry_url, verbose=verbose)


def send_method(
    agent_id: str,
    host: str,
    port: int,
    method_name: str,
    *,
    accept: str,
    body: bytes = b"",
    body_content_type: Optional[str] = None,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
    verbose: bool = False,
) -> wire.AGTPResponse:
    """Open an AGTP connection, send one method, return the response."""
    if verbose:
        scheme = "agtps" if use_tls else "agtp"
        print(
            f"[client] connecting: {scheme}://{host}:{port} (method {method_name})",
            file=sys.stderr,
        )

    sock = socket.create_connection((host, port), timeout=10.0)

    if use_tls:
        ctx = ssl.create_default_context()
        if insecure_skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)

    headers = {
        "Target-Agent": agent_id,
        "Accept": accept,
        "Host": host,
    }
    if body and body_content_type:
        headers["Content-Type"] = body_content_type

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


def _coerce_param_value(raw: str) -> Any:
    """
    Try to JSON-parse a `--param key=value` value so numeric, boolean, or
    JSON-array/object values come through with the right type. Falls back
    to the raw string when JSON parsing fails.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def build_body(
    raw_data: Optional[str],
    params_file: Optional[Path],
    params: Optional[list[str]],
) -> Tuple[bytes, Optional[str]]:
    """
    Resolve the JSON body from the three mutually exclusive input modes.

    Returns (body_bytes, content_type). Both are empty when no body source
    is supplied. Raises ValueError when inputs conflict or are invalid.
    """
    sources = sum(
        1 for s in (raw_data, params_file, (params or None)) if s
    )
    if sources == 0:
        return b"", None
    if sources > 1:
        raise ValueError(
            "use only one of -d / --params-file / --param at a time"
        )

    if raw_data is not None:
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"-d value is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("-d JSON must be an object")
        return json.dumps(parsed).encode("utf-8"), "application/json"

    if params_file is not None:
        try:
            text = params_file.read_text(encoding="utf-8")
            parsed = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read --params-file: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--params-file content must be a JSON object")
        return json.dumps(parsed).encode("utf-8"), "application/json"

    payload: Dict[str, Any] = {}
    for entry in params or []:
        if "=" not in entry:
            raise ValueError(f"--param expects key=value (got {entry!r})")
        key, _, value = entry.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--param key is empty (got {entry!r})")
        payload[key] = _coerce_param_value(value)
    return json.dumps(payload).encode("utf-8"), "application/json"


def _accept_for_format(fmt: str, method_name: str) -> str:
    """
    Pick the Accept header value. DESCRIBE responses are content-negotiated
    by the server; other methods return JSON regardless, so we always
    accept JSON for them.
    """
    if method_name != "DESCRIBE":
        return "application/json"
    return {
        "html": CONTENT_TYPE_HTML,
        "yaml": CONTENT_TYPE_YAML,
    }.get(fmt, CONTENT_TYPE_JSON)


def _format_response_body(response: wire.AGTPResponse) -> str:
    body_text = response.body_bytes.decode("utf-8", errors="replace")
    content_type = wire.header(response, "Content-Type", default="").lower()
    if "json" in content_type:
        try:
            return json.dumps(json.loads(body_text), indent=2)
        except json.JSONDecodeError:
            return body_text
    return body_text


def open_in_browser(html: str, agent_id: str) -> Path:
    short = agent_id[:12]
    out_path = Path(tempfile.gettempdir()) / f"agtp-{short}.html"
    out_path.write_text(html, encoding="utf-8")
    webbrowser.open(out_path.as_uri())
    return out_path


def run(args) -> int:
    try:
        parsed = parse_uri(args.uri)
    except AgentIDError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    method_name = (args.method or DEFAULT_METHOD).upper()

    if args.yaml:
        fmt = "yaml"
    elif args.html:
        fmt = "html"
    else:
        fmt = "json"

    if method_name != "DESCRIBE" and fmt in ("yaml", "html"):
        print(
            f"error: --{fmt} is meaningful only for DESCRIBE; "
            f"{method_name} returns JSON",
            file=sys.stderr,
        )
        return 2

    accept = _accept_for_format(fmt, method_name)

    try:
        body, content_type = build_body(args.data, args.params_file, args.param)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        host, port = resolve_target(parsed, args.registry, verbose=args.verbose)
    except ResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        response = send_method(
            parsed.agent_id,
            host,
            port,
            method_name,
            accept=accept,
            body=body,
            body_content_type=content_type,
            use_tls=not args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            verbose=args.verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        print(f"error: connection failed: {exc}", file=sys.stderr)
        return 1

    body_text = _format_response_body(response)

    if (
        method_name == "DESCRIBE"
        and fmt == "html"
        and not args.no_open
        and response.status_code == 200
    ):
        path = open_in_browser(body_text, parsed.agent_id)
        if args.verbose:
            print(f"[client] opened {path}", file=sys.stderr)
        return 0

    if response.status_code != 200:
        print(
            f"AGTP/1.0 {response.status_code} {response.status_text}\n",
            file=sys.stderr,
        )
        print(body_text)
        return 1

    print(body_text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agtp",
        description="AGTP client; resolves agtp:// URIs and invokes methods",
        epilog=(
            "Examples:\n"
            "  agtp agtp://d8dc6f0d...\n"
            "  agtp agtp://d8dc6f0d... QUERY --param intent='hello'\n"
            "  agtp agtp://d8dc6f0d... DISCOVER --param target=methods\n"
            "  agtp agtp://d8dc6f0d... --html\n"
            "  agtp agtp://d8dc6f0d...@agents.agtp.io"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("uri", help="agtp:// URI to resolve")
    parser.add_argument(
        "method",
        nargs="?",
        default=None,
        help="Method to invoke (defaults to DESCRIBE)",
    )

    parser.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="Set a single parameter (repeatable). Values are JSON-parsed when possible.",
    )
    parser.add_argument(
        "-d",
        "--data",
        help="Send this raw JSON object as the request body",
    )
    parser.add_argument(
        "--params-file",
        type=Path,
        help="Read the request body JSON from a file",
    )

    fmt_group = parser.add_mutually_exclusive_group()
    fmt_group.add_argument(
        "--json", action="store_true", help="JSON output (default)"
    )
    fmt_group.add_argument(
        "--yaml", action="store_true", help="YAML output (DESCRIBE only)"
    )
    fmt_group.add_argument(
        "--html",
        action="store_true",
        help="HTML identity card (DESCRIBE only); opens in default browser",
    )

    parser.add_argument(
        "--no-open",
        action="store_true",
        help="With --html: print HTML to stdout instead of opening browser",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show resolution and connection steps on stderr",
    )

    parser.add_argument("--registry", default=DEFAULT_REGISTRY_URL)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Plaintext connection (development only)",
    )
    parser.add_argument(
        "--insecure-skip-verify",
        action="store_true",
        help="Skip TLS certificate verification (development only)",
    )

    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
