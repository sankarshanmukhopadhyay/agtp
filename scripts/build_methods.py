"""
Generate ``core/methods.json`` from the canonical curated method list.

The source of truth is the ``METHODS`` Python list (one tuple per method;
each tuple is ``(name, [categories], description)`` or for legacy
methods ``(name, [categories], description, preferred_replacement)``).
Running this script reads that list, deduplicates methods that appear
in multiple categories (their categories merge; the first
description wins), and emits the JSON document the protocol consumes
at startup.

The JSON file is checked in. Re-run this script after changes to the
canonical list:

    python scripts/build_methods.py

The path to the input list defaults to ``scripts/methods_source.py`` in
this repo; pass an alternate path as the first CLI argument when the
canonical source is hosted elsewhere (e.g., a downloaded sketch).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path(__file__).resolve().parent / "methods_source.py"
OUTPUT = REPO_ROOT / "core" / "methods.json"


# Embedded methods are a fixed set — protocol primitives that every
# AGTP server must answer to.
EMBEDDED = [
    "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
    "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
    "INSPECT",
    # Phase 8: identity lifecycle methods. The daemon owns these
    # uniformly — every AGTP server transitions agent status the same
    # way and emits the same lifecycle stream entries.
    "ACTIVATE", "DEACTIVATE", "REVOKE", "REINSTATE", "DEPRECATE",
]


# Legacy HTTP methods. Each carries a "preferred" mapping so servers
# that opt-in via ``[policies.methods]`` can redirect callers to the
# AGTP-canonical method. The source list only marks GET and DELETE
# explicitly; the other three are added here so the registry always
# reflects the full HTTP-method legacy set.
LEGACY_DEFAULTS: Dict[str, Dict[str, str]] = {
    "GET":    {"preferred": "FETCH",   "description": "Legacy retrieval verb."},
    "POST":   {"preferred": "CREATE",  "description": "Legacy creation/submission verb."},
    "PUT":    {"preferred": "REPLACE", "description": "Legacy replacement verb."},
    "DELETE": {"preferred": "REMOVE",  "description": "Legacy removal verb."},
    "PATCH":  {"preferred": "MODIFY",  "description": "Legacy modification verb."},
}


# Category metadata. Description text mirrors what callers see in
# discovery responses; ordering controls the "methods grouped by
# category" emit order in the final JSON.
CATEGORIES: "OrderedDict[str, str]" = OrderedDict([
    ("discovery",       "Find, locate, observe, or detect without retrieving content."),
    ("retrieval",       "Obtain, load, or pull content into the agent's working context."),
    ("analysis",        "Reason over, compute on, score, or judge content."),
    ("transaction",     "Commit value, exchange, or external state-changing actions."),
    ("modification",    "Change, restructure, or remove existing state."),
    ("creation",        "Bring new entities or artifacts into existence."),
    ("notification",    "Signal, communicate, or publish to recipients."),
    ("mechanics",       "Protocol primitives for agent coordination and lifecycle."),
    ("domain_spanning", "Cross-cutting lifecycle and control-plane operations."),
])

# methods_source.py uses "domain" as a shorthand for the long category
# name in CATEGORIES. Map it during parsing.
CATEGORY_ALIASES = {
    "domain": "domain_spanning",
}


def _load_source(path: Path) -> Tuple[List[Tuple], Dict[str, Dict[str, str]]]:
    """Import the methods source module and return ``(METHODS, DEPRECATED)``.

    For back-compat the loader also accepts a top-level ``VERBS`` symbol
    (pre-rename source files). New source files should expose ``METHODS``.

    ``DEPRECATED`` is optional in the source module (defaults to an
    empty dict). Each entry maps a method name to a dict of
    deprecation metadata: ``{"deprecated_in": "...", "removed_in":
    "...", "successor": "..."}``. The build script merges these
    fields into the per-method entry in the emitted JSON.
    """
    if not path.exists():
        raise SystemExit(f"method source not found: {path}")
    spec = importlib.util.spec_from_file_location("_methods_source", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import-time error
        raise SystemExit(f"could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "METHODS"):
        methods = module.METHODS
    elif hasattr(module, "VERBS"):
        methods = module.VERBS
    else:
        raise SystemExit(
            f"{path} does not define a top-level METHODS list "
            f"(or legacy VERBS list)"
        )
    deprecated = getattr(module, "DEPRECATED", {}) or {}
    if not isinstance(deprecated, dict):
        raise SystemExit(
            f"{path}: DEPRECATED must be a dict mapping method name -> "
            f"{{deprecated_in, removed_in, successor}}"
        )
    return methods, deprecated


def _normalize_categories(cats: List[str]) -> List[str]:
    """Apply ``CATEGORY_ALIASES`` and reject unknown categories."""
    out: List[str] = []
    for c in cats:
        canonical = CATEGORY_ALIASES.get(c, c)
        if canonical not in CATEGORIES:
            raise SystemExit(
                f"unknown category {c!r} (no alias). "
                f"Add it to CATEGORIES or CATEGORY_ALIASES in build_methods.py."
            )
        if canonical not in out:
            out.append(canonical)
    return out


def build(source_path: Path) -> Dict[str, Any]:
    raw, deprecated = _load_source(source_path)

    # Merge duplicates. Some methods (ENUMERATE, RECONCILE, SUBSCRIBE,
    # etc.) appear under multiple categories in the source list; their
    # categories are unioned and the first description is kept.
    methods: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    legacy: Dict[str, Dict[str, str]] = dict(LEGACY_DEFAULTS)

    for entry in raw:
        if len(entry) == 3:
            name, cats, desc = entry
            preferred = None
        elif len(entry) == 4:
            name, cats, desc, preferred = entry
        else:
            raise SystemExit(f"unexpected method entry shape: {entry!r}")

        name = name.upper()
        cats = _normalize_categories(cats)

        if preferred is not None:
            # Legacy entry. The build_methods defaults already cover
            # the five HTTP methods; if the source overrides one,
            # honor the source's preferred mapping.
            legacy[name] = {
                "preferred": preferred.upper(),
                "description": desc,
            }
            continue

        # Methods declared as legacy (GET/POST/PUT/DELETE/PATCH) are
        # excluded from the curated catalog even when the source
        # list also defines them as regular methods. The legacy
        # registration is authoritative — POST as a legacy HTTP
        # method shadows POST as "publish to a feed", and the
        # protocol layer must not have both meanings active. A
        # server that wants POST-as-publish can re-register it as
        # a custom method under its own namespace.
        if name in legacy:
            continue

        if name in methods:
            existing = methods[name]
            for c in cats:
                if c not in existing["categories"]:
                    existing["categories"].append(c)
            # Keep the first description for stability.
        else:
            methods[name] = {
                "categories": list(cats),
                "description": desc,
            }

    # Sort methods: embedded first (in EMBEDDED order), then by category
    # (in CATEGORIES order), alphabetical within each.
    by_category: Dict[str, List[str]] = {c: [] for c in CATEGORIES}
    embedded_set = set(EMBEDDED)
    embedded_block: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for name in EMBEDDED:
        if name in methods:
            embedded_block[name] = methods[name]
        else:
            # Embedded method missing from source — synthesize a minimal
            # entry so the registry stays complete.
            embedded_block[name] = {
                "categories": ["mechanics"],
                "description": f"Embedded AGTP method {name}.",
            }

    for name, data in methods.items():
        if name in embedded_set:
            continue
        # Place under its first canonical category.
        primary = data["categories"][0]
        by_category[primary].append(name)

    # Alphabetize within each category.
    for cat in by_category:
        by_category[cat].sort()

    final: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for name, data in embedded_block.items():
        final[name] = data
    for cat in CATEGORIES:
        for name in by_category[cat]:
            final[name] = methods[name]

    # Layer per-method deprecation metadata on top of the base entries.
    # A DEPRECATED entry referencing a method that doesn't exist in
    # the catalog is a build-time bug; refuse rather than silently
    # producing a methods.json with dangling deprecations.
    for method_name, dep in (deprecated or {}).items():
        upper = method_name.upper()
        if upper not in final:
            raise SystemExit(
                f"DEPRECATED references unknown method {upper!r}; "
                f"declare it in METHODS first or remove the deprecation entry."
            )
        if not isinstance(dep, dict):
            raise SystemExit(
                f"DEPRECATED[{upper!r}] must be a dict with "
                f"deprecated_in / removed_in / successor fields."
            )
        if "deprecated_in" in dep:
            final[upper]["deprecated_in"] = str(dep["deprecated_in"])
        if "removed_in" in dep:
            final[upper]["removed_in"] = str(dep["removed_in"])
        if "successor" in dep:
            final[upper]["successor"] = str(dep["successor"]).upper()

    return {
        "version": "1.0.0",
        "embedded": list(EMBEDDED),
        "legacy": legacy,
        "categories": dict(CATEGORIES),
        "methods": final,
    }


def main(argv: List[str]) -> int:
    src = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_SOURCE
    doc = build(src)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {OUTPUT}: "
        f"{len(doc['methods'])} methods, "
        f"{len(doc['embedded'])} embedded, "
        f"{len(doc['legacy'])} legacy",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
