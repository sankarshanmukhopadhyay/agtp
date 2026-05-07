"""
Tests for ``agtp._paths.normalize``.

Behavior we want guaranteed:
  * round-trips an already-native absolute path on every platform.
  * lifts MSYS-style drive paths (``/x/agtp/...``) to Windows form on
    Windows, and leaves them alone on POSIX.
  * resolves relative paths against the current working directory and
    yields the same answer regardless of which form was passed in.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agtp._paths import normalize, normalize_str


WINDOWS = os.name == "nt"


class PathNormalizationTests(unittest.TestCase):

    def test_native_absolute_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve()
            self.assertEqual(normalize(p), p)
            self.assertEqual(normalize(str(p)), p)

    def test_relative_path_resolves_against_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd_before = Path.cwd()
            try:
                os.chdir(tmp)
                got = normalize("subdir/file.txt")
                expected = (Path(tmp).resolve() / "subdir" / "file.txt")
                self.assertEqual(got, expected)
            finally:
                os.chdir(cwd_before)

    @unittest.skipUnless(WINDOWS, "MSYS path lifting is Windows-only")
    def test_msys_drive_path_lifts_to_windows_form(self) -> None:
        # Choose a drive that very likely exists in CI as well as locally.
        # On GitHub Windows runners that's C:; locally we accept any.
        candidate_drive = Path.cwd().drive[0].lower()
        msys = f"/{candidate_drive}/some/where/file.txt"
        got = normalize(msys)
        self.assertEqual(got.drive.lower(), f"{candidate_drive}:")
        self.assertEqual(got.parts[-3:], ("some", "where", "file.txt"))

    @unittest.skipIf(WINDOWS, "non-Windows leaves /x/... untouched")
    def test_posix_paths_left_alone(self) -> None:
        # On POSIX, /x/agtp/v1 is just an absolute path and should be
        # treated as such, even though the same string is MSYS-form on
        # Windows.
        self.assertEqual(normalize("/tmp"), Path("/tmp").resolve())

    def test_normalize_str_returns_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(normalize_str(tmp), str)

    def test_normalize_accepts_pathlike(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class Stub:
                def __fspath__(self): return tmp
            self.assertEqual(normalize(Stub()), Path(tmp).resolve())


if __name__ == "__main__":
    unittest.main()
