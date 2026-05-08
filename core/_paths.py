"""
Path normalization for cross-platform subprocess invocation.

Git Bash on Windows returns POSIX-form paths from `pwd` (e.g.
`/x/agtp/v1`) which Python on Windows misinterprets as paths rooted at
the current drive (so `/x/...` becomes `X:\\x\\...`). This module
provides a single `normalize()` that resolves any incoming path to the
platform's native form, absolute and resolved.

Use this anywhere user-facing code accepts a path that may have been
produced by `pwd`, `cygpath`, an environment variable, or a shell
script. Internal `Path(...)` literals don't need it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Union


PathLike = Union[str, os.PathLike]


# Matches Git-Bash's MSYS-style absolute paths: a single lowercase drive
# letter under root, e.g. /c/Users/foo or /x/agtp/v1.
_MSYS_DRIVE_PATTERN = re.compile(r"^/([a-zA-Z])(/|$)(.*)$")


def _is_windows() -> bool:
    return os.name == "nt"


def _from_msys(text: str) -> str:
    """Convert /x/agtp/v1 -> X:/agtp/v1. Returns input unchanged if no match."""
    m = _MSYS_DRIVE_PATTERN.match(text)
    if not m:
        return text
    drive, _slash, rest = m.group(1), m.group(2), m.group(3)
    return f"{drive.upper()}:/{rest}"


def normalize(path: PathLike) -> Path:
    """
    Return ``path`` in the platform's native form, resolved and absolute.

    Behavior:
      * On non-Windows hosts, ``Path(path).resolve()`` is the answer.
      * On Windows, MSYS-form paths (`/x/agtp/...`) are first lifted to
        Windows form (`X:/agtp/...`); then the result is resolved.
      * Relative paths resolve against the current working directory.

    The returned :class:`Path` is suitable for handing to a subprocess,
    writing to a config file, or comparing for equality across calls.
    """
    text = os.fspath(path)
    if _is_windows():
        text = _from_msys(text)
    return Path(text).resolve()


def normalize_str(path: PathLike) -> str:
    """Convenience wrapper that returns the normalized path as a string."""
    return str(normalize(path))


__all__ = ["normalize", "normalize_str"]
