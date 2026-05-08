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
from agtp.handshake import format_outcome, match_from_manifest_dict
from agtp.identity import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
    from_dict,
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
    """
    Map a ParsedURI to a (host, port) pair to connect to.

    Form 1a / Form 2 (host present) skip the registry. Form 1 (bare
    agent ID) goes through the configured registry.
    """
    if parsed.has_explicit_host:
        if verbose:
            print(
                f"[client] direct: {parsed.host}:{parsed.effective_port}",
                file=sys.stderr,
            )
        return parsed.host, parsed.effective_port
    if parsed.agent_id is None:
        # Should not happen since the URI grammar guarantees one of the two.
        raise ResolutionError("URI has neither agent ID nor host")
    return lookup_registry(parsed.agent_id, registry_url, verbose=verbose)


def send_method(
    agent_id: Optional[str],
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
    """
    Open an AGTP connection, send one method, return the response.

    ``agent_id`` is None for server-level requests (Form 2 URIs). In
    that case the Target-Agent header is omitted, which is the cue
    server.py uses to route DISCOVER to the manifest path.
    """
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

    headers: Dict[str, str] = {
        "Accept": accept,
        "Host": host,
    }
    if agent_id is not None:
        headers["Target-Agent"] = agent_id
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


def _handle_negotiate(
    args,
    parsed: ParsedURI,
    host: str,
    port: int,
    original_method: str,
    original_body: bytes,
) -> Optional[int]:
    """
    Issue a PROPOSE for ``original_method`` and react to the outcome.

    Returns an exit code if it handled the call (200 retry succeeded,
    460 refused, 461 not auto-accepted), or None if the caller should
    continue with the original 452/462 response.
    """
    print(
        f"[client] --negotiate: server refused {original_method}; "
        f"issuing PROPOSE",
        file=sys.stderr,
    )

    proposal = {
        "name": original_method,
        "parameters": _peek_proposal_parameters(original_body),
        "outcome": "auto-generated proposal from --negotiate",
        "description": f"client-driven proposal for {original_method}",
    }

    try:
        propose_resp = send_method(
            parsed.agent_id,
            host,
            port,
            "PROPOSE",
            accept="application/json",
            body=json.dumps(proposal).encode("utf-8"),
            body_content_type="application/json",
            use_tls=not args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            verbose=args.verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        print(f"error: PROPOSE failed: {exc}", file=sys.stderr)
        return 1

    if propose_resp.status_code == 200:
        return _retry_via_synthesis(
            args, parsed, host, port, original_body, propose_resp
        )

    if propose_resp.status_code == 460:
        # Refusal: print reason + explanation and exit 1.
        try:
            payload = json.loads(propose_resp.body_bytes.decode("utf-8"))
            err = payload.get("error", {})
            print(
                f"PROPOSE refused: {err.get('reason', 'unknown')} - "
                f"{err.get('explanation', '')}",
                file=sys.stderr,
            )
        except json.JSONDecodeError:
            print(propose_resp.body_bytes.decode("utf-8"), file=sys.stderr)
        return 1

    if propose_resp.status_code == 461:
        # Counter-proposal: print, optionally re-invoke under the
        # suggested method, otherwise exit 0 to let the caller decide.
        try:
            payload = json.loads(propose_resp.body_bytes.decode("utf-8"))
            counter = payload.get("counter_proposal", {})
        except json.JSONDecodeError:
            counter = {}
        suggested = counter.get("name")
        print(
            f"Server suggests {suggested}: "
            f"{counter.get('description', '')}",
            file=sys.stderr,
        )
        if args.auto_accept_counter and suggested:
            print(
                f"[client] --auto-accept-counter: re-invoking with {suggested}",
                file=sys.stderr,
            )
            try:
                retry = send_method(
                    parsed.agent_id,
                    host,
                    port,
                    suggested,
                    accept="application/json",
                    body=original_body,
                    body_content_type="application/json" if original_body else None,
                    use_tls=not args.insecure,
                    insecure_skip_verify=args.insecure_skip_verify,
                    verbose=args.verbose,
                )
            except (OSError, wire.WireFormatError) as exc:
                print(f"error: retry failed: {exc}", file=sys.stderr)
                return 1
            print(_format_response_body(retry))
            return 0 if retry.status_code == 200 else 1
        # No auto-accept: surface the counter and let the user decide.
        print(_format_response_body(propose_resp))
        return 1

    # Anything else (typically structural refusal): hand back to caller.
    return None


def _peek_proposal_parameters(body_bytes: bytes) -> Dict[str, Any]:
    """Extract a parameters skeleton for the PROPOSE body."""
    if not body_bytes:
        return {}
    try:
        data = json.loads(body_bytes.decode("utf-8"))
        if isinstance(data, dict):
            return {k: type(v).__name__ for k, v in data.items()}
    except json.JSONDecodeError:
        pass
    return {}


def _retry_via_synthesis(
    args,
    parsed: ParsedURI,
    host: str,
    port: int,
    original_body: bytes,
    propose_response: wire.AGTPResponse,
) -> int:
    """Use the synthesis_id from a 200 PROPOSE to retry the original call."""
    try:
        payload = json.loads(propose_response.body_bytes.decode("utf-8"))
        synth = payload["synthesis"]
        synth_id = synth["synthesis_id"]
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"error: malformed PROPOSE accept body: {exc}", file=sys.stderr)
        return 1

    print(
        f"[client] PROPOSE accepted: synthesis {synth_id}; retrying via synthesis",
        file=sys.stderr,
    )

    # Send any method name with the Synthesis-Id header. The server
    # rewrites it to the synthesis target. We pass the synthesis name
    # for clarity in logs.
    try:
        sock_method = synth.get("target_method", "QUERY")
        retry = _send_with_synthesis(
            args,
            host,
            port,
            sock_method,
            synth_id,
            original_body,
        )
    except (OSError, wire.WireFormatError) as exc:
        print(f"error: synthesis retry failed: {exc}", file=sys.stderr)
        return 1

    print(_format_response_body(retry))
    return 0 if retry.status_code == 200 else 1


def _send_with_synthesis(
    args,
    host: str,
    port: int,
    method_name: str,
    synthesis_id: str,
    body_bytes: bytes,
) -> wire.AGTPResponse:
    """Issue a request that names a synthesis via the Synthesis-Id header."""
    sock = socket.create_connection((host, port), timeout=10.0)
    if not args.insecure:
        ctx = ssl.create_default_context()
        if args.insecure_skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)
    headers = {
        "Accept": "application/json",
        "Host": host,
        "Synthesis-Id": synthesis_id,
    }
    if body_bytes:
        headers["Content-Type"] = "application/json"
    request = wire.AGTPRequest(
        method=method_name,
        headers=headers,
        body_bytes=body_bytes,
    )
    try:
        sock.sendall(request.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        try: sock.close()
        except OSError: pass


def _do_match_check(args, parsed: ParsedURI) -> int:
    """
    Implement --match-check: fetch the agent's identity, fetch the
    server manifest, compute the matching outcome, print it. Does not
    invoke any other method.

    Requires Form 1 or 1a (the URI must address an agent). Form 2
    URIs do not carry an agent identity.
    """
    if parsed.is_server_level:
        print(
            "error: --match-check requires a URI that addresses an agent "
            "(Form 1 or 1a); the supplied URI addresses a server",
            file=sys.stderr,
        )
        return 2

    try:
        host, port = resolve_target(parsed, args.registry, verbose=args.verbose)
    except ResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Fetch the Agent Document via DESCRIBE.
    try:
        agent_resp = send_method(
            parsed.agent_id,
            host,
            port,
            "DESCRIBE",
            accept=CONTENT_TYPE_JSON,
            use_tls=not args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            verbose=args.verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        print(f"error: DESCRIBE failed: {exc}", file=sys.stderr)
        return 1
    if agent_resp.status_code != 200:
        print(
            f"error: DESCRIBE returned {agent_resp.status_code} "
            f"{agent_resp.status_text}",
            file=sys.stderr,
        )
        return 1
    try:
        agent_doc = from_dict(json.loads(agent_resp.body_bytes.decode("utf-8")))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: malformed Agent Document: {exc}", file=sys.stderr)
        return 1

    # Fetch the Server Manifest via server-level DISCOVER.
    try:
        manifest_resp = send_method(
            agent_id=None,
            host=host,
            port=port,
            method_name="DISCOVER",
            accept="application/json",
            use_tls=not args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            verbose=args.verbose,
        )
    except (OSError, wire.WireFormatError) as exc:
        print(f"error: server-level DISCOVER failed: {exc}", file=sys.stderr)
        return 1
    if manifest_resp.status_code != 200:
        print(
            f"error: manifest DISCOVER returned {manifest_resp.status_code} "
            f"{manifest_resp.status_text}",
            file=sys.stderr,
        )
        return 1
    try:
        manifest_dict = json.loads(manifest_resp.body_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: malformed manifest: {exc}", file=sys.stderr)
        return 1

    outcome = match_from_manifest_dict(agent_doc, manifest_dict)
    print(format_outcome(outcome))
    return 0 if outcome.is_actionable else 1


def run(args) -> int:
    try:
        parsed = parse_uri(args.uri)
    except AgentIDError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.match_check:
        return _do_match_check(args, parsed)

    # Server-level URI (Form 2) defaults to DISCOVER, returning the
    # Server Manifest. DESCRIBE has no meaning at the server level
    # because it requires a Target-Agent.
    if parsed.is_server_level and not args.method:
        method_name = "DISCOVER"
    else:
        method_name = (args.method or DEFAULT_METHOD).upper()

    if parsed.is_server_level and method_name == "DESCRIBE":
        print(
            "error: DESCRIBE requires an agent target; this URI addresses "
            "the server (use a method like DISCOVER or supply an agent ID)",
            file=sys.stderr,
        )
        return 2

    if args.yaml:
        fmt = "yaml"
    elif args.html:
        fmt = "html"
    else:
        fmt = "json"

    if method_name != "DESCRIBE" and fmt in ("yaml", "html"):
        if parsed.is_server_level and method_name == "DISCOVER" and fmt == "html":
            print(
                "[client] HTML rendering of the Server Manifest is not "
                "implemented; falling back to JSON",
                file=sys.stderr,
            )
            fmt = "json"
        else:
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

    # Optional --negotiate fallback. Triggers when the original method
    # is refused via the v2 inbound gate (452/462). The client then
    # issues a PROPOSE naming the original method and reacts to the
    # three documented outcomes.
    if (
        args.negotiate
        and response.status_code in (452, 462)
        and method_name != "PROPOSE"
    ):
        rc = _handle_negotiate(
            args, parsed, host, port, method_name, body
        )
        if rc is not None:
            return rc

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
        "--match-check",
        action="store_true",
        help=(
            "Run the matching handshake against the server's manifest "
            "and report which of the agent's required methods the "
            "server exposes. No method is invoked."
        ),
    )
    parser.add_argument(
        "--negotiate",
        action="store_true",
        help=(
            "When the requested method is refused (452 / 462), "
            "automatically issue a PROPOSE for it. The PROPOSE "
            "outcome is then surfaced as a 200 (synthesis), 460 "
            "(refusal), or 461 (counter-proposal)."
        ),
    )
    parser.add_argument(
        "--auto-accept-counter",
        action="store_true",
        help=(
            "With --negotiate: accept a 461 counter-proposal "
            "automatically by re-invoking with the proposed method. "
            "Without this flag the counter is reported and the user "
            "decides on a subsequent invocation."
        ),
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
