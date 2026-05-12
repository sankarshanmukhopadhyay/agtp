"""
Interactive PROPOSE flow for the ``agtp`` CLI — endpoint editor.

This module walks the author through an *endpoint* — a (verb, path,
semantic block, parameters) tuple — with two cheap validators
backed by the curated verb catalog (``core/methods.json``):

  * :func:`core.methods.is_approved_verb` against the verb catalog
    (with :func:`core.methods.find_close_matches` for typo nudges),
  * :func:`core.path_grammar.validate_path` for the path field.

Three entry shapes:

  * ``agtp <uri> --propose --interactive``       walkthrough
  * ``agtp <uri> --propose -d '<json>'``         inline body
  * ``agtp <uri> --propose --params-file FILE``  JSON or YAML file

Response handling renders the three PROPOSE outcomes:

  * 200                              Synthesis accepted.
  * 422 negotiation-refused          Server refused the proposal.
  * 422 + counter_proposal body      Server suggested an alternative.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from client import core_client
from client.core_client import FetchResult
from core.endpoint import (
    ALL_CAPABILITIES,
    ALL_IMPACTS,
    SUGGESTED_ACTORS,
    SemanticBlock,
)
from core.methods import (
    APPROVED_VERBS,
    EMBEDDED_VERBS,
    LEGACY_VERBS,
    find_close_matches,
    is_approved_verb,
    is_legacy_verb,
)
from core.path_grammar import PathGrammarError, validate_path


# ---------------------------------------------------------------------------
# Glyph + colour helpers (degrade gracefully on cp1252 consoles).
# ---------------------------------------------------------------------------


def _supports_unicode() -> bool:
    enc = (sys.stdout.encoding or "").lower()
    return enc.startswith("utf")


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _ok() -> str:    return "✓" if _supports_unicode() else "[OK]"
def _bad() -> str:   return "✗" if _supports_unicode() else "[X]"
def _redo() -> str:  return "↻" if _supports_unicode() else "~"
def _bar() -> str:   return "─" if _supports_unicode() else "-"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _supports_color() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _supports_color() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _supports_color() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _supports_color() else s


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _supports_color() else s


# ---------------------------------------------------------------------------
# Per-field validation.
# ---------------------------------------------------------------------------


_PARAM_TYPES = ("string", "integer", "number", "boolean", "object", "array")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class _FieldOutcome:
    ok: bool
    message: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)


def _check_verb(value: str) -> _FieldOutcome:
    """Validate against the AGTP verb catalog."""
    name = value.strip().upper()
    if not name:
        return _FieldOutcome(ok=False, message="Verb is required.")
    if is_approved_verb(name):
        bucket = "embedded" if name in EMBEDDED_VERBS else "approved"
        return _FieldOutcome(
            ok=True,
            message=f"Verb {name!r} is in the {bucket} AGTP catalog.",
        )
    if is_legacy_verb(name):
        suggestions = find_close_matches(name)
        return _FieldOutcome(
            ok=False,
            message=(
                f"{name!r} is a legacy HTTP method. AGTP servers admit "
                f"it only via the policies.methods.legacy opt-in."
            ),
            suggestions=suggestions,
        )
    suggestions = find_close_matches(name)
    return _FieldOutcome(
        ok=False,
        message=f"{name!r} is not in the AGTP verb catalog.",
        suggestions=suggestions,
    )


def _check_path(value: str) -> _FieldOutcome:
    """Validate against the path grammar. Empty path is allowed."""
    text = value.strip()
    if not text:
        return _FieldOutcome(
            ok=True,
            message="Path omitted (optional).",
        )
    try:
        validate_path(text)
    except PathGrammarError as exc:
        return _FieldOutcome(
            ok=False,
            message=exc.message,
            suggestions=[
                "Move verb tokens out of the path; verbs belong in the method.",
            ] if exc.code == "verb-in-path" else [],
        )
    return _FieldOutcome(ok=True, message=f"Path {text!r} accepted.")


def _check_intent(value: str) -> _FieldOutcome:
    text = value.strip()
    if not text:
        return _FieldOutcome(ok=False, message="Intent is required.")
    if len(text) < 20:
        return _FieldOutcome(
            ok=False,
            message=f"Intent is {len(text)} chars; aim for at least 20.",
        )
    return _FieldOutcome(ok=True, message="Intent looks good.")


def _check_actor(value: str) -> _FieldOutcome:
    # ``actor`` is a free-form identifier per agtp-api §6. Refuse only
    # empty strings; the suggested-vocabulary set is offered as a
    # tooltip below, not enforced.
    text = value.strip()
    if not text:
        return _FieldOutcome(ok=False, message="Actor is required.")
    if text.lower() in SUGGESTED_ACTORS:
        return _FieldOutcome(ok=True, message=f"Actor {text!r} accepted.")
    return _FieldOutcome(
        ok=True,
        message=(
            f"Actor {text!r} accepted. (Suggested vocabulary: "
            f"{sorted(SUGGESTED_ACTORS)} — free-form is fine.)"
        ),
    )


def _check_outcome(value: str) -> _FieldOutcome:
    text = value.strip()
    if not text:
        return _FieldOutcome(ok=False, message="Outcome is required.")
    if len(text) < 20:
        return _FieldOutcome(
            ok=False,
            message=f"Outcome is {len(text)} chars; aim for at least 20.",
        )
    return _FieldOutcome(ok=True, message="Outcome looks good.")


def _check_capability(value: str) -> _FieldOutcome:
    text = value.strip().lower()
    if not text:
        return _FieldOutcome(ok=True, message="Capability omitted (optional).")
    if text in ALL_CAPABILITIES:
        return _FieldOutcome(ok=True, message=f"Capability {text!r} accepted.")
    return _FieldOutcome(
        ok=False,
        message=(
            f"Capability must be one of {sorted(ALL_CAPABILITIES)} "
            f"(got {value!r})."
        ),
    )


def _check_param_triple(value: str) -> _FieldOutcome:
    parts = value.split(":", 2)
    if len(parts) < 3:
        return _FieldOutcome(
            ok=False,
            message="Parameter format is name:type:description.",
        )
    name, type_, description = (p.strip() for p in parts)
    if not _PARAM_NAME_RE.match(name):
        return _FieldOutcome(
            ok=False,
            message=f"Parameter name {name!r} must be lowercase snake_case.",
        )
    if type_ not in _PARAM_TYPES:
        return _FieldOutcome(
            ok=False,
            message=(
                f"Parameter type must be one of {list(_PARAM_TYPES)} "
                f"(got {type_!r})."
            ),
        )
    if not description:
        return _FieldOutcome(ok=False, message="Description is required.")
    return _FieldOutcome(ok=True, message=f"Parameter {name!r} accepted.")


# ---------------------------------------------------------------------------
# Draft state.
# ---------------------------------------------------------------------------


@dataclass
class _Draft:
    verb: str = ""
    path: str = ""
    intent: str = ""
    actor: str = "agent"
    outcome: str = ""
    capability: Optional[str] = None
    required_params: List[Dict[str, Any]] = field(default_factory=list)
    optional_params: List[Dict[str, Any]] = field(default_factory=list)
    namespace: Optional[str] = None
    description: Optional[str] = None

    def to_proposal_body(self) -> Dict[str, Any]:
        """Render the draft as a wire-shaped PROPOSE body."""
        body: Dict[str, Any] = {
            "name": self.verb,
            "parameters": {p["name"]: p["type"] for p in self.required_params},
            "outcome": self.outcome,
        }
        if self.description:
            body["description"] = self.description
        if self.path:
            body["path"] = self.path
        # Semantic block — optional but useful for the synthesis runtime
        # to read intent/capability when matching recipes.
        sb_data: Dict[str, Any] = {}
        if self.intent:
            sb_data["intent"] = self.intent
        if self.actor:
            sb_data["actor"] = self.actor
        if self.outcome:
            sb_data["outcome"] = self.outcome
        if self.capability:
            sb_data["capability"] = self.capability
        if sb_data:
            body["semantic"] = sb_data
        if self.required_params:
            body["required_params"] = list(self.required_params)
        if self.optional_params:
            body["optional_params"] = list(self.optional_params)
        if self.namespace:
            body["namespace"] = self.namespace
        return body


# ---------------------------------------------------------------------------
# Prompting primitives.
# ---------------------------------------------------------------------------


def _ask(
    prompt: str,
    *,
    default: Optional[str] = None,
    validate_fn: Optional[Callable[[str], _FieldOutcome]] = None,
    out: Any = sys.stdout,
) -> str:
    suffix = f" [{default}]" if default is not None and default != "" else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}\n> ")
        except EOFError:
            raise KeyboardInterrupt() from None
        text = raw.strip()
        if not text and default is not None:
            text = default
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"', "`"):
            text = text[1:-1]
        if validate_fn is None:
            return text
        outcome = validate_fn(text)
        if outcome.ok:
            if outcome.message:
                print(_dim(f"  {_green(_ok())} {outcome.message}"), file=out)
            return text
        if outcome.message:
            print(_red(f"  {_bad()} {outcome.message}"), file=out)
        for s in outcome.suggestions:
            print(_dim(f"    suggestion: {s}"), file=out)
        print(file=out)


def _ask_param_list(
    prompt: str,
    *,
    keep: Optional[List[Dict[str, Any]]] = None,
    out: Any = sys.stdout,
) -> List[Dict[str, Any]]:
    """Repeatedly prompt for ``name:type:description`` triples; empty line ends."""
    print(prompt, file=out)
    items: List[Dict[str, Any]] = []
    first = True
    while True:
        try:
            raw = input("> ")
        except EOFError:
            raise KeyboardInterrupt() from None
        text = raw.strip()
        if not text:
            if first and keep:
                return list(keep)
            return items
        first = False
        outcome = _check_param_triple(text)
        if not outcome.ok:
            if outcome.message:
                print(_red(f"  {_bad()} {outcome.message}"), file=out)
            print(file=out)
            continue
        name, type_, description = (p.strip() for p in text.split(":", 2))
        items.append({"name": name, "type": type_, "description": description})


# ---------------------------------------------------------------------------
# Walkthrough.
# ---------------------------------------------------------------------------


def _walk_endpoint(
    uri: str,
    prefill: Optional[_Draft] = None,
    *,
    out: Any = sys.stdout,
) -> Optional[_Draft]:
    """Catalog-aware endpoint walkthrough. Returns a populated draft, or
    None when the user cancels."""
    pre = prefill or _Draft()

    if prefill is None:
        print(file=out)
        print(_bold(f"Compose an endpoint to propose to {uri}."), file=out)
        print(_dim("Press Ctrl-C at any prompt to abort."), file=out)
        print(file=out)
    else:
        print(_dim(
            "Edit fields. Press Enter to keep the current value."
        ), file=out)
        print(file=out)

    try:
        verb = _ask(
            "Verb (one of the AGTP catalog):",
            default=pre.verb or None,
            validate_fn=_check_verb,
            out=out,
        ).upper()
        path = _ask(
            "Path (optional, e.g. /orders/{order_id}):",
            default=pre.path or "",
            validate_fn=_check_path,
            out=out,
        )
        intent = _ask(
            "Intent (one sentence, agent-goal voice):",
            default=pre.intent or None,
            validate_fn=_check_intent,
            out=out,
        )
        actor = _ask(
            "Actor (agent / user / system):",
            default=pre.actor or "agent",
            validate_fn=_check_actor,
            out=out,
        ).lower()
        outcome = _ask(
            "Outcome (one sentence, post-condition):",
            default=pre.outcome or None,
            validate_fn=_check_outcome,
            out=out,
        )
        capability_raw = _ask(
            "Capability (discovery / retrieval / analysis / transaction "
            "/ modification / creation / notification — optional):",
            default=pre.capability or "",
            validate_fn=_check_capability,
            out=out,
        ).lower()
        capability = capability_raw or None

        if pre.required_params:
            print(_dim("Current required parameters:"), file=out)
            for p in pre.required_params:
                print(_dim(f"  {p['name']}:{p['type']}:{p['description']}"), file=out)
            print(_dim("Press Enter on the first line to keep these."), file=out)
        required_params = _ask_param_list(
            "Required parameters (one per line, format: "
            "name:type:description; empty line to finish):",
            keep=pre.required_params,
            out=out,
        )

        if pre.optional_params:
            print(_dim("Current optional parameters:"), file=out)
            for p in pre.optional_params:
                print(_dim(f"  {p['name']}:{p['type']}:{p['description']}"), file=out)
            print(_dim("Press Enter on the first line to keep these."), file=out)
        optional_params = _ask_param_list(
            "Optional parameters (one per line; empty line to skip):",
            keep=pre.optional_params,
            out=out,
        )

        namespace = _ask(
            "Namespace (your organization or project, optional):",
            default=pre.namespace or "",
            out=out,
        ) or None

    except KeyboardInterrupt:
        print(file=out)
        print(_dim("Cancelled."), file=out)
        return None

    return _Draft(
        verb=verb,
        path=path,
        intent=intent,
        actor=actor,
        outcome=outcome,
        capability=capability,
        required_params=required_params,
        optional_params=optional_params,
        namespace=namespace,
    )


# ---------------------------------------------------------------------------
# Preview rendering.
# ---------------------------------------------------------------------------


def render_preview(draft: _Draft, *, out: Any = sys.stdout) -> None:
    bar = _bar() * 60
    print(file=out)
    print(_bold(f"{_bar()*3} Proposed Endpoint {bar[18:]}"), file=out)
    print(f"Verb:        {draft.verb}", file=out)
    if draft.path:
        print(f"Path:        {draft.path}", file=out)
    print(f"Intent:      {draft.intent}", file=out)
    print(f"Actor:       {draft.actor}", file=out)
    print(f"Outcome:     {draft.outcome}", file=out)
    if draft.capability:
        print(f"Capability:  {draft.capability}", file=out)
    if draft.namespace:
        print(f"Namespace:   {draft.namespace}", file=out)
    print("Parameters:", file=out)
    if draft.required_params:
        print("  Required:", file=out)
        for p in draft.required_params:
            print(
                f"    {p['name']}:{p['type']} {_bar()} {p['description']}",
                file=out,
            )
    else:
        print("  Required: (none)", file=out)
    if draft.optional_params:
        print("  Optional:", file=out)
        for p in draft.optional_params:
            print(
                f"    {p['name']}:{p['type']} {_bar()} {p['description']}",
                file=out,
            )
    else:
        print("  Optional: (none)", file=out)
    print(_bold(bar), file=out)


# ---------------------------------------------------------------------------
# Submission + response rendering.
# ---------------------------------------------------------------------------


def _submit(uri: str, args, body: Dict[str, Any]) -> FetchResult:
    return core_client.invoke_method(
        uri,
        "PROPOSE",
        body=body,
        registry_url=args.registry,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
        verbose=args.verbose,
    )


def _render_propose_response(
    result: FetchResult,
    uri: str,
    *,
    draft: Optional[_Draft] = None,
    out: Any = sys.stdout,
) -> int:
    if not result.ok:
        print(_red(f"{_bad()} {result.error}"), file=out)
        return 1
    payload = result.parsed if isinstance(result.parsed, dict) else {}
    code = result.status_code
    err_code = (payload.get("error") or {}).get("code") if payload else None

    if code == 200:
        synth = payload.get("synthesis") or {}
        print(_green(f"{_ok()} Server accepted. Synthesis instantiated."), file=out)
        print(file=out)
        print(f"Synthesis ID:    {synth.get('synthesis_id', '(unknown)')}", file=out)
        target = synth.get("target_method")
        mapping = synth.get("parameter_mapping") or {}
        if target:
            print(f"Underlying method: {target}", file=out)
            arrow = f"{_bar()}>" if not _supports_unicode() else "→"
            for proposal_param, target_param in mapping.items():
                print(
                    f"  {target} {arrow} {target_param} mapped from "
                    f"{proposal_param!r}",
                    file=out,
                )
        if synth.get("description"):
            print(f"Description:     {synth['description']}", file=out)
        print(file=out)
        if draft is not None and draft.verb:
            print("Invoke this synthesis with:", file=out)
            print(
                f"  agtp {uri} {draft.verb} -d '{{...}}' "
                f"--synthesis-id {synth.get('synthesis_id', '<id>')}",
                file=out,
            )
        return 0

    # 422 with counter_proposal body → counter-proposal flow.
    if code == 422 and isinstance(payload.get("counter_proposal"), dict):
        counter = payload["counter_proposal"]
        suggested = counter.get("name", "(unknown)")
        print(_yellow(f"{_redo()} Server proposed an alternative."), file=out)
        print(file=out)
        if draft is not None:
            print(f"Server suggests: {suggested} instead of {draft.verb}", file=out)
        else:
            print(f"Server suggests: {suggested}", file=out)
        if counter.get("description"):
            print(f"Reason:          {counter['description']}", file=out)
        print(file=out)
        return _maybe_accept_counter(uri, suggested, draft, payload, out=out)

    # 422 with negotiation-refused body → plain refusal.
    if code == 422 and err_code == "negotiation-refused":
        err = payload.get("error", {}) or {}
        print(_red(f"{_bad()} Server refused negotiation."), file=out)
        print(file=out)
        print(f"Reason: {err.get('reason', '(unknown)')}", file=out)
        if err.get("explanation"):
            print(f"Detail: {err['explanation']}", file=out)
        return 1

    # 459 — verb not in catalog. Shouldn't happen if the walkthrough's
    # _check_verb gate worked, but render cleanly if it does.
    if code == 459:
        err = payload.get("error", {}) or {}
        print(_red(f"{_bad()} Server: 459 Method Grammar Violation"), file=out)
        if err.get("message"):
            print(f"  {err['message']}", file=out)
        sugs = err.get("suggestions") or []
        if sugs:
            print(f"  Suggestions: {', '.join(sugs)}", file=out)
        return 1

    # Anything else: surface the body.
    print(_red(f"{_bad()} {code} {result.status_text}"), file=out)
    if result.body_text:
        print(result.body_text, file=out)
    return 1


def _maybe_accept_counter(
    uri: str,
    suggested: str,
    draft: Optional[_Draft],
    payload: Dict[str, Any],
    *,
    out: Any,
) -> int:
    if draft is None or not suggested:
        return 1
    try:
        ans = input(
            f"Accept counter-proposal and re-propose as {suggested}? (y/N):\n> "
        ).strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        return 1
    print(file=out)
    print(_dim(f"Re-proposing as {suggested}..."), file=out)
    body = draft.to_proposal_body()
    body["name"] = suggested
    result = core_client.invoke_method(uri, "PROPOSE", body=body)
    if not result.ok:
        print(_red(f"{_bad()} Re-proposal failed: {result.error}"), file=out)
        return 1
    return _render_propose_response(result, uri, draft=None, out=out)


# ---------------------------------------------------------------------------
# Confirm / edit / save flow.
# ---------------------------------------------------------------------------


def _save_draft(body: Dict[str, Any], path_str: str, *, out: Any) -> int:
    target = Path(path_str.strip()).expanduser()
    ext = target.suffix.lower()
    if ext == ".json":
        text = json.dumps(body, indent=2)
    else:
        if ext not in (".yaml", ".yml"):
            print(_yellow(
                f"Warning: extension {ext!r} not .yaml/.yml/.json; "
                f"saving as YAML."
            ), file=out)
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print(_red(
                "PyYAML is not installed; install with `pip install pyyaml` "
                "or save as .json."
            ), file=out)
            return 2
        text = yaml.safe_dump(body, sort_keys=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return 0


def _confirm_or_edit_save(
    draft: _Draft,
    uri: str,
    *,
    out: Any = sys.stdout,
) -> str:
    """Render the preview and prompt for the four-state confirmation.

    Returns one of: "submit" / "cancel" / "edit" / "saved".
    """
    while True:
        render_preview(draft, out=out)
        print(file=out)
        try:
            ans = input(
                f"Submit this PROPOSE to {uri}? "
                f"(y/N/e to edit/s to save):\n> "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "cancel"
        if ans in ("y", "yes"):
            return "submit"
        if ans in ("e", "edit"):
            return "edit"
        if ans in ("s", "save"):
            default_path = f"./{draft.verb.lower()}.endpoint.yaml"
            try:
                path_str = input(f"Save as: [{default_path}]\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                return "cancel"
            path_str = path_str or default_path
            rc = _save_draft(draft.to_proposal_body(), path_str, out=out)
            if rc != 0:
                continue
            print(_green(f"{_ok()} Saved."), file=out)
            print("Submit later with:", file=out)
            print(
                f"  agtp {uri} --propose --params-file {path_str}",
                file=out,
            )
            return "saved"
        return "cancel"


def _interactive_propose(
    uri: str,
    args,
    *,
    out: Any = sys.stdout,
) -> Optional[_Draft]:
    """Walk → preview → confirm loop; returns the draft on submit, else None."""
    draft: Optional[_Draft] = None
    while True:
        draft = _walk_endpoint(uri, prefill=draft, out=out)
        if draft is None:
            return None
        action = _confirm_or_edit_save(draft, uri, out=out)
        if action == "submit":
            return draft
        if action == "edit":
            continue
        return None


# ---------------------------------------------------------------------------
# Non-interactive body loaders.
# ---------------------------------------------------------------------------


def _load_proposal_body(args) -> Dict[str, Any]:
    """Load a non-interactive proposal body from -d or --params-file."""
    if args.data is not None:
        try:
            parsed = json.loads(args.data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"-d value is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("-d JSON must be an object")
        return parsed

    if args.params_file is not None:
        path = args.params_file
        ext = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8")
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
                raise ValueError(f"invalid YAML: {exc}") from exc
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--params-file content must be a mapping")
        return parsed

    raise ValueError(
        "--propose requires one of --interactive, -d <json>, or "
        "--params-file <path>"
    )


def _validate_non_interactive_body(body: Dict[str, Any]) -> None:
    """
    Cheap pre-flight against the catalog so the caller sees the
    refusal locally rather than as a server-side 459 round-trip.
    """
    name = str(body.get("name", "")).upper()
    if not name:
        raise ValueError("proposal body lacks a 'name' field")
    if not is_approved_verb(name):
        suggestions = find_close_matches(name)
        hint = (
            f" Did you mean {', '.join(suggestions)}?"
            if suggestions else ""
        )
        raise ValueError(
            f"{name!r} is not in the AGTP verb catalog.{hint}"
        )
    path = body.get("path")
    if path:
        try:
            validate_path(str(path))
        except PathGrammarError as exc:
            raise ValueError(
                f"path {path!r} violates path grammar: {exc.message}"
            ) from exc


# ---------------------------------------------------------------------------
# Top-level entry: run_propose.
# ---------------------------------------------------------------------------


def run_propose(args, *, out: Any = sys.stdout) -> int:
    """
    Entry point for ``--propose``. Dispatches between interactive
    walkthrough and non-interactive (-d / --params-file) shapes.
    """
    uri = args.uri

    if args.interactive:
        draft = _interactive_propose(uri, args, out=out)
        if draft is None:
            return 1
        body = draft.to_proposal_body()
    else:
        try:
            body = _load_proposal_body(args)
        except ValueError as exc:
            print(_red(f"error: {exc}"), file=sys.stderr)
            return 2
        try:
            _validate_non_interactive_body(body)
        except ValueError as exc:
            print(_red(f"{_bad()} {exc}"), file=sys.stderr)
            return 1
        # Build a partial draft for response rendering (synthesis_id
        # invocation hint, counter-proposal re-issue body).
        draft = _Draft(
            verb=str(body.get("name", "")).upper(),
            path=str(body.get("path", "") or ""),
            outcome=str(body.get("outcome", "") or ""),
            description=body.get("description"),
        )

    print(file=out)
    print(_dim("Submitting..."), file=out)
    result = _submit(uri, args, body)
    return _render_propose_response(result, uri, draft=draft, out=out)


__all__ = [
    "render_preview",
    "run_propose",
]
