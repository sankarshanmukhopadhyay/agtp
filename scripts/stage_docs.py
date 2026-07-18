#!/usr/bin/env python3
"""Stage all repository Markdown into a deterministic MkDocs source tree."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / ".pages-src"
EXCLUDED_PARTS = {".git", ".venv", "venv", "site", ".pages-src", "__pycache__"}


def included(path: Path) -> bool:
    return path.suffix.lower() == ".md" and not any(part in EXCLUDED_PARTS for part in path.parts)


def main() -> int:
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    source_files = sorted(
        p for p in ROOT.rglob("*")
        if p.is_file() and not any(part in EXCLUDED_PARTS for part in p.relative_to(ROOT).parts)
    )
    markdown_count = 0
    for source in source_files:
        relative = source.relative_to(ROOT)
        if relative == Path("README.md"):
            markdown_count += 1
            continue
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if source.suffix.lower() == ".md":
            markdown_count += 1

    # Keep the repository README addressable at README.md while also
    # publishing it as the site home page.
    shutil.copy2(ROOT / "README.md", DEST / "index.md")

    # MkDocs publishes README.md as index.md. Rewrite repository-relative
    # README links only in the staged copies, preserving GitHub-native links
    # in source documents.
    for staged_markdown in DEST.rglob("*.md"):
        text = staged_markdown.read_text(encoding="utf-8")
        text = text.replace("../README.md", "../index.md")
        text = text.replace("../../README.md", "../../index.md")
        staged_markdown.write_text(text, encoding="utf-8")

    print(f"Staged {markdown_count} Markdown files and their repository-local assets in {DEST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
