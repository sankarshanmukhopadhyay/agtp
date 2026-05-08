"""
Interactive PROPOSE flow for the ``agtp`` CLI.

Wires the AMG composer into the main client so authors can walk
through method composition, validate locally, and submit a PROPOSE
in one command. Three entry shapes:

  * ``agtp <uri> --propose --interactive``       walkthrough
  * ``agtp <uri> --propose -d '<json>'``         inline body
  * ``agtp <uri> --propose --params-file FILE``  JSON or YAML file

The walkthrough validates each field as it's entered (re-prompts on
failure with suggestions from the composer's suggestion engine),
shows a preview, and offers four exits: yes / no / edit / save.

Response handling renders 200 / 460 / 461 with appropriate detail
and (for 461) prompts to accept the counter-proposal.

This module is deliberately separate from ``client.cli.main`` so the
main CLI stays focused on method invocation; ``main.run()`` dispatches
to ``run_propose`` when ``--propose`` is set.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from client import core_client
from client.amg import (
    ALL_ACTORS,
    ALL_CAPABILITIES,
    ALL_IMPACT_TIERS,
    AMGMethodSpec,
    CompositionError,
    ParamSpec,
    SemanticBlock,
    SubstitutionHint,
    compose_method,
    suggest_fix,
)
from client.amg.grammar import PARAM_TYPES
from client.amg.reserved import (
    EMBEDDED_METHODS,
    HTTP_METHODS,
    STOPLIST,
    stoplist_suggestion,
)
from client.amg.validator import validate
from client.core_client import FetchResult


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
#
# We deliberately use small inline checks for each field so the user
# gets feedback at the moment they enter a value, not at the end of
# composition. The composer / validator still run on the full spec at
# preview time; that's where cross-field warnings (irreversible +
# low confidence, description == intent verbatim) surface.
# ---------------------------------------------------------------------------


import re

_LEXICAL_RE = re.compile(r"^[A-Z]{3,32}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class _FieldOutcome:
    ok: bool
    message: Optional[str] = None       # human-readable status line
    suggestions: List[str] = field(default_factory=list)


def _check_name(value: str) -> _FieldOutcome:
    name = value.strip()
    if not _LEXICAL_RE.match(name):
        return _FieldOutcome(
            ok=False,
            message=f"{name!r} is not a valid method name "
                    f"(must be 3-32 uppercase ASCII letters).",
            suggestions=(
                [f"Try {name.upper()!r}."]
                if name and name.isalpha()
                else ["Use only uppercase A-Z, no digits or punctuation."]
            ),
        )
    if name in HTTP_METHODS:
        return _FieldOutcome(
            ok=False,
            message=f"{name!r} is reserved as an HTTP method.",
            suggestions=[
                "AGTP and HTTP method semantics differ. "
                "Choose a non-HTTP verb (e.g. FETCH, RETRIEVE, CREATE).",
            ],
        )
    if name in EMBEDDED_METHODS:
        return _FieldOutcome(
            ok=False,
            message=f"{name!r} is one of the 12 embedded AGTP methods.",
            suggestions=[
                "User-defined methods cannot register an embedded "
                "name. Pick a different verb.",
            ],
        )
    if name in STOPLIST:
        suggestion = stoplist_suggestion(name)
        # Reach into the substitution catalog for catalog candidates
        # that share the offending name's prefix.
        from client.amg.substitution import DEFAULT_SUBSTITUTIONS
        catalog_hits = sorted({
            m for ec in DEFAULT_SUBSTITUTIONS for m in ec.members
            if m.startswith(name[:3]) and m not in STOPLIST
        })
        sugs = [suggestion]
        if catalog_hits:
            sugs.append(f"Catalog candidates: {', '.join(catalog_hits)}.")
        return _FieldOutcome(
            ok=False,
            message=f"{name!r} is in the AMG stoplist "
                    f"(describes a state, not an action).",
            suggestions=sugs,
        )
    return _FieldOutcome(
        ok=True,
        message="Name passes lexical and stoplist checks.",
    )


def _check_intent(value: str) -> _FieldOutcome:
    text = value.strip()
    if not text:
        return _FieldOutcome(ok=False, message="Intent is required.")
    if len(text) < 20:
        return _FieldOutcome(
            ok=False,
            message=f"Intent is {len(text)} chars; aim for at least 20.",
            suggestions=[
                "Describe what the agent wants to happen, in one sentence.",
            ],
        )
    lowered = text.lower()
    for stub in ("todo", "fixme", "stub", "placeholder", "lorem ipsum"):
        if stub in lowered:
            return _FieldOutcome(
                ok=False,
                message=f"Intent appears to be a stub ({stub!r}).",
                suggestions=[
                    "Replace placeholder text with a real one-sentence intent."
                ],
            )
    return _FieldOutcome(ok=True, message="Intent looks good.")


def _check_actor(value: str) -> _FieldOutcome:
    text = value.strip().lower()
    if text in ALL_ACTORS:
        return _FieldOutcome(ok=True, message=f"Actor {text!r} accepted.")
    return _FieldOutcome(
        ok=False,
        message=f"Actor must be one of {sorted(ALL_ACTORS)} "
                f"(got {value!r}).",
    )


def _check_outcome(value: str) -> _FieldOutcome:
    text = value.strip()
    if not text:
        return _FieldOutcome(ok=False, message="Outcome is required.")
    if len(text) < 20:
        return _FieldOutcome(
            ok=False,
            message=f"Outcome is {len(text)} chars; aim for at least 20.",
            suggestions=[
                "Describe the post-condition: what callers will receive."
            ],
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
        message=f"Capability must be one of {sorted(ALL_CAPABILITIES)} "
                f"(got {value!r}).",
    )


def _check_confidence(value: str) -> _FieldOutcome:
    text = value.strip()
    if not text:
        return _FieldOutcome(ok=True, message="Confidence omitted (optional).")
    try:
        f = float(text)
    except ValueError:
        return _FieldOutcome(
            ok=False, message=f"Confidence must be a number (got {value!r}).",
        )
    if not (0.0 <= f <= 1.0):
        return _FieldOutcome(
            ok=False,
            message=f"Confidence must be in [0.0, 1.0] (got {f}).",
        )
    return _FieldOutcome(ok=True, message=f"Confidence {f} accepted.")


def _check_impact_tier(value: str) -> _FieldOutcome:
    text = value.strip().lower()
    if not text:
        return _FieldOutcome(ok=True, message="Impact tier omitted (optional).")
    if text in ALL_IMPACT_TIERS:
        return _FieldOutcome(ok=True, message=f"Impact tier {text!r} accepted.")
    return _FieldOutcome(
        ok=False,
        message=f"Impact tier must be one of {sorted(ALL_IMPACT_TIERS)} "
                f"(got {value!r}).",
    )


def _check_idempotent(value: str) -> _FieldOutcome:
    text = value.strip().lower()
    if text in ("y", "yes", "true", "1"):
        return _FieldOutcome(ok=True, message="Marked idempotent.")
    if text in ("n", "no", "false", "0", ""):
        return _FieldOutcome(ok=True, message="Marked non-idempotent.")
    return _FieldOutcome(
        ok=False, message=f"Answer y or n (got {value!r}).",
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
    if type_ not in PARAM_TYPES:
        return _FieldOutcome(
            ok=False,
            message=f"Parameter type must be one of {sorted(PARAM_TYPES)} "
                    f"(got {type_!r}).",
        )
    if not description:
        return _FieldOutcome(
            ok=False,
            message="Parameter description is required.",
        )
    return _FieldOutcome(ok=True, message=f"Parameter {name!r} accepted.")


# ---------------------------------------------------------------------------
# In-memory representation of an in-progress proposal. Used as the
# baseline for edit-mode re-entry.
# ---------------------------------------------------------------------------


@dataclass
class _Draft:
    name: str = ""
    intent: str = ""
    actor: str = "agent"
    outcome: str = ""
    capability: Optional[str] = None
    confidence_guidance: Optional[float] = None
    impact_tier: Optional[str] = None
    is_idempotent: bool = False
    required_params: List[Dict[str, Any]] = field(default_factory=list)
    optional_params: List[Dict[str, Any]] = field(default_factory=list)
    namespace: Optional[str] = None
    substitutes_for: Optional[str] = None
    substitution_conditions: Optional[str] = None
    description: Optional[str] = None
    error_codes: List[int] = field(default_factory=lambda: [400, 405, 422])

    def to_compose_kwargs(self) -> Dict[str, Any]:
        substitutes = (
            [SubstitutionHint(
                target_method=self.substitutes_for,
                conditions=self.substitution_conditions,
            )]
            if self.substitutes_for else None
        )
        return dict(
            intent=self.intent,
            actor=self.actor,
            outcome=self.outcome,
            capability=self.capability,
            confidence_guidance=self.confidence_guidance,
            impact_tier=self.impact_tier,
            is_idempotent=self.is_idempotent,
            description=self.description,
            namespace=self.namespace,
            required_params=[ParamSpec(**p) for p in self.required_params],
            optional_params=[ParamSpec(**p) for p in self.optional_params],
            error_codes=list(self.error_codes),
            substitutes_for=substitutes,
        )

    def to_dict_for_save(self) -> Dict[str, Any]:
        """Compose-then-export form (for `s` save action)."""
        spec = compose_method(self.name, **self.to_compose_kwargs())
        return spec.to_dict()


# ---------------------------------------------------------------------------
# Prompting primitives. Mockable via builtins.input in tests.
# ---------------------------------------------------------------------------


def _ask(
    prompt: str,
    *,
    default: Optional[str] = None,
    validate_fn: Optional[Callable[[str], _FieldOutcome]] = None,
    out: Any = sys.stdout,
) -> str:
    """
    Ask a single question. Loops until ``validate_fn`` returns ok=True.
    The result is the (stripped) raw user input — callers post-process.
    Pressing Enter on a default-bearing prompt accepts the default.
    """
    suffix = f" [{default}]" if default is not None and default != "" else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}\n> ")
        except EOFError:
            # input() raises EOFError on closed stdin; treat as cancel.
            raise KeyboardInterrupt() from None
        text = raw.strip()
        if not text and default is not None:
            text = default
        # Trim surrounding quotes (single, double, or backtick).
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
            print(_dim(f"    {s}"), file=out)
        print(file=out)


def _ask_list(
    prompt: str,
    *,
    validate_fn: Callable[[str], _FieldOutcome],
    out: Any = sys.stdout,
) -> List[Dict[str, Any]]:
    """
    Repeatedly ask for ``name:type:description`` triples; empty line ends.
    Returns a list of dicts ready to pass to ParamSpec.
    """
    print(prompt, file=out)
    items: List[Dict[str, Any]] = []
    while True:
        try:
            raw = input("> ")
        except EOFError:
            raise KeyboardInterrupt() from None
        text = raw.strip()
        if not text:
            return items
        outcome = validate_fn(text)
        if not outcome.ok:
            if outcome.message:
                print(_red(f"  {_bad()} {outcome.message}"), file=out)
            for s in outcome.suggestions:
                print(_dim(f"    {s}"), file=out)
            print(file=out)
            continue
        name, type_, description = (p.strip() for p in text.split(":", 2))
        items.append({
            "name": name, "type": type_, "description": description
        })


# ---------------------------------------------------------------------------
# Walkthrough.
# ---------------------------------------------------------------------------


def _walk_compose(
    uri: str,
    prefill: Optional[_Draft] = None,
    *,
    out: Any = sys.stdout,
) -> Optional[_Draft]:
    """
    Run the interactive walkthrough. Returns a populated draft, or
    None if the user cancels (via Ctrl-C / EOF).

    When ``prefill`` is supplied, every prompt offers the previous
    value as the default. This is the edit-mode entry point.
    """
    pre = prefill or _Draft()
    if prefill is None:
        print(file=out)
        print(_bold(f"Compose a new method to propose to {uri}."), file=out)
        print(_dim("Press Ctrl-C at any prompt to abort."), file=out)
        print(file=out)
    else:
        print(_dim(
            "Edit fields. Press Enter to keep the current value."
        ), file=out)
        print(file=out)

    try:
        name = _ask(
            "Method name (uppercase, single token):",
            default=pre.name or None,
            validate_fn=_check_name,
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
            "Capability (discovery / transaction / modification / "
            "retrieval / analysis / notification):",
            default=pre.capability or "",
            validate_fn=_check_capability,
            out=out,
        ).lower()
        capability = capability_raw or None

        confidence_raw = _ask(
            "Confidence guidance (0.0 - 1.0):",
            default=(
                str(pre.confidence_guidance)
                if pre.confidence_guidance is not None else ""
            ),
            validate_fn=_check_confidence,
            out=out,
        )
        confidence = float(confidence_raw) if confidence_raw else None

        impact_raw = _ask(
            "Impact tier (informational / reversible / irreversible):",
            default=pre.impact_tier or "",
            validate_fn=_check_impact_tier,
            out=out,
        ).lower()
        impact_tier = impact_raw or None

        idempotent_raw = _ask(
            "Is this method idempotent? (y/N):",
            default="y" if pre.is_idempotent else "n",
            validate_fn=_check_idempotent,
            out=out,
        ).lower()
        is_idempotent = idempotent_raw in ("y", "yes", "true", "1")

        # Required params — we re-ask the whole list rather than item
        # by item, since edit mode wants to clear and re-enter.
        if pre.required_params:
            print(
                _dim("Current required parameters:"),
                file=out,
            )
            for p in pre.required_params:
                print(_dim(f"  {p['name']}:{p['type']}:{p['description']}"), file=out)
            print(_dim("Press Enter on the first line to keep these."), file=out)
        required_params = _ask_list_with_keep(
            "Required parameters (one per line, format: "
            "name:type:description; empty line to finish):",
            validate_fn=_check_param_triple,
            keep=pre.required_params,
            out=out,
        )

        if pre.optional_params:
            print(
                _dim("Current optional parameters:"),
                file=out,
            )
            for p in pre.optional_params:
                print(_dim(f"  {p['name']}:{p['type']}:{p['description']}"), file=out)
            print(_dim("Press Enter on the first line to keep these."), file=out)
        optional_params = _ask_list_with_keep(
            "Optional parameters (one per line, format: "
            "name:type:description; empty line to skip):",
            validate_fn=_check_param_triple,
            keep=pre.optional_params,
            out=out,
        )

        namespace = _ask(
            "Namespace (your organization or project, e.g., acme-quality):",
            default=pre.namespace or "",
            out=out,
        ) or None

        substitutes_for = _ask(
            "Substitutes for (existing method name; empty to skip):",
            default=pre.substitutes_for or "",
            out=out,
        ) or None

        substitution_conditions: Optional[str] = None
        if substitutes_for:
            substitution_conditions = _ask(
                "Conditions for substitution "
                "(e.g., \"when ruleset is JSON Schema\"):",
                default=pre.substitution_conditions or "",
                out=out,
            ) or None

    except KeyboardInterrupt:
        print(file=out)
        print(_dim("Cancelled."), file=out)
        return None

    return _Draft(
        name=name,
        intent=intent,
        actor=actor,
        outcome=outcome,
        capability=capability,
        confidence_guidance=confidence,
        impact_tier=impact_tier,
        is_idempotent=is_idempotent,
        required_params=required_params,
        optional_params=optional_params,
        namespace=namespace,
        substitutes_for=substitutes_for,
        substitution_conditions=substitution_conditions,
    )


def _ask_list_with_keep(
    prompt: str,
    *,
    validate_fn: Callable[[str], _FieldOutcome],
    keep: List[Dict[str, Any]],
    out: Any,
) -> List[Dict[str, Any]]:
    """Variant of _ask_list that returns ``keep`` when the first input
    is empty AND ``keep`` is non-empty; otherwise behaves like
    _ask_list. Used by edit-mode walkthroughs."""
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
        outcome = validate_fn(text)
        if not outcome.ok:
            if outcome.message:
                print(_red(f"  {_bad()} {outcome.message}"), file=out)
            for s in outcome.suggestions:
                print(_dim(f"    {s}"), file=out)
            print(file=out)
            continue
        name, type_, description = (p.strip() for p in text.split(":", 2))
        items.append({
            "name": name, "type": type_, "description": description,
        })


# ---------------------------------------------------------------------------
# Preview rendering.
# ---------------------------------------------------------------------------


def render_preview(spec: AMGMethodSpec, *, out: Any = sys.stdout) -> None:
    bar = _bar() * 60
    print(file=out)
    print(_bold(f"{_bar()*3} Proposed Method {bar[16:]}"), file=out)
    sb = spec.semantic
    print(f"Name:        {spec.name}", file=out)
    if sb:
        print(f"Intent:      {sb.intent}", file=out)
        print(f"Actor:       {sb.actor}", file=out)
        print(f"Outcome:     {sb.outcome}", file=out)
        if sb.capability:
            print(f"Capability:  {sb.capability}", file=out)
        impact_line = ""
        if sb.impact_tier:
            impact_line = sb.impact_tier
            if sb.confidence_guidance is not None:
                impact_line += f"  (confidence floor {sb.confidence_guidance})"
        if impact_line:
            print(f"Impact:      {impact_line}", file=out)
        idemp = "yes" if sb.is_idempotent else "no"
        print(f"Idempotent:  {idemp}", file=out)
    print("Parameters:", file=out)
    if spec.required_params:
        print("  Required:", file=out)
        for p in spec.required_params:
            print(
                f"    {p.name}:{p.type} {_bar()} {p.description}",
                file=out,
            )
    else:
        print("  Required: (none)", file=out)
    if spec.optional_params:
        print("  Optional:", file=out)
        for p in spec.optional_params:
            print(
                f"    {p.name}:{p.type} {_bar()} {p.description}",
                file=out,
            )
    else:
        print("  Optional: (none)", file=out)
    if spec.substitutes_for:
        for s in spec.substitutes_for:
            cond = f" ({s.conditions})" if s.conditions else ""
            print(f"Substitutes: {s.target_method}{cond}", file=out)
    if spec.namespace:
        print(f"Namespace:   {spec.namespace}", file=out)
    print(f"Source:      {spec.source}", file=out)
    print(_bold(bar), file=out)
    # Surface composer warnings so the user can react before submission.
    warnings = spec.__dict__.get("_composer_warnings", [])
    if warnings:
        print(file=out)
        print(_yellow("Warnings:"), file=out)
        for w in warnings:
            print(_yellow(f"  - {w}"), file=out)


# ---------------------------------------------------------------------------
# Submission + response rendering.
# ---------------------------------------------------------------------------


def _submit(
    uri: str,
    args,
    body: Dict[str, Any],
    *,
    method: str = "PROPOSE",
    synthesis_id: Optional[str] = None,
) -> FetchResult:
    return core_client.invoke_method(
        uri,
        method,
        body=body,
        registry_url=args.registry,
        insecure=args.insecure,
        insecure_skip_verify=args.insecure_skip_verify,
        synthesis_id=synthesis_id,
        verbose=args.verbose,
    )


def _render_propose_response(
    result: FetchResult,
    uri: str,
    *,
    spec: Optional[AMGMethodSpec] = None,
    out: Any = sys.stdout,
) -> int:
    """
    Render a PROPOSE response. Returns an exit code.

    For 461, when ``spec`` is provided, prompts for counter-proposal
    acceptance and re-invokes against the suggested method name with
    the original body.
    """
    if not result.ok:
        print(_red(f"{_bad()} {result.error}"), file=out)
        return 1
    payload = result.parsed if isinstance(result.parsed, dict) else {}
    code = result.status_code

    if code == 200:
        synth = payload.get("synthesis") or {}
        print(_green(f"{_ok()} Server accepted. Synthesis instantiated."), file=out)
        print(file=out)
        print(f"Synthesis ID:    {synth.get('synthesis_id', '(unknown)')}", file=out)
        target = synth.get("target_method")
        mapping = synth.get("parameter_mapping") or {}
        if target:
            print(f"Underlying method: {target}", file=out)
            # mapping is {proposal_param: target_param}: each line
            # reads "ANALYZE -> target_param mapped from 'proposal_param'".
            arrow = f"{_bar()}>" if not _supports_unicode() else "→"
            for proposal_param, target_param in mapping.items():
                print(
                    f"  {target} {arrow} {target_param} mapped from "
                    f"{proposal_param!r}",
                    file=out,
                )
        if synth.get("description"):
            print(f"Description:     {synth['description']}", file=out)
        print("Expires:         end of session", file=out)
        print(file=out)
        if spec is not None:
            print("Invoke this synthesis with:", file=out)
            print(
                f"  agtp {uri} {spec.name} -d '{{...}}' "
                f"--synthesis-id {synth.get('synthesis_id', '<id>')}",
                file=out,
            )
        return 0

    if code == 460:
        err = payload.get("error", {}) or {}
        print(_red(f"{_bad()} Server refused negotiation."), file=out)
        print(file=out)
        print(f"Reason: {err.get('reason', '(unknown)')}", file=out)
        if err.get("explanation"):
            print(f"Detail: {err['explanation']}", file=out)
        print(file=out)
        print(_dim(
            "You might try a different server, or use --negotiate to attempt "
            "alternative proposals automatically."
        ), file=out)
        return 1

    if code == 461:
        counter = payload.get("counter_proposal") or {}
        suggested = counter.get("name", "(unknown)")
        print(_yellow(f"{_redo()} Server proposed an alternative."), file=out)
        print(file=out)
        if spec is not None:
            print(f"Server suggests: {suggested} instead of {spec.name}", file=out)
        else:
            print(f"Server suggests: {suggested}", file=out)
        if counter.get("description"):
            print(f"Reason:          {counter['description']}", file=out)
        if spec is not None:
            _render_counter_differences(spec, counter, out=out)
        print(file=out)
        return _maybe_accept_counter(uri, suggested, spec, payload, out=out)

    # Anything else: surface the body and return non-zero.
    print(_red(f"{_bad()} {code} {result.status_text}"), file=out)
    if result.body_text:
        print(result.body_text, file=out)
    return 1


def _render_counter_differences(
    spec: AMGMethodSpec,
    counter: Dict[str, Any],
    *,
    out: Any,
) -> None:
    """
    Print a short "Differences:" section comparing the original
    proposed spec to the server's counter-proposal. Only fields the
    server populated in ``counter`` are compared — absent fields are
    left out so the section stays terse and accurate.
    """
    diffs: List[str] = []
    suggested_name = counter.get("name")
    if suggested_name and suggested_name != spec.name:
        diffs.append(f"Name: {spec.name} -> {suggested_name}")

    counter_required = counter.get("required_params")
    if counter_required is not None:
        ours = sorted(p.name for p in spec.required_params)
        theirs = sorted(
            p.get("name") for p in counter_required if isinstance(p, dict)
        )
        if ours == theirs:
            joined = ", ".join(ours) if ours else "(none)"
            diffs.append(f"Required params: identical ({joined})")
        else:
            diffs.append(
                f"Required params: {ours} -> {theirs}"
            )

    counter_idemp = counter.get("idempotent")
    if counter_idemp is not None and counter_idemp == spec.idempotent:
        diffs.append(f"Idempotent: identical ({str(spec.idempotent).lower()})")
    elif counter_idemp is not None:
        diffs.append(
            f"Idempotent: {str(spec.idempotent).lower()} -> "
            f"{str(counter_idemp).lower()}"
        )

    if diffs:
        print("Differences:", file=out)
        for d in diffs:
            print(f"  - {d}", file=out)


def _maybe_accept_counter(
    uri: str,
    suggested: str,
    spec: Optional[AMGMethodSpec],
    payload: Dict[str, Any],
    *,
    out: Any,
) -> int:
    """
    Prompt the user to accept the counter-proposal. On acceptance,
    re-PROPOSE the original spec under the suggested name (the user
    is composing a method, not invoking one — they have parameter
    *specs*, not values). The server should accept the second
    PROPOSE since it suggested the name, returning 200 with synthesis.
    """
    if spec is None or not suggested:
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

    # Re-compose the spec under the new name. Re-use every other
    # field; if the user supplied a substitution, leave it as-is.
    body = spec.to_dict()
    body["name"] = suggested
    if body.get("semantic"):
        # The semantic block doesn't carry the name, so it survives
        # untouched. No-op kept here for clarity.
        pass

    result = core_client.invoke_method(uri, "PROPOSE", body=body)
    if not result.ok:
        print(_red(f"{_bad()} Re-proposal failed: {result.error}"), file=out)
        return 1
    # Render the second response with spec=None so a counter-of-a-counter
    # is reported but not re-prompted (the user already accepted once).
    return _render_propose_response(result, uri, spec=None, out=out)


# ---------------------------------------------------------------------------
# Save flow.
# ---------------------------------------------------------------------------


def _save_draft(spec_dict: Dict[str, Any], path_str: str, *, out: Any) -> int:
    path = Path(path_str.strip()).expanduser()
    ext = path.suffix.lower()
    if ext == ".json":
        text = json.dumps(spec_dict, indent=2)
    else:
        if ext not in (".yaml", ".yml"):
            print(_yellow(
                f"Warning: extension {ext!r} is not .yaml/.yml/.json; "
                f"saving as YAML."
            ), file=out)
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print(_red(
                "PyYAML is not installed. Install with `pip install pyyaml` "
                "or save as .json instead."
            ), file=out)
            return 2
        text = yaml.safe_dump(spec_dict, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Top-level entry: run_propose.
# ---------------------------------------------------------------------------


def _confirm_or_edit_save(
    spec: AMGMethodSpec,
    draft: _Draft,
    uri: str,
    *,
    out: Any = sys.stdout,
) -> str:
    """
    Render the preview and prompt for the four-state confirmation.
    Returns one of: ``"submit"`` / ``"cancel"`` / ``"edit"`` / ``"saved"``.
    """
    while True:
        render_preview(spec, out=out)
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
            default_path = f"./{spec.name.lower()}.method.yaml"
            try:
                path_str = input(f"Save as: [{default_path}]\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                return "cancel"
            path_str = path_str or default_path
            spec_dict = spec.to_dict()
            rc = _save_draft(spec_dict, path_str, out=out)
            if rc != 0:
                continue
            print(_green(f"{_ok()} Saved."), file=out)
            print("Submit later with:", file=out)
            print(
                f"  agtp {uri} --propose --params-file {path_str}",
                file=out,
            )
            return "saved"
        # "n" or anything else: cancel.
        return "cancel"


def _interactive_compose(
    uri: str,
    args,
    *,
    out: Any = sys.stdout,
) -> Optional[AMGMethodSpec]:
    """
    Run the walkthrough -> compose -> preview -> confirm loop.

    Returns the composed spec when the user picks "submit", or None
    when they cancel / save / hit a composition error they choose
    not to fix.
    """
    draft: Optional[_Draft] = None
    while True:
        draft = _walk_compose(uri, prefill=draft, out=out)
        if draft is None:
            return None
        try:
            spec = compose_method(draft.name, **draft.to_compose_kwargs())
        except CompositionError as exc:
            print(_red(f"{_bad()} Composition refused: {exc}"), file=out)
            for s in exc.suggestions:
                print(_dim(f"  {s}"), file=out)
            try:
                ans = input(
                    "Edit the draft to fix? (Y/n):\n> "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if ans in ("n", "no"):
                return None
            continue

        action = _confirm_or_edit_save(spec, draft, uri, out=out)
        if action == "submit":
            return spec
        if action == "edit":
            continue
        # cancel, saved
        return None


def _load_proposal_body(args) -> Dict[str, Any]:
    """
    Load a non-interactive proposal body from -d or --params-file.
    Raises ValueError on parse / format problems.
    """
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
        "--propose requires either --interactive, -d <json>, "
        "or --params-file <path>"
    )


def _validate_non_interactive_body(body: Dict[str, Any]) -> AMGMethodSpec:
    """
    For non-interactive --propose, validate the body locally before
    sending. The body shape determines the path:

    * Bodies with a ``semantic`` block (or any of the AGIS top-level
      fields) are treated as full method specs and validated via
      compose_from_dict.
    * Bare runtime proposals (just ``name`` + ``parameters``) are
      validated via from_proposal -> validator. This path uppercases
      the name, so it is only chosen for explicitly minimal bodies.
    """
    from client.amg.composer import compose_from_dict

    looks_like_full_spec = (
        "semantic" in body
        or "description" in body
        or "category" in body
        or "required_params" in body
    )
    if looks_like_full_spec:
        return compose_from_dict(body)

    spec = AMGMethodSpec.from_proposal(body)
    result = validate(spec)
    if not result.valid:
        sugs = suggest_fix(result, spec.name)
        raise CompositionError(
            f"AMG validation refused {spec.name!r} at pass "
            f"'{result.error.pass_name}' [{result.error.code}]: "
            f"{result.error.message}",
            validation_result=result,
            suggestions=sugs,
        ) from None
    return spec


def run_propose(args, *, out: Any = sys.stdout) -> int:
    """
    Top-level entry for ``--propose``. Called from ``main.run`` when
    ``args.propose`` is set.
    """
    uri = args.uri

    if args.interactive:
        spec = _interactive_compose(uri, args, out=out)
        if spec is None:
            return 1
        body = spec.to_dict()
    else:
        try:
            body = _load_proposal_body(args)
        except ValueError as exc:
            print(_red(f"error: {exc}"), file=sys.stderr)
            return 2
        try:
            spec = _validate_non_interactive_body(body)
        except CompositionError as exc:
            print(_red(f"{_bad()} Local validation refused the proposal."),
                  file=sys.stderr)
            print(_red(f"  {exc}"), file=sys.stderr)
            for s in exc.suggestions:
                print(_dim(f"    {s}"), file=sys.stderr)
            return 1
        # Write the validated spec back so the wire body carries the
        # full AGIS shape when the caller supplied a full method.yaml.
        if "semantic" in body or "intent" in body.get("semantic", {}):
            body = spec.to_dict()

    print(file=out)
    print(_dim("Submitting..."), file=out)
    result = _submit(uri, args, body)
    return _render_propose_response(result, uri, spec=spec, out=out)


__all__ = [
    "run_propose",
    "render_preview",
]
