"""
``agtp`` — the AGTP CLI client.

A thin layer over ``client.core_client``. Argparse, output formatting,
exit codes, --match-check, and --negotiate live here; the actual
protocol work is delegated.

Usage::

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
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from client import core_client
from client.core_client import (
    DEFAULT_REGISTRY_URL,
    FORMAT_TO_ACCEPT,
    FetchResult,
    ResolutionError,
)
from core.handshake import format_outcome, match_from_manifest_dict
from core.identity import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
    from_dict,
)
from core.ids import AgentIDError, ParsedURI, parse_uri


DEFAULT_METHOD = "DESCRIBE"


# ---------------------------------------------------------------------------
# Body / parameter assembly.
# ---------------------------------------------------------------------------


def _coerce_param_value(raw: str) -> Any:
    """
    Try JSON-parse so numeric, boolean, or JSON-array/object values come
    through with the right type. Plain strings fall through.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def build_body(
    raw_data: Optional[str],
    params_file: Optional[Path],
    params: Optional[List[str]],
) -> Optional[Dict[str, Any]]:
    """
    Resolve the body dict from the three mutually-exclusive input modes.

    Returns the parsed body dict (or ``None`` when no body source is
    supplied). Raises ValueError when inputs conflict or are invalid.
    """
    sources = sum(
        1 for s in (raw_data, params_file, (params or None)) if s
    )
    if sources == 0:
        return None
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
        return parsed

    if params_file is not None:
        try:
            text = params_file.read_text(encoding="utf-8")
            parsed = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read --params-file: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--params-file content must be a JSON object")
        return parsed

    payload: Dict[str, Any] = {}
    for entry in params or []:
        if "=" not in entry:
            raise ValueError(f"--param expects key=value (got {entry!r})")
        key, _, value = entry.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--param key is empty (got {entry!r})")
        payload[key] = _coerce_param_value(value)
    return payload


# ---------------------------------------------------------------------------
# Output formatting.
# ---------------------------------------------------------------------------


def _format_body(result: FetchResult) -> str:
    """
    Pretty-print a FetchResult body. JSON content types get
    json.dumps(indent=2); other types pass through as-is.
    """
    body_text = result.body_text
    content_type = (result.headers.get("Content-Type") or "").lower()
    if "json" in content_type and result.parsed is not None:
        try:
            return json.dumps(result.parsed, indent=2)
        except (TypeError, ValueError):
            return body_text
    return body_text


def _open_in_browser(html: str, agent_id: Optional[str]) -> Path:
    short = (agent_id or "manifest")[:12]
    out_path = Path(tempfile.gettempdir()) / f"agtp-{short}.html"
    out_path.write_text(html, encoding="utf-8")
    webbrowser.open(out_path.as_uri())
    return out_path


# ---------------------------------------------------------------------------
# --match-check (no method invocation).
# ---------------------------------------------------------------------------


def _do_match_check(args, parsed: ParsedURI) -> int:
    if parsed.is_server_level:
        print(
            "error: --match-check requires a URI that addresses an agent "
            "(Form 1 or 1a); the supplied URI addresses a server",
            file=sys.stderr,
        )
        return 2

    agent = core_client.fetch(
        args.uri,
        fmt="json",
        registry_url=args.registry,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
        verbose=args.verbose,
    )
    if not agent.ok or agent.status_code != 200:
        print(
            f"error: DESCRIBE failed: "
            f"{agent.error or f'{agent.status_code} {agent.status_text}'}",
            file=sys.stderr,
        )
        return 1
    if not isinstance(agent.parsed, dict):
        print("error: agent response was not a JSON object", file=sys.stderr)
        return 1

    try:
        agent_doc = from_dict(agent.parsed)
    except ValueError as exc:
        print(f"error: malformed Agent Document: {exc}", file=sys.stderr)
        return 1

    host, port = agent.host, agent.port
    if host is None or port is None:
        print("error: missing resolved endpoint", file=sys.stderr)
        return 1
    manifest = core_client.fetch_manifest(
        host, port,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
        verbose=args.verbose,
    )
    if not manifest.ok or manifest.status_code != 200:
        print(
            f"error: server-level DISCOVER failed: "
            f"{manifest.error or f'{manifest.status_code} {manifest.status_text}'}",
            file=sys.stderr,
        )
        return 1
    if not isinstance(manifest.parsed, dict):
        print("error: malformed manifest", file=sys.stderr)
        return 1

    outcome = match_from_manifest_dict(agent_doc, manifest.parsed)
    print(format_outcome(outcome))
    return 0 if outcome.is_actionable else 1


# ---------------------------------------------------------------------------
# --negotiate (PROPOSE fallback when 452 / 462 returns).
# ---------------------------------------------------------------------------


def _peek_proposal_parameters(body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a parameters skeleton for a PROPOSE body from the original."""
    if not isinstance(body, dict):
        return {}
    return {k: type(v).__name__ for k, v in body.items()}


def _handle_negotiate(
    args,
    original_method: str,
    original_body: Optional[Dict[str, Any]],
) -> Optional[int]:
    """
    Issue a PROPOSE for ``original_method`` and react to the outcome.
    Returns an exit code when handled, or None to fall through.
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

    propose = core_client.invoke_method(
        args.uri,
        "PROPOSE",
        body=proposal,
        registry_url=args.registry,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
        verbose=args.verbose,
    )
    if not propose.ok:
        print(f"error: PROPOSE failed: {propose.error}", file=sys.stderr)
        return 1

    if propose.status_code == 200:
        synth = (propose.parsed or {}).get("synthesis") or {}
        synth_id = synth.get("synthesis_id")
        if not synth_id:
            print("error: PROPOSE 200 lacked synthesis_id", file=sys.stderr)
            return 1
        print(
            f"[client] PROPOSE accepted: synthesis {synth_id}; "
            f"retrying via synthesis",
            file=sys.stderr,
        )
        retry = core_client.invoke_method(
            args.uri,
            synth.get("target_method", "QUERY"),
            body=original_body,
            registry_url=args.registry,
            insecure=args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            synthesis_id=synth_id,
            verbose=args.verbose,
        )
        if not retry.ok:
            print(f"error: synthesis retry failed: {retry.error}",
                  file=sys.stderr)
            return 1
        print(_format_body(retry))
        return 0 if retry.status_code == 200 else 1

    if propose.status_code == 460:
        err = (propose.parsed or {}).get("error", {})
        print(
            f"PROPOSE refused: {err.get('reason', 'unknown')} - "
            f"{err.get('explanation', '')}",
            file=sys.stderr,
        )
        return 1

    if propose.status_code == 461:
        counter = (propose.parsed or {}).get("counter_proposal", {})
        suggested = counter.get("name")
        print(
            f"Server suggests {suggested}: {counter.get('description', '')}",
            file=sys.stderr,
        )
        if args.auto_accept_counter and suggested:
            print(
                f"[client] --auto-accept-counter: re-invoking with {suggested}",
                file=sys.stderr,
            )
            retry = core_client.invoke_method(
                args.uri,
                suggested,
                body=original_body,
                registry_url=args.registry,
                insecure=args.insecure,
                insecure_skip_verify=args.insecure_skip_verify,
                verbose=args.verbose,
            )
            if not retry.ok:
                print(f"error: retry failed: {retry.error}", file=sys.stderr)
                return 1
            print(_format_body(retry))
            return 0 if retry.status_code == 200 else 1
        # No auto-accept: surface the counter and let the user decide.
        print(_format_body(propose))
        return 1

    return None  # fall through; caller renders the original 452/462


# ---------------------------------------------------------------------------
# Run loop.
# ---------------------------------------------------------------------------


def run(args) -> int:
    try:
        parsed = parse_uri(args.uri)
    except AgentIDError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.match_check:
        return _do_match_check(args, parsed)

    # Server-level URI (Form 2) defaults to DISCOVER. DESCRIBE has no
    # meaning at the server level because it requires a Target-Agent.
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

    fmt = "yaml" if args.yaml else "html" if args.html else "json"

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

    try:
        body = build_body(args.data, args.params_file, args.param)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Default behavior (no method specified) is DESCRIBE for agent
    # URIs and DISCOVER for server URIs; both go through fetch().
    # An explicit method (or any method on a server URI) goes through
    # invoke_method().
    if not args.method and method_name in ("DESCRIBE", "DISCOVER"):
        result = core_client.fetch(
            args.uri,
            fmt=fmt,
            registry_url=args.registry,
            insecure=args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            verbose=args.verbose,
        )
    else:
        result = core_client.invoke_method(
            args.uri,
            method_name,
            body=body,
            registry_url=args.registry,
            insecure=args.insecure,
            insecure_skip_verify=args.insecure_skip_verify,
            verbose=args.verbose,
        )

    if not result.ok:
        print(f"error: {result.error}", file=sys.stderr)
        return 1

    # --negotiate fallback: triggers when the original method was
    # refused via the v2 inbound gate (452 / 462). The PROPOSE flow
    # may return its own exit code; if it returns None, we fall
    # through and surface the original refusal body.
    if (
        args.negotiate
        and result.status_code in (452, 462)
        and method_name != "PROPOSE"
    ):
        rc = _handle_negotiate(args, method_name, body)
        if rc is not None:
            return rc

    body_text = _format_body(result)

    if (
        method_name == "DESCRIBE"
        and fmt == "html"
        and not args.no_open
        and result.status_code == 200
    ):
        path = _open_in_browser(body_text, result.agent_id)
        if args.verbose:
            print(f"[client] opened {path}", file=sys.stderr)
        return 0

    if result.status_code != 200:
        print(
            f"AGTP/1.0 {result.status_code} {result.status_text}\n",
            file=sys.stderr,
        )
        print(body_text)
        return 1

    print(body_text)
    return 0


# ---------------------------------------------------------------------------
# Argparse.
# ---------------------------------------------------------------------------


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
            "automatically by re-invoking with the proposed method."
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
