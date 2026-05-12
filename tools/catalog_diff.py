"""
``agtp-catalog-diff`` — compare two AGTP verb catalogs and report
breakage against a deployment.

The tool answers two questions:

  1. **What changed between the two catalogs?** Added verbs,
     removed verbs, newly deprecated verbs.
  2. **Does the proposed catalog break anything in this deployment?**
     Endpoint TOMLs whose ``method = "..."`` references a removed
     verb, paths whose segments now collide with newly-added verbs
     (path-grammar refusals at registration), recipe steps that
     name removed verbs, and ``[policies.methods]`` directives in
     ``agtp-server.toml`` that do the same.

Usage::

    agtp-catalog-diff old.json new.json
    agtp-catalog-diff old.json new.json --against-deployment ./agtp-server/
    agtp-catalog-diff old.json new.json --json

Exit codes:

  * **0** — no breaking changes detected. (Pure catalog diff: always 0
    unless parse errors occur. Deployment-aware diff: 0 only when
    nothing in the deployment references removed verbs and no path
    collides with a newly-added verb.)
  * **1** — breaking changes detected in deployment context.
  * **2** — parse errors in either catalog file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Catalog parsing.
# ---------------------------------------------------------------------------


class CatalogDiffError(Exception):
    """Raised on malformed catalog input."""


def load_catalog(path: Any) -> Dict[str, Any]:
    """Load and minimally validate a methods.json document.

    Pre-rename catalogs put the per-method dict under a top-level
    ``"verbs"`` key. The loader accepts either ``"methods"`` (current)
    or ``"verbs"`` (legacy) and normalizes the document so the rest
    of the tool sees a single ``"methods"`` key.
    """
    p = Path(path)
    if not p.exists():
        raise CatalogDiffError(f"catalog not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogDiffError(f"{p}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogDiffError(f"{p}: top-level document must be a JSON object")
    if "methods" not in data and "verbs" in data:
        data["methods"] = data["verbs"]
    if "methods" not in data or not isinstance(data["methods"], dict):
        raise CatalogDiffError(
            f"{p}: missing or invalid 'methods' object "
            f"(also accepts legacy 'verbs')"
        )
    return data


# ---------------------------------------------------------------------------
# Pure catalog diff.
# ---------------------------------------------------------------------------


@dataclass
class CatalogDiff:
    """The result of comparing two catalogs."""

    old_version: str
    new_version: str
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    newly_deprecated: List[Dict[str, Any]] = field(default_factory=list)

    # Deployment scan results (filled by ``scan_deployment`` when a
    # deployment directory is supplied).
    path_grammar_conflicts: List[Dict[str, str]] = field(default_factory=list)
    endpoint_conflicts: List[Dict[str, str]] = field(default_factory=list)
    recipe_conflicts: List[Dict[str, str]] = field(default_factory=list)
    method_policy_conflicts: List[Dict[str, str]] = field(default_factory=list)

    @property
    def has_deployment_breakage(self) -> bool:
        return bool(
            self.path_grammar_conflicts
            or self.endpoint_conflicts
            or self.recipe_conflicts
            or self.method_policy_conflicts
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "added": list(self.added),
            "removed": list(self.removed),
            "newly_deprecated": list(self.newly_deprecated),
            "path_grammar_conflicts": list(self.path_grammar_conflicts),
            "endpoint_conflicts": list(self.endpoint_conflicts),
            "recipe_conflicts": list(self.recipe_conflicts),
            "method_policy_conflicts": list(self.method_policy_conflicts),
        }


def diff_catalogs(
    old: Dict[str, Any],
    new: Dict[str, Any],
) -> CatalogDiff:
    """Pure catalog diff — added / removed / newly-deprecated. No
    deployment scan; pass the result to :func:`scan_deployment` for
    the breakage analysis."""
    old_verbs = set((old.get("methods") or {}).keys())
    new_verbs = set((new.get("methods") or {}).keys())

    added = sorted(new_verbs - old_verbs)
    removed = sorted(old_verbs - new_verbs)

    # Newly-deprecated: verbs in both old and new where new has a
    # ``deprecated_in`` and old didn't (or had a different one).
    old_dep = {
        name: bool("deprecated_in" in (entry or {}))
        for name, entry in (old.get("methods") or {}).items()
    }
    newly_deprecated: List[Dict[str, Any]] = []
    for name in sorted(new_verbs & old_verbs):
        entry = new["methods"].get(name) or {}
        if "deprecated_in" not in entry:
            continue
        if not old_dep.get(name):
            newly_deprecated.append({
                "name": name,
                "deprecated_in": str(entry.get("deprecated_in")),
                "removed_in": (
                    str(entry["removed_in"])
                    if entry.get("removed_in") else None
                ),
                "successor": (
                    str(entry["successor"]).upper()
                    if entry.get("successor") else None
                ),
            })

    return CatalogDiff(
        old_version=str(old.get("version") or "?"),
        new_version=str(new.get("version") or "?"),
        added=added,
        removed=removed,
        newly_deprecated=newly_deprecated,
    )


# ---------------------------------------------------------------------------
# Deployment scan.
# ---------------------------------------------------------------------------


def _normalize_path_segment(segment: str) -> str:
    """Mirror ``core.path_grammar`` segment normalization: uppercase,
    strip ``-`` / ``_``."""
    return segment.upper().replace("-", "").replace("_", "")


def _legacy_verbs_from_catalog(catalog: Dict[str, Any]) -> Set[str]:
    return set((catalog.get("legacy") or {}).keys())


def scan_deployment(
    diff: CatalogDiff,
    deployment_dir: Path,
    *,
    new_catalog: Dict[str, Any],
) -> CatalogDiff:
    """
    Walk a deployment directory and stamp the diff with the
    breakage findings. Returns the same ``diff`` object for
    convenience.

    Looks at three locations under ``deployment_dir``:

      * ``endpoints/*.toml`` — endpoint declarations.
      * ``agtp-recipes.toml`` — synthesis recipes.
      * ``agtp-server.toml`` — server config, specifically the
        ``[policies.methods]`` block (per ``agtp-api §8``).

    Missing files are skipped silently — a deployment may not have
    every layer.
    """
    base = Path(deployment_dir)

    added_set = set(diff.added)
    removed_set = set(diff.removed)

    # --- endpoint TOMLs ---
    endpoints_dir = base / "endpoints"
    if endpoints_dir.is_dir():
        for fp in sorted(endpoints_dir.glob("*.toml")):
            _scan_endpoint_toml(
                fp, diff,
                added=added_set,
                removed=removed_set,
                legacy=_legacy_verbs_from_catalog(new_catalog),
            )

    # --- recipes ---
    recipes_path = base / "agtp-recipes.toml"
    if recipes_path.is_file():
        _scan_recipes_toml(
            recipes_path, diff, removed=removed_set,
        )

    # --- agtp-server.toml [policies.methods] ---
    server_toml = base / "agtp-server.toml"
    if server_toml.is_file():
        _scan_method_policy(
            server_toml, diff, removed=removed_set,
        )

    return diff


def _read_toml(fp: Path) -> Optional[Dict[str, Any]]:
    """Minimal TOML loader; returns None on parse errors so the
    diff doesn't crash on a malformed file."""
    try:
        import tomllib as _toml
    except ImportError:  # pragma: no cover
        import tomli as _toml  # type: ignore[no-redef]
    try:
        return _toml.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _scan_endpoint_toml(
    fp: Path,
    diff: CatalogDiff,
    *,
    added: Set[str],
    removed: Set[str],
    legacy: Set[str],
) -> None:
    doc = _read_toml(fp)
    if doc is None:
        return
    endpoint = doc.get("endpoint") or {}
    if not isinstance(endpoint, dict):
        return
    method = str(endpoint.get("method") or "").upper()
    path = str(endpoint.get("path") or "")

    # Endpoint declares a removed verb → endpoint conflict.
    if method and method in removed:
        diff.endpoint_conflicts.append({
            "file": str(fp),
            "method": method,
        })

    # Path contains a segment that becomes a verb under the new
    # catalog → path-grammar conflict.
    for segment in [s for s in path.split("/") if s]:
        if segment.startswith("{") and segment.endswith("}"):
            continue
        norm = _normalize_path_segment(segment)
        if norm in added:
            diff.path_grammar_conflicts.append({
                "file": str(fp),
                "path": path,
                "segment": segment,
                "verb": norm,
            })


def _scan_recipes_toml(
    fp: Path,
    diff: CatalogDiff,
    *,
    removed: Set[str],
) -> None:
    doc = _read_toml(fp)
    if doc is None:
        return
    recipes = doc.get("recipe") or []
    if not isinstance(recipes, list):
        return
    for recipe in recipes:
        if not isinstance(recipe, dict):
            continue
        rname = str(recipe.get("name") or "?")
        steps = recipe.get("steps") or []
        if not isinstance(steps, list):
            continue
        for i, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            method = str(step.get("method") or "").upper()
            if method and method in removed:
                diff.recipe_conflicts.append({
                    "file": str(fp),
                    "recipe": rname,
                    "step": i,
                    "method": method,
                })


def _scan_method_policy(
    fp: Path,
    diff: CatalogDiff,
    *,
    removed: Set[str],
) -> None:
    """Scan ``[policies.methods]`` in agtp-server.toml for verbs the
    new catalog has removed. Conflicts are reported with the source
    file path, the directive (``allow`` / ``disallow`` / ``legacy``
    / ``redirect``), and the offending verb so the operator can
    locate and update the entry.
    """
    doc = _read_toml(fp)
    if doc is None:
        return
    policies = doc.get("policies") or doc.get("policy") or {}
    methods = policies.get("methods") or {}
    if not isinstance(methods, dict):
        return

    def _record(directive: str, verb: str) -> None:
        if verb and verb.upper() in removed:
            diff.method_policy_conflicts.append({
                "file": str(fp),
                "directive": directive,
                "verb": verb.upper(),
            })

    # allow: string ``"*"`` or list of names.
    allow = methods.get("allow")
    if isinstance(allow, list):
        for v in allow:
            _record("allow", str(v))

    # disallow: list of names.
    for v in (methods.get("disallow") or []):
        _record("disallow", str(v))

    # legacy: string ``"*"`` / ``"NONE"`` or list of names. The five
    # legacy HTTP verbs are not in the AGTP method catalog, so a
    # name appearing here can't be in ``removed`` — but iterate
    # defensively anyway.
    legacy = methods.get("legacy")
    if isinstance(legacy, list):
        for v in legacy:
            _record("legacy", str(v))

    # redirects: array of {from_method, [from_path], to_method,
    # [to_path]}. Each redirect carries two verbs (source +
    # destination); flag either if removed.
    for entry in (methods.get("redirects") or []):
        if not isinstance(entry, dict):
            continue
        _record("redirect (from)", str(entry.get("from_method") or ""))
        _record("redirect (to)", str(entry.get("to_method") or ""))


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def render_text(diff: CatalogDiff) -> str:
    lines: List[str] = []
    lines.append(
        f"Catalog diff: {diff.old_version} -> {diff.new_version}"
    )
    lines.append("")

    if diff.added:
        lines.append(f"Added ({len(diff.added)} verb"
                     f"{'s' if len(diff.added) != 1 else ''}):")
        for v in diff.added:
            lines.append(f"  {v}")
        lines.append("")

    if diff.removed:
        lines.append(f"Removed ({len(diff.removed)} verb"
                     f"{'s' if len(diff.removed) != 1 else ''}):")
        for v in diff.removed:
            lines.append(f"  {v}")
        lines.append("")

    if diff.newly_deprecated:
        lines.append(
            f"Newly deprecated ({len(diff.newly_deprecated)} verb"
            f"{'s' if len(diff.newly_deprecated) != 1 else ''}):"
        )
        for entry in diff.newly_deprecated:
            bits = []
            if entry.get("successor"):
                bits.append(f"successor: {entry['successor']}")
            if entry.get("removed_in"):
                bits.append(f"removed_in: {entry['removed_in']}")
            tail = (" (" + ", ".join(bits) + ")") if bits else ""
            lines.append(f"  {entry['name']}{tail}")
        lines.append("")

    if diff.path_grammar_conflicts:
        lines.append(
            f"Path-grammar conflicts ({len(diff.path_grammar_conflicts)} "
            f"endpoint TOML"
            f"{'s' if len(diff.path_grammar_conflicts) != 1 else ''} "
            f"reference paths that contain newly-added verbs):"
        )
        for c in diff.path_grammar_conflicts:
            lines.append(
                f"  {c['file']}  (path {c['path']} contains {c['verb']})"
            )
        lines.append("")

    if diff.endpoint_conflicts:
        lines.append(
            f"Endpoint conflicts ({len(diff.endpoint_conflicts)} "
            f"endpoint TOML"
            f"{'s' if len(diff.endpoint_conflicts) != 1 else ''} "
            f"declare removed verbs):"
        )
        for c in diff.endpoint_conflicts:
            lines.append(f"  {c['file']}: method = {c['method']}")
        lines.append("")

    if diff.recipe_conflicts:
        lines.append(
            f"Recipe conflicts ({len(diff.recipe_conflicts)} "
            f"recipe step"
            f"{'s' if len(diff.recipe_conflicts) != 1 else ''} "
            f"reference removed verbs):"
        )
        for c in diff.recipe_conflicts:
            lines.append(
                f"  {c['file']}: recipe {c['recipe']!r} "
                f"step {c['step']}: {c['method']}"
            )
        lines.append("")

    if diff.method_policy_conflicts:
        lines.append(
            f"Method-policy conflicts ({len(diff.method_policy_conflicts)} "
            f"entr"
            f"{'ies' if len(diff.method_policy_conflicts) != 1 else 'y'} "
            f"in [policies.methods] reference removed verbs):"
        )
        for c in diff.method_policy_conflicts:
            lines.append(
                f"  {c['file']}: {c['directive']}: {c['verb']}"
            )
        lines.append("")

    if diff.has_deployment_breakage:
        n = (
            len(diff.path_grammar_conflicts)
            + len(diff.endpoint_conflicts)
            + len(diff.recipe_conflicts)
            + len(diff.method_policy_conflicts)
        )
        lines.append(
            f"Summary: {n} breaking change"
            f"{'s' if n != 1 else ''} in deployment context."
        )
    else:
        lines.append("Summary: no breaking changes detected.")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agtp-catalog-diff",
        description=(
            "Compare two AGTP method catalogs (methods.json files) and "
            "optionally scan a deployment directory for breakage. "
            "Catches removed-verb references in endpoints / recipes "
            "/ [policies.methods] before they fail at boot, and "
            "surfaces newly-added verbs that would clash with "
            "existing path segments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  agtp-catalog-diff old.json new.json\n"
            "  agtp-catalog-diff old.json new.json --against-deployment ./server/\n"
            "  agtp-catalog-diff old.json new.json --json\n"
        ),
    )
    parser.add_argument("old", help="The current catalog (methods.json).")
    parser.add_argument("new", help="The proposed catalog (methods.json).")
    parser.add_argument(
        "--against-deployment",
        metavar="DIR",
        help="Scan a deployment directory (with endpoints/, "
             "agtp-recipes.toml, agtp-server.toml [policies.methods]) "
             "for breakage against "
             "the proposed catalog.",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit a structured JSON document instead of human "
             "readable text.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        old = load_catalog(args.old)
        new = load_catalog(args.new)
    except CatalogDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diff = diff_catalogs(old, new)
    if args.against_deployment:
        scan_deployment(diff, Path(args.against_deployment), new_catalog=new)

    if args.json_output:
        print(json.dumps(diff.to_dict(), indent=2))
    else:
        print(render_text(diff), end="")

    if args.against_deployment and diff.has_deployment_breakage:
        return 1
    return 0


__all__ = [
    "CatalogDiff",
    "CatalogDiffError",
    "build_parser",
    "diff_catalogs",
    "load_catalog",
    "main",
    "render_text",
    "scan_deployment",
]


if __name__ == "__main__":
    sys.exit(main())
