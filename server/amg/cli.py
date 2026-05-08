"""
``agtp-amg`` — command-line driver for the AMG validator + composer.

Two subcommands are supported. The first positional argument selects:

  * ``agtp-amg validate ...``   nine-pass validator (existing surface)
  * ``agtp-amg compose  ...``   composer (new)

The first positional argument is optional. When omitted, the tool
falls through to ``validate`` so existing invocations such as
``agtp-amg path/to/method.json`` keep working.

Examples::

  agtp-amg validate path/to/method.json
  agtp-amg validate path/to/methods/                     # all *.method.json
  agtp-amg --check-substitution METHOD_NAME              # validate-mode flag

  agtp-amg compose --from path/to/evaluate.method.yaml
  agtp-amg compose --name EVALUATE \\
      --intent "Evaluates the input against a declared ruleset" \\
      --actor agent \\
      --outcome "A structured assessment is returned" \\
      --capability analysis \\
      --required-param "input:object:The data to evaluate" \\
      --required-param "ruleset:string:Identifier of the ruleset"

Exit codes:
  0  validation / composition succeeded
  1  validation / composition failed
  2  argument or I/O error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from server.amg.composer import (
    CompositionError,
    compose_from_dict,
    compose_from_json,
    compose_from_yaml,
    compose_method,
)
from server.amg.grammar import AMGMethodSpec, ParamSpec
from server.amg.reserved import EMBEDDED_METHODS
from server.amg.substitution import find_substitutes
from server.amg.validator import ValidationResult, validate


# ---------------------------------------------------------------------------
# ANSI + glyph helpers.
# ---------------------------------------------------------------------------


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _supports_unicode() -> bool:
    enc = (sys.stdout.encoding or "").lower()
    return enc in ("utf-8", "utf8", "utf-16", "utf-32") or enc.startswith("utf")


def _glyph_ok() -> str:
    return "✓" if _supports_unicode() else "OK"


def _glyph_fail() -> str:
    return "✗" if _supports_unicode() else "X "


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if _supports_color() else text


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m" if _supports_color() else text


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _supports_color() else text


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _supports_color() else text


# ===========================================================================
# Validate subcommand (existing behavior).
# ===========================================================================


def _gather_specs(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        files = sorted(path.glob("**/*.method.json"))
        if not files:
            files = sorted(path.glob("**/*.json"))
        return files
    raise FileNotFoundError(path)


def _load_spec(path: Path) -> AMGMethodSpec:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level JSON value is not an object"
        )
    return AMGMethodSpec.from_dict(data)


def _load_known_methods(path: Optional[Path]) -> Set[str]:
    if path is None:
        return set()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {str(n).upper() for n in data}
    if isinstance(data, dict):
        return {str(n).upper() for n in data.keys()}
    raise ValueError(
        f"{path}: --known-methods must be a list or object"
    )


def _print_header(spec: AMGMethodSpec) -> None:
    ns = f", namespace: {spec.namespace}" if spec.namespace else ""
    print(_bold(f"Validating {spec.name} ({spec.source}{ns})"))


def _print_result(result: ValidationResult) -> None:
    for p in result.passes:
        mark = _green(_glyph_ok()) if p.passed else _red(_glyph_fail())
        label = p.name.ljust(16)
        detail = p.detail or ""
        print(f"  {mark} Pass ({label})  {detail}")
    if result.error is not None:
        err = result.error
        print()
        print(_red(f"  Error [{err.code}]: {err.message}"))
        if err.suggestion:
            print(_dim(f"  Suggestion: {err.suggestion}"))
    print()
    if result.valid:
        print(_green(_bold("VALID")))
    else:
        print(_red(_bold("INVALID")))


def _print_substitutes(method_name: str, registry: Iterable[str]) -> None:
    name = method_name.upper()
    subs = find_substitutes(name, list(registry))
    if not subs:
        print(_dim(
            f"No substitution candidates for {name} in the supplied "
            f"registry."
        ))
        return
    print(_bold(f"Substitution candidates for {name}:"))
    for s in subs:
        print(f"  - {s}")


def _build_validate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agtp-amg validate",
        description="Validate AMG method specifications.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="One or more *.method.json files (or directories to scan).",
    )
    p.add_argument(
        "--known-methods",
        type=Path,
        metavar="PATH",
        help=(
            "JSON file (array or object) of additional method names "
            "to admit when checking substitution targets."
        ),
    )
    p.add_argument(
        "--check-substitution",
        metavar="NAME",
        help=(
            "Print the substitution candidates for NAME against the "
            "supplied --known-methods set (or just the embedded methods)."
        ),
    )
    return p


def run_validate(argv: List[str]) -> int:
    args = _build_validate_parser().parse_args(argv)

    try:
        extra = _load_known_methods(args.known_methods)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: --known-methods: {exc}", file=sys.stderr)
        return 2
    known = set(EMBEDDED_METHODS) | extra

    if args.check_substitution:
        _print_substitutes(args.check_substitution, known)
        return 0

    if not args.paths:
        print(
            "error: no spec files supplied. Pass a path or use "
            "--check-substitution.",
            file=sys.stderr,
        )
        return 2

    overall_ok = True
    for top in args.paths:
        try:
            files = _gather_specs(top)
        except FileNotFoundError:
            print(f"error: {top}: not found", file=sys.stderr)
            overall_ok = False
            continue
        if not files:
            print(f"note: no specs found under {top}", file=sys.stderr)
            continue
        for f in files:
            try:
                spec = _load_spec(f)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                print(f"error: {f}: cannot load spec: {exc}", file=sys.stderr)
                overall_ok = False
                continue
            print(_dim(f"# {f}"))
            _print_header(spec)
            result = validate(spec, known_methods=known)
            _print_result(result)
            if not result.valid:
                overall_ok = False
            print()

    return 0 if overall_ok else 1


# ===========================================================================
# Compose subcommand (new).
# ===========================================================================


def _parse_param_triple(text: str) -> ParamSpec:
    """
    Parse a colon-delimited param spec from --required-param /
    --optional-param: ``name:type:description``.
    """
    parts = text.split(":", 2)
    if len(parts) < 3:
        raise argparse.ArgumentTypeError(
            f"--required/--optional-param expects 'name:type:description', "
            f"got {text!r}"
        )
    name, type_, description = parts
    return ParamSpec(
        name=name.strip(),
        type=type_.strip(),
        description=description.strip(),
    )


def _build_compose_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agtp-amg compose",
        description="Compose well-formed AMG method specifications.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        metavar="PATH",
        help="Load fields from a *.method.yaml or *.method.json file.",
    )
    p.add_argument(
        "--output",
        choices=("json", "yaml"),
        default="json",
        help="Output format for the composed spec (default: json).",
    )

    p.add_argument("--name", help="Method name (uppercase ASCII).")
    p.add_argument("--intent", help="AGIS intent: agent-goal voice.")
    p.add_argument(
        "--actor",
        choices=("agent", "user", "system"),
        help="AGIS actor (agent | user | system).",
    )
    p.add_argument("--outcome", help="AGIS outcome: post-condition voice.")
    p.add_argument(
        "--capability",
        choices=(
            "discovery", "transaction", "modification",
            "retrieval", "analysis", "notification",
        ),
        help="AGIS capability bucket.",
    )
    p.add_argument(
        "--confidence-guidance",
        type=float,
        metavar="FLOAT",
        help="AGIS confidence_guidance (0.0-1.0).",
    )
    p.add_argument(
        "--impact-tier",
        choices=("informational", "reversible", "irreversible"),
        help="AGIS impact_tier.",
    )
    p.add_argument(
        "--idempotent",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="AGIS is_idempotent flag (use --no-idempotent for false).",
    )

    p.add_argument(
        "--description",
        help="Validator-facing description (defaults to --intent).",
    )
    p.add_argument(
        "--category",
        default="transact",
        help="Method category bucket (default: transact).",
    )
    p.add_argument(
        "--namespace",
        help="Namespace for source=amg/1.0 methods.",
    )
    p.add_argument(
        "--source",
        default="amg/1.0",
        help="Method source (default: amg/1.0).",
    )

    p.add_argument(
        "--required-param",
        action="append",
        type=_parse_param_triple,
        metavar="name:type:description",
        default=[],
        help="Add a required parameter (repeatable).",
    )
    p.add_argument(
        "--optional-param",
        action="append",
        type=_parse_param_triple,
        metavar="name:type:description",
        default=[],
        help="Add an optional parameter (repeatable).",
    )
    p.add_argument(
        "--error-code",
        action="append",
        type=int,
        metavar="N",
        default=[],
        help="Add an error code (repeatable).",
    )
    p.add_argument(
        "--substitutes-for",
        action="append",
        metavar="NAME",
        default=[],
        help="Declare a substitution target (repeatable).",
    )
    p.add_argument(
        "--known-methods",
        type=Path,
        metavar="PATH",
        help="JSON file of additional known method names for substitution checks.",
    )
    return p


def _emit_spec(spec: AMGMethodSpec, fmt: str) -> None:
    if fmt == "yaml":
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print(
                "warning: PyYAML not installed; falling back to JSON output",
                file=sys.stderr,
            )
            fmt = "json"
    if fmt == "yaml":
        import yaml  # type: ignore[import-not-found]
        print(yaml.safe_dump(spec.to_dict(), sort_keys=False))
    else:
        print(json.dumps(spec.to_dict(), indent=2))


def _print_composition_error(exc: CompositionError) -> None:
    print(_red(_bold("COMPOSITION FAILED")), file=sys.stderr)
    print(_red(f"  {exc}"), file=sys.stderr)
    if exc.validation_result is not None:
        result = exc.validation_result
        for p in result.passes:
            mark = _green(_glyph_ok()) if p.passed else _red(_glyph_fail())
            label = p.name.ljust(16)
            detail = p.detail or ""
            print(f"  {mark} Pass ({label})  {detail}", file=sys.stderr)
    if exc.suggestions:
        print(file=sys.stderr)
        print(_bold("Suggestions:"), file=sys.stderr)
        for s in exc.suggestions:
            print(_dim(f"  - {s}"), file=sys.stderr)


def run_compose(argv: List[str]) -> int:
    args = _build_compose_parser().parse_args(argv)

    try:
        known = (
            _load_known_methods(args.known_methods)
            if args.known_methods else set()
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: --known-methods: {exc}", file=sys.stderr)
        return 2
    full_known = set(EMBEDDED_METHODS) | known

    if args.from_path:
        try:
            ext = args.from_path.suffix.lower()
            if ext in (".yaml", ".yml"):
                spec = compose_from_yaml(args.from_path, known_methods=full_known)
            else:
                spec = compose_from_json(args.from_path, known_methods=full_known)
        except CompositionError as exc:
            _print_composition_error(exc)
            return 1
        except ImportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _emit_spec(spec, args.output)
        return 0

    # Inline-arg composition.
    if not args.name or not args.intent or not args.actor or not args.outcome:
        print(
            "error: composition requires either --from PATH or "
            "(--name + --intent + --actor + --outcome).",
            file=sys.stderr,
        )
        return 2

    try:
        spec = compose_method(
            args.name,
            intent=args.intent,
            actor=args.actor,
            outcome=args.outcome,
            capability=args.capability,
            confidence_guidance=args.confidence_guidance,
            impact_tier=args.impact_tier,
            is_idempotent=args.idempotent,
            description=args.description,
            category=args.category,
            namespace=args.namespace,
            source=args.source,
            required_params=list(args.required_param),
            optional_params=list(args.optional_param),
            error_codes=(list(args.error_code) or None),
            substitutes_for=[
                {"target_method": s} for s in (args.substitutes_for or [])
            ] or None,
            known_methods=full_known,
        )
    except CompositionError as exc:
        _print_composition_error(exc)
        return 1

    _emit_spec(spec, args.output)
    return 0


# ===========================================================================
# Top-level dispatch.
# ===========================================================================


_KNOWN_SUBCOMMANDS = ("validate", "compose")


def main() -> int:
    argv = sys.argv[1:]
    # Backward-compat: if the first arg is not one of our subcommands,
    # fall through to validate. This keeps ``agtp-amg path/to/file.json``
    # working unchanged.
    if argv and argv[0] in _KNOWN_SUBCOMMANDS:
        cmd = argv[0]
        sub_argv = argv[1:]
    else:
        cmd = "validate"
        sub_argv = argv

    if cmd == "compose":
        return run_compose(sub_argv)
    return run_validate(sub_argv)


if __name__ == "__main__":
    sys.exit(main())
