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
from core.methods import find_close_matches, is_approved_verb
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
        ext = params_file.suffix.lower()
        try:
            text = params_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"could not read --params-file: {exc}") from exc
        if ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ValueError(
                    "PyYAML is required for --params-file *.yaml. "
                    "Install with `pip install pyyaml`."
                ) from exc
            try:
                parsed = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ValueError(f"invalid YAML in --params-file: {exc}") from exc
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"--params-file is not valid JSON: {exc}"
                ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("--params-file content must be a mapping")
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


def _print_catalog_warning(
    result: FetchResult,
    method_name: str,
) -> None:
    """Phase-6 advisory: when the response carries an
    ``AGTP-Catalog-Warning`` header, surface it to the operator so
    they know to migrate. The header is purely advisory; the
    request still processed normally on the server side.

    Header shape: ``deprecated; successor=AUDIT; removed_in=2.0.0``.
    The CLI parses the parts and prints a one-line YELLOW warning
    to stderr. Coloring is best-effort (skipped on non-TTY stderr
    so transcript captures stay clean).
    """
    header = ""
    for k, v in (result.headers or {}).items():
        if k.lower() == "agtp-catalog-warning":
            header = v
            break
    if not header:
        return
    # Parse the structured shape so we can surface a readable line.
    parts = [p.strip() for p in header.split(";") if p.strip()]
    fields: Dict[str, str] = {}
    label = ""
    for p in parts:
        if "=" in p:
            k, _, v = p.partition("=")
            fields[k.strip().lower()] = v.strip()
        else:
            label = p
    successor = fields.get("successor")
    removed_in = fields.get("removed_in")
    bits = [f"{method_name} is {label or 'deprecated'}."]
    if successor:
        bits.append(f"Successor: {successor}.")
    if removed_in:
        bits.append(f"Removed in: {removed_in}.")
    msg = " ".join(bits)

    # ANSI yellow when stderr is a TTY; otherwise plain text so
    # CI / piped output stays grep-friendly.
    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        prefix = "\x1b[33mWARNING:\x1b[0m"
    else:
        prefix = "WARNING:"
    print(f"{prefix} {msg}", file=sys.stderr)


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
# --negotiate (PROPOSE fallback when the server soft-denies via 403).
#
# The historical 452 / 462 codes have been folded into 403 with a
# structured error.code field; we trigger negotiation on the same
# soft-deny semantics under the new wire numbers.
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

    payload = propose.parsed if isinstance(propose.parsed, dict) else {}
    err_code = (payload.get("error") or {}).get("code")

    # 422 with counter_proposal body → counter-proposal flow.
    if propose.status_code == 422 and isinstance(payload.get("counter_proposal"), dict):
        counter = payload["counter_proposal"]
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

    # 422 with negotiation-refused body → plain refusal.
    if propose.status_code == 422 and err_code == "negotiation-refused":
        err = payload.get("error", {}) or {}
        print(
            f"PROPOSE refused: {err.get('reason', 'unknown')} - "
            f"{err.get('explanation', '')}",
            file=sys.stderr,
        )
        return 1

    return None  # fall through; caller renders the original soft-deny


# ---------------------------------------------------------------------------
# --grammar-check (catalog probe).
# ---------------------------------------------------------------------------


def _do_grammar_check(args, parsed: ParsedURI) -> int:
    """
    Probe whether a verb is admissible on the target server.

    There is no separate probe header for verb admission; the catalog
    gate at the top of every dispatch does the admission check, so
    this flag is sugar that asks the question without committing the
    operator to a real invocation.

    Two-stage behavior:

      1. **Local check** against ``core/methods.json``. If the method
         isn't even in the catalog locally, refuse immediately with
         close-match suggestions — no network call.
      2. **Live probe** against the server with an empty body. The
         status code carries the answer:

           * **200 / 400 / 422** — the verb passed the dispatcher's
             gates and reached the handler. 400 is the common case
             for a probe with no body (the handler reports missing
             required parameters); we treat that as proof of
             admission.
           * **459 method-violation** — the server's
             catalog (or its ``policies.methods``) refused the
             name. Suggestions printed.
           * **405 method-not-implemented** — the verb is in the
             catalog but no handler is registered. On an interactive
             TTY, the CLI offers to chain into
             ``--propose --interactive`` so the operator can
             negotiate a synthesis in one command.
           * **405 method-not-allowed-by-policy** — the server's
             ``policies.methods`` actively disallows the verb.
           * **403** — the agent's capability or the server's policy
             refuses the call (no PROPOSE chain offered).
    """
    method_name = args.method.upper()

    # Local catalog check first — fast feedback, no network round-trip.
    # Lookups are imported at module level so tests can monkeypatch
    # ``cli_main.is_approved_verb`` to exercise the rare case where
    # the local catalog disagrees with the server.
    if not is_approved_verb(method_name):
        suggestions = find_close_matches(method_name)
        print(
            f"459 (local catalog): {method_name!r} is not in the "
            f"AGTP verb catalog.",
            file=sys.stderr,
        )
        if suggestions:
            print(
                f"  suggestions: {', '.join(suggestions)}",
                file=sys.stderr,
            )
        return 1

    result = core_client.invoke_method(
        args.uri,
        method_name,
        registry_url=args.registry,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
        verbose=args.verbose,
    )
    if not result.ok:
        print(f"error: {result.error}", file=sys.stderr)
        return 1

    payload: Dict[str, Any] = (
        result.parsed if isinstance(result.parsed, dict) else {}
    )
    code = result.status_code
    err = payload.get("error") or {}

    # Admitted — verb cleared every dispatcher gate. 400/422 are the
    # common shapes for a probe with no body; both prove admission.
    if code in (200, 400, 422):
        print(
            f"✓ {method_name} is admitted by {parsed.host or args.uri}.",
        )
        if code == 400:
            print("  (probe carried no body, so the handler reported "
                  "missing required parameters — this is expected.)")
        return 0

    # Catalog refusal at the server (server's catalog or policy
    # disagreed with the local check, or the legacy/embedded carve-out
    # changed the answer). Render suggestions if present.
    if code == 459:
        print(
            f"459 Method Violation: {method_name!r} is not "
            f"admissible on this server.",
            file=sys.stderr,
        )
        suggestions = err.get("suggestions") or []
        if suggestions:
            print(
                f"  suggestions: {', '.join(suggestions)}",
                file=sys.stderr,
            )
        return 1

    # 405 splits into "no handler registered" (catalog admits but
    # nothing is bound) vs "policy refuses" (policies.methods disallows).
    # The first invites a PROPOSE; the second doesn't.
    if code == 405:
        explanation = err.get("explanation") or ""
        if err.get("code") == "method-not-implemented":
            print(
                f"405 Method Not Allowed: {method_name} is in the "
                f"AGTP catalog but no handler is registered on this "
                f"server.",
            )
            if sys.stdin.isatty():
                try:
                    ans = input(
                        f"Want to PROPOSE {method_name}? (y/N): "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                if ans in ("y", "yes"):
                    args.propose = True
                    args.interactive = True
                    from client.cli.propose import run_propose
                    return run_propose(args)
            return 0
        # policies.methods actively refused.
        print(
            f"405 Method Not Allowed: {method_name} is refused by "
            f"this server's policies.methods.",
            file=sys.stderr,
        )
        if explanation:
            print(f"  {explanation}", file=sys.stderr)
        return 1

    if code == 403:
        print(
            f"403 Forbidden: {err.get('code', 'unknown')}",
            file=sys.stderr,
        )
        if err.get("explanation"):
            print(f"  {err['explanation']}", file=sys.stderr)
        return 1

    # Anything else: surface it.
    print(
        f"AGTP/1.0 {result.status_code} {result.status_text}",
        file=sys.stderr,
    )
    print(_format_body(result))
    return 1


# ---------------------------------------------------------------------------
# Run loop.
# ---------------------------------------------------------------------------


def run(args) -> int:
    try:
        parsed = parse_uri(args.uri)
    except AgentIDError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.propose:
        if args.method:
            print(
                "error: --propose is mutually exclusive with a positional "
                "method argument",
                file=sys.stderr,
            )
            return 2
        if not (args.interactive or args.data or args.params_file):
            print(
                "error: --propose requires one of --interactive, -d, or "
                "--params-file",
                file=sys.stderr,
            )
            return 2
        from client.cli.propose import run_propose
        return run_propose(args)

    if args.interactive:
        print(
            "error: --interactive is only meaningful with --propose",
            file=sys.stderr,
        )
        return 2

    if args.grammar_check:
        if not args.method:
            print(
                "error: --grammar-check requires a positional method "
                "argument (the verb to probe)",
                file=sys.stderr,
            )
            return 2
        if args.match_check or args.negotiate:
            print(
                "error: --grammar-check is mutually exclusive with "
                "--match-check and --negotiate",
                file=sys.stderr,
            )
            return 2
        return _do_grammar_check(args, parsed)

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
    # refused via the v2 inbound gate. Soft-deny refusals
    # (method-not-permitted-for-agent, wildcards-refused) now ride
    # 403 with a structured error.code; the dispatcher's "method not
    # implemented" path stays at 501. The PROPOSE flow may return
    # its own exit code; if it returns None, we fall through and
    # surface the original refusal body.
    err_code = None
    if isinstance(result.parsed, dict):
        err_code = (result.parsed.get("error") or {}).get("code")
    softdeny_codes = {"method-not-permitted-for-agent", "wildcards-refused"}
    if (
        args.negotiate
        and result.status_code == 403
        and err_code in softdeny_codes
        and method_name != "PROPOSE"
    ):
        rc = _handle_negotiate(args, method_name, body)
        if rc is not None:
            return rc

    body_text = _format_body(result)

    # Phase-6: surface AGTP-Catalog-Warning advisories to the user.
    # The header rides on every response for a deprecated verb; the
    # CLI prints a short yellow warning to stderr before the body so
    # automated transcripts capture it without parsing JSON.
    _print_catalog_warning(result, method_name)

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
            "  agtp agtp://d8dc6f0d...@agents.agtp.io\n"
            "  agtp agtp://d8dc6f0d... --propose -i\n"
            "  agtp agtp://d8dc6f0d... --propose --params-file method.yaml"
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
        help=(
            "Read the request body from a file. JSON by default; "
            "files with .yaml/.yml extension are parsed as YAML "
            "(requires pyyaml)."
        ),
    )

    parser.add_argument(
        "--propose",
        action="store_true",
        help=(
            "Submit a PROPOSE for a new method. Pair with -d / "
            "--params-file to send a fixture, or with --interactive "
            "to walk through composition."
        ),
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "With --propose: walk through method composition "
            "interactively, with per-field validation and a "
            "preview before submission."
        ),
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
        "--grammar-check",
        action="store_true",
        help=(
            "Probe whether the verb is in the AGTP catalog and "
            "admissible on the target server. Refuses unknown verbs "
            "locally with close-match suggestions; 459 / 405 / 403 "
            "from the server are rendered inline. On 405 "
            "method-not-implemented and an interactive TTY, offers "
            "to chain into --propose --interactive."
        ),
    )
    parser.add_argument(
        "--negotiate",
        action="store_true",
        help=(
            "When the requested method is soft-denied (403 with "
            "error.code='method-not-permitted-for-agent' or "
            "'wildcards-refused'), automatically issue a PROPOSE for it. "
            "The PROPOSE outcome is then surfaced as a 200 (synthesis), "
            "422 negotiation-refused, or 422 with a counter_proposal body."
        ),
    )
    parser.add_argument(
        "--auto-accept-counter",
        action="store_true",
        help=(
            "With --negotiate: accept a 422 counter-proposal "
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
