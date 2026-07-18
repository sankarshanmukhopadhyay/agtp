#!/usr/bin/env python3
"""Fail on unresolved local Markdown links in publication sources."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SKIP_SCHEMES = {"http", "https", "mailto", "tel", "data"}
EXCLUDED = {".git", ".venv", "venv", "site", ".pages-src", "__pycache__"}


def markdown_files() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md")
        if not any(part in EXCLUDED for part in p.relative_to(ROOT).parts)
    )


def main() -> int:
    failures: list[str] = []
    for source in markdown_files():
        text = source.read_text(encoding="utf-8")
        for raw in LINK.findall(text):
            value = raw.strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(value)
            if parsed.scheme in SKIP_SCHEMES or value.startswith("#"):
                continue
            path_text = unquote(parsed.path)
            if not path_text:
                continue
            target = (ROOT / path_text.lstrip("/")) if path_text.startswith("/") else (source.parent / path_text)
            if not target.resolve().exists():
                failures.append(f"{source.relative_to(ROOT)} -> {value}")
    if failures:
        print("Broken local Markdown links:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in failures), file=sys.stderr)
        return 1
    print(f"Validated local links across {len(markdown_files())} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
