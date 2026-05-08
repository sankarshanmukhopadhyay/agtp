"""
Tests for the v2 Agent Document schema and the v1->v2 compat path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.identity import (
    AgentDocument,
    DOCUMENT_VERSION_V1_MIGRATED,
    DOCUMENT_VERSION_V2,
    RequiresDeclaration,
    from_dict,
    from_dict_v1_compat,
    is_v1_document,
)
from core.render import render_html


REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_DIR = REPO_ROOT / "server" / "agents" / "legacy"
LIVE_DIR = REPO_ROOT / "server" / "agents"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema detection + load.
# ---------------------------------------------------------------------------


class V2LoadTests(unittest.TestCase):

    def test_v2_lauren_loads_cleanly(self):
        data = _read_json(LIVE_DIR / "lauren.agent.json")
        self.assertFalse(is_v1_document(data))
        doc = from_dict(data)
        self.assertEqual(doc.document_version, DOCUMENT_VERSION_V2)
        self.assertFalse(doc.is_migrated)
        self.assertGreater(len(doc.skills), 0)
        self.assertIn("DESCRIBE", doc.requires.methods)
        self.assertFalse(doc.requires.wildcards)

    def test_v2_orchestrator_loads_with_wildcards(self):
        data = _read_json(LIVE_DIR / "orchestrator.agent.json")
        doc = from_dict(data)
        self.assertTrue(doc.requires.wildcards)
        self.assertEqual(len(doc.requires.methods), 12)
        self.assertTrue(doc.accepts_method("ANYTHING"))

    def test_v2_missing_field_raises(self):
        data = _read_json(LIVE_DIR / "lauren.agent.json")
        del data["skills"]
        with self.assertRaises(ValueError):
            from_dict(data)


class V1CompatTests(unittest.TestCase):

    def setUp(self):
        self.legacy_lauren = _read_json(LEGACY_DIR / "lauren.agent.json")
        self.legacy_orch = _read_json(LEGACY_DIR / "orchestrator.agent.json")

    def test_v1_detected(self):
        self.assertTrue(is_v1_document(self.legacy_lauren))
        self.assertTrue(is_v1_document(self.legacy_orch))

    def test_v1_loads_via_compat_path(self):
        doc = from_dict(self.legacy_lauren)
        self.assertEqual(doc.document_version, DOCUMENT_VERSION_V1_MIGRATED)
        self.assertTrue(doc.is_migrated)

    def test_v1_capabilities_become_requires_methods(self):
        doc = from_dict(self.legacy_lauren)
        self.assertEqual(
            doc.requires.methods,
            self.legacy_lauren["capabilities"],
        )
        self.assertEqual(doc.requires.scopes, [])
        self.assertFalse(doc.requires.wildcards)

    def test_v1_skills_seeded_from_description(self):
        doc = from_dict(self.legacy_lauren)
        self.assertEqual(len(doc.skills), 1)
        self.assertEqual(doc.skills[0], self.legacy_lauren["description"])

    def test_to_json_emits_clean_v2(self):
        doc = from_dict(self.legacy_lauren)
        rebuilt = json.loads(doc.to_json())
        self.assertEqual(rebuilt["document_version"], DOCUMENT_VERSION_V2)
        self.assertNotIn("capabilities", rebuilt)
        self.assertIn("skills", rebuilt)
        self.assertIn("requires", rebuilt)

    def test_round_trip_v1_to_v2_to_load_is_lossless(self):
        first = from_dict(self.legacy_lauren)
        clean_v2 = json.loads(first.to_json())
        second = from_dict(clean_v2)
        # Migration flag drops away on the second pass; everything else
        # stays equivalent.
        self.assertFalse(second.is_migrated)
        self.assertEqual(first.requires.methods, second.requires.methods)
        self.assertEqual(first.skills, second.skills)
        self.assertEqual(first.scopes_accepted, second.scopes_accepted)


# ---------------------------------------------------------------------------
# Renderer.
# ---------------------------------------------------------------------------


class RendererTests(unittest.TestCase):

    def test_renderer_includes_skills_section(self):
        doc = from_dict(_read_json(LIVE_DIR / "lauren.agent.json"))
        html = render_html(doc)
        self.assertIn(">Skills<", html)
        for skill in doc.skills:
            self.assertIn(skill, html)

    def test_renderer_includes_requires_subsections(self):
        doc = from_dict(_read_json(LIVE_DIR / "lauren.agent.json"))
        html = render_html(doc)
        self.assertIn("Methods Needed", html)
        self.assertIn("Scopes", html)

    def test_renderer_marks_wildcard_orchestrators(self):
        doc = from_dict(_read_json(LIVE_DIR / "orchestrator.agent.json"))
        html = render_html(doc)
        self.assertIn("wildcards-on", html)
        self.assertIn("Wildcard", html)

    def test_renderer_marks_strict_agents(self):
        doc = from_dict(_read_json(LIVE_DIR / "lauren.agent.json"))
        html = render_html(doc)
        self.assertIn("wildcards-off", html)

    def test_renderer_marks_migrated_documents(self):
        doc = from_dict(_read_json(LEGACY_DIR / "lauren.agent.json"))
        html = render_html(doc)
        self.assertIn("Migrated from v1", html)


# ---------------------------------------------------------------------------
# agtp-migrate CLI.
# ---------------------------------------------------------------------------


class MigrateCLITests(unittest.TestCase):

    def test_check_mode_does_not_modify(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "lauren.agent.json"
            target.write_text(
                (LEGACY_DIR / "lauren.agent.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            before = target.read_text(encoding="utf-8")
            out = subprocess.run(
                [sys.executable, "-m", "client.migrate", "--check", str(target)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("WOULD-MIGRATE", out.stdout)
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_migrates_in_place_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "lauren.agent.json"
            target.write_text(
                (LEGACY_DIR / "lauren.agent.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            out = subprocess.run(
                [sys.executable, "-m", "client.migrate", str(target)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("MIGRATED", out.stdout)
            after = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(after["document_version"], "v2")
            self.assertNotIn("capabilities", after)
            backup = target.with_suffix(target.suffix + ".v1.bak")
            self.assertTrue(backup.exists())

    def test_migrate_idempotent_on_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "lauren.agent.json"
            target.write_text(
                (LIVE_DIR / "lauren.agent.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            out = subprocess.run(
                [sys.executable, "-m", "client.migrate", str(target)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(out.returncode, 0)
            self.assertIn("already v2", out.stdout)


if __name__ == "__main__":
    unittest.main()
