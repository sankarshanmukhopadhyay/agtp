"""
agtp-migrate: convert v1 Agent Document files to v2 in place.

Usage:
  agtp-migrate path/to/lauren.agent.json
  agtp-migrate path/to/dir/                  # all *.agent.json under dir
  agtp-migrate --check path/to/lauren.agent.json   # report only

The conversion is exactly the one performed by
``agtp.identity.from_dict_v1_compat`` at load time, materialized to
disk so the source file becomes self-describing v2. A backup of the
original is written alongside as ``<name>.v1.bak`` unless ``--no-backup``
is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List

from agtp.identity import from_dict, is_v1_document


def _gather(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.glob("**/*.agent.json"))
    raise FileNotFoundError(path)


def _migrate_one(path: Path, *, write_backup: bool, dry_run: bool) -> str:
    """Migrate a single file. Returns a one-line status string."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        return f"SKIP {path}: cannot read ({exc})"

    if not isinstance(data, dict):
        return f"SKIP {path}: top-level value is not an object"

    if not is_v1_document(data):
        return f"OK   {path}: already v2"

    doc = from_dict(data)
    new_text = doc.to_json(pretty=True) + "\n"

    if dry_run:
        return f"WOULD-MIGRATE {path}: v1 -> v2"

    if write_backup:
        backup = path.with_suffix(path.suffix + ".v1.bak")
        backup.write_text(text, encoding="utf-8")

    path.write_text(new_text, encoding="utf-8")
    return f"MIGRATED {path}: v1 -> v2 (backup: {backup if write_backup else 'none'})"


def run(paths: Iterable[Path], *, write_backup: bool, dry_run: bool) -> int:
    rc = 0
    for top in paths:
        try:
            files = _gather(top)
        except FileNotFoundError:
            print(f"error: {top}: not found", file=sys.stderr)
            rc = 1
            continue
        if not files:
            print(f"note: no *.agent.json under {top}", file=sys.stderr)
            continue
        for f in files:
            line = _migrate_one(f, write_backup=write_backup, dry_run=dry_run)
            print(line)
            if line.startswith("SKIP"):
                rc = max(rc, 1)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agtp-migrate",
        description="Convert v1 Agent Document files to v2 in place.",
    )
    p.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="One or more *.agent.json files (or directories to scan).",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without modifying any files.",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing the .v1.bak alongside each migrated file.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run(
        args.paths,
        write_backup=not args.no_backup,
        dry_run=args.check,
    )


if __name__ == "__main__":
    sys.exit(main())
