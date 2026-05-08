"""
agtp-validate: command-line driver for the AMG validator.

Usage:

  agtp-validate path/to/method.json
  agtp-validate path/to/methods/                       # all *.method.json
  agtp-validate --check-substitution METHOD_NAME
  agtp-validate --known-methods extra-methods.json     # extend universe

Exit codes:
  0  every spec validated
  1  at least one spec failed validation
  2  argument or I/O error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set

from client.amg.grammar import AMGMethodSpec
from client.amg.reserved import EMBEDDED_METHODS
from client.amg.substitution import find_substitutes
from client.amg.validator import ValidationResult, validate


# ---------------------------------------------------------------------------
# ANSI + glyph helpers.
#
# Colors fall back to plain text when stdout isn't a tty. Pass-mark
# glyphs fall back to ASCII when stdout's encoding can't represent
# Unicode (notably Windows cp1252 consoles).
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


# ---------------------------------------------------------------------------
# Spec loading.
# ---------------------------------------------------------------------------


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
    """
    Read additional method names from a JSON file. The file may be
    either an array of names or an object whose top-level keys are
    method names.
    """
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


# ---------------------------------------------------------------------------
# Output formatting.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agtp-validate",
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


def main() -> int:
    args = build_parser().parse_args()

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


if __name__ == "__main__":
    sys.exit(main())
