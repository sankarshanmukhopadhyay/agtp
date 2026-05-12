"""
agtp-curl: a diagnostic CLI for AGTP that mirrors curl's surface.

This is the "I just want to poke an AGTP server" tool. The official
client is `agtp` (agtp.client). agtp-curl is for debugging.

Usage:
  agtp-curl DESCRIBE agtp://{agent-id}
  agtp-curl QUERY    agtp://localhost:4480 -d '{"intent":"weather"}'
  agtp-curl DISCOVER agtp://localhost:4480/methods
  agtp-curl -X DESCRIBE agtp://{agent-id}
  agtp-curl -H "Agent-ID: {id}" DESCRIBE agtp://localhost:4480
  agtp-curl -i DISCOVER agtp://localhost:4480/methods   # include headers

Two URI shapes are accepted:
  agtp://{agent-id}[@host[:port]]    Standard form. Resolves via registry
                                     unless host is embedded.
  agtp://host[:port][/<verb>]        Server form. Skips registry.
                                     The trailing /<verb> is sugar for
                                     `<verb>` and `target=<verb>`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from client import DEFAULT_REGISTRY_URL
from client import core_client
from core import wire
from core.ids import (
    AGENT_ID_PATTERN,
    AgentIDError,
    DEFAULT_AGTP_PORT,
    parse_uri,
)


# Server-form pattern: agtp://host[:port][/path]. The `host` is anything
# that isn't a 64-char hex agent id. Captured for the curl-style sugar.
_SERVER_FORM = (
    r"^agtp://"
    r"(?P<host>[a-zA-Z0-9.\-]+)"
    r"(?::(?P<port>\d+))?"
    r"(?:/(?P<path>[A-Za-z0-9_\-./]+))?$"
)


class CurlError(Exception):
    """Raised for any user-visible error during the request."""


def _resolve_via_registry(agent_id: str, registry_url: str) -> Tuple[str, int]:
    """
    curl-flavored wrapper around ``core_client.lookup_registry`` that
    surfaces the registry URL in error messages so users debugging a
    failing curl invocation see what the tool tried.
    """
    try:
        return core_client.lookup_registry(agent_id, registry_url)
    except core_client.ResolutionError as exc:
        raise CurlError(str(exc)) from exc


def _parse_target(
    uri: str, registry_url: str, *, verbose: bool = False
) -> Tuple[Optional[str], str, int, Optional[str]]:
    """
    Resolve the URI to (target_agent_id, host, port, path_sugar).

    The trailing ``/path`` (if present) is curl-flavored sugar: on
    DISCOVER, the caller fills the body with ``{"target": "<path>"}``
    so ``agtp-curl DISCOVER agtp://localhost:4480/methods`` works
    without an explicit -d argument. The path is stripped before the
    URI is handed to the formal parser.

    ``target_agent_id`` is None for server-level (Form 2) URIs;
    otherwise it is the canonical 64-char hex ID.
    """
    text = uri.strip()
    path_sugar: Optional[str] = None
    scheme, sep, rest = text.partition("://")
    if sep and "/" in rest:
        authority, _, path = rest.partition("/")
        text = f"{scheme}://{authority}"
        path_sugar = path or None

    try:
        parsed = parse_uri(text)
    except AgentIDError as exc:
        raise CurlError(f"not a recognized AGTP URI: {uri!r}") from exc

    if parsed.is_server_level:
        # Server-level: agent_id is None; the manifest path activates
        # in server.py when no Target-Agent header is sent.
        assert parsed.host is not None
        return None, parsed.host, parsed.effective_port, path_sugar

    if parsed.has_explicit_host:
        # Form 1a: agent ID with embedded host.
        return parsed.agent_id, parsed.host, parsed.effective_port, path_sugar

    # Form 1: bare agent ID; resolve via registry.
    host, port = _resolve_via_registry(parsed.agent_id, registry_url)
    if verbose:
        print(
            f"* registry {registry_url} -> {host}:{port}",
            file=sys.stderr,
        )
    return parsed.agent_id, host, port, path_sugar


def _split_header(text: str) -> Tuple[str, str]:
    if ":" not in text:
        raise CurlError(f"-H expects 'Name: value' (got {text!r})")
    name, _, value = text.partition(":")
    return name.strip(), value.strip()


def _send(
    method: str,
    host: str,
    port: int,
    headers: Dict[str, str],
    body: bytes,
    *,
    use_tls: bool,
    verify_tls: bool,
    verbose: bool,
) -> wire.AGTPResponse:
    """
    curl-flavored wrapper around ``core_client.send_method`` that
    prints curl-style request lines (`*`, `>`) on stderr when
    ``--verbose`` is set. The actual wire work delegates to
    core_client; the request envelope is built from raw headers
    rather than the high-level invoke helpers because curl wants to
    let users override anything via -H.
    """
    if verbose:
        scheme = "agtps" if use_tls else "agtp"
        print(f"* connecting: {scheme}://{host}:{port}", file=sys.stderr)
        print(f"> {method} (AGTP/1.0)", file=sys.stderr)
        for k, v in headers.items():
            print(f"> {k}: {v}", file=sys.stderr)
        if body:
            preview = body[:256].decode("utf-8", errors="replace")
            print(f"> body ({len(body)} bytes): {preview}", file=sys.stderr)

    # core_client.send_method derives Agent-ID / Accept / Host /
    # Content-Type from its arguments and merges extra_headers last.
    # curl already built a complete header dict, so we pass it
    # wholesale via extra_headers and supply blank "intrinsic" values
    # so the merge resolves to exactly what the user asked for.
    accept = headers.get("Accept", "application/json")
    # §10: prefer Agent-ID, but accept the legacy Target-Agent value
    # for users following pre-§10 docs / cheat sheets.
    target_agent = headers.get("Agent-ID") or headers.get("Target-Agent")
    body_content_type = headers.get("Content-Type")
    return core_client.send_method(
        target_agent,
        host,
        port,
        method,
        accept=accept,
        body=body,
        body_content_type=body_content_type,
        use_tls=use_tls,
        insecure_skip_verify=not verify_tls,
        extra_headers={
            k: v for k, v in headers.items()
            if k not in (
                "Accept", "Agent-ID", "Target-Agent", "Host", "Content-Type",
            )
        } or None,
        verbose=False,  # curl already printed its own verbose trace above
    )


def _format_response(response: wire.AGTPResponse, *, include_headers: bool) -> str:
    body_text = response.body_bytes.decode("utf-8", errors="replace")
    content_type = wire.header(response, "Content-Type", default="").lower()

    if "json" in content_type:
        try:
            body_text = json.dumps(json.loads(body_text), indent=2)
        except json.JSONDecodeError:
            pass

    if not include_headers:
        return body_text

    lines = [f"AGTP/1.0 {response.status_code} {response.status_text}"]
    for k, v in response.headers.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append(body_text)
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    if args.method_pos and args.method_flag:
        print(
            "error: method given both positionally and via -X",
            file=sys.stderr,
        )
        return 2

    use_tls = not args.insecure
    verify_tls = not args.insecure_skip_verify

    try:
        target_id, host, port, path_sugar = _parse_target(
            args.uri, args.registry, verbose=args.verbose
        )
    except CurlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Choose the default method: agent-targeting URIs default to
    # DESCRIBE (the v1 behavior); server-level URIs default to
    # DISCOVER, which returns the Server Manifest.
    explicit_method = args.method_pos or args.method_flag
    if explicit_method:
        method = explicit_method.upper()
    elif target_id is None:
        method = "DISCOVER"
    else:
        method = "DESCRIBE"

    headers: Dict[str, str] = {"Host": host}
    if target_id is not None:
        headers["Agent-ID"] = target_id

    # Curl-style sugar: agtp://host:port/methods on DISCOVER auto-fills
    # the body with `{"target": "methods"}`. Any explicit -d/-H wins.
    body = b""
    body_content_type: Optional[str] = None

    if args.data is not None:
        body = args.data.encode("utf-8")
        body_content_type = "application/json"
    elif method == "DISCOVER" and path_sugar:
        body = json.dumps({"target": path_sugar}).encode("utf-8")
        body_content_type = "application/json"

    accept = args.accept or (
        "application/vnd.agtp.identity+json"
        if method == "DESCRIBE"
        else "application/json"
    )
    headers["Accept"] = accept

    for h in args.header or []:
        try:
            name, value = _split_header(h)
        except CurlError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        headers[name] = value

    if body and body_content_type and "Content-Type" not in headers:
        headers["Content-Type"] = body_content_type

    try:
        response = _send(
            method,
            host,
            port,
            headers,
            body,
            use_tls=use_tls,
            verify_tls=verify_tls,
            verbose=args.verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        print(f"error: connection failed: {exc}", file=sys.stderr)
        return 1

    print(_format_response(response, include_headers=args.include))
    return 0 if response.status_code == 200 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agtp-curl",
        description="curl-equivalent for AGTP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "method_pos",
        nargs="?",
        metavar="METHOD",
        help="Method to send (e.g. DESCRIBE, QUERY).",
    )
    p.add_argument("uri", help="agtp:// URI")
    p.add_argument(
        "-X", "--request",
        dest="method_flag",
        help="Method, alternative to the positional form.",
    )
    p.add_argument(
        "-H", "--header",
        action="append",
        metavar="'Name: value'",
        help="Add a header (repeatable).",
    )
    p.add_argument(
        "-d", "--data",
        help="Send raw JSON body. The Content-Type defaults to application/json.",
    )
    p.add_argument(
        "-A", "--accept",
        help="Override the Accept header. Defaults to application/json.",
    )
    p.add_argument(
        "-i", "--include",
        action="store_true",
        help="Include response headers in the output.",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show connection and request details on stderr.",
    )
    p.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY_URL,
        help="Registry URL for bare-ID resolution.",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Plaintext connection (development only).",
    )
    p.add_argument(
        "--insecure-skip-verify",
        action="store_true",
        help="Skip TLS certificate verification (development only).",
    )
    return p


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
