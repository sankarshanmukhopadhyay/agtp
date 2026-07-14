"""
Tests for the OAuth no-op-validator boot guard.

Background: ``NoOpValidator`` accepts any non-empty bearer token —
useful for local development and CI fixtures, dangerous in
production. The only mitigation used to be a stderr warning at boot,
which is easy to miss in a container/orchestrator log pipeline and
does nothing at request time. This suite pins down the fix: loading
a config with ``[policies.oauth] enabled = true`` and
``validator = "noop"`` now refuses to boot (raises ``ValueError``)
unless ``allow_noop_validator = true`` is set explicitly.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config as cfg_module


def _write(tmp_path: Path, oauth_block: str) -> Path:
    f = tmp_path / "s.toml"
    f.write_text(
        f"""
[server]
server_id = "t.local"
operator = "o"
contact = "c"

[policies.oauth]
{oauth_block}
""",
        encoding="utf-8",
    )
    return f


class NoopValidatorBootGuardTests(unittest.TestCase):

    def test_refuses_boot_when_noop_enabled_without_opt_in(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            f = _write(
                Path(tmp),
                'enabled = true\nvalidator = "noop"\n',
            )
            with self.assertRaises(ValueError) as ctx:
                cfg_module.load(f)
            self.assertIn("allow_noop_validator", str(ctx.exception))

    def test_boots_with_explicit_opt_in(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            f = _write(
                Path(tmp),
                'enabled = true\nvalidator = "noop"\n'
                'allow_noop_validator = true\n',
            )
            cfg = cfg_module.load(f)
            self.assertTrue(cfg.oauth.enabled)
            self.assertEqual(cfg.oauth.validator, "noop")
            self.assertTrue(cfg.oauth.allow_noop_validator)

    def test_boots_when_oauth_disabled_even_with_noop(self):
        """enabled = false is the default posture; the noop
        validator being named is irrelevant if OAuth isn't on."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            f = _write(
                Path(tmp),
                'enabled = false\nvalidator = "noop"\n',
            )
            cfg = cfg_module.load(f)
            self.assertFalse(cfg.oauth.enabled)

    def test_boots_when_jwt_validator_named_without_opt_in(self):
        """Only the noop validator triggers the guard; a real
        validator boots normally without allow_noop_validator."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            f = _write(
                Path(tmp),
                'enabled = true\nvalidator = "jwt"\n'
                'validator_config = { public_key = '
                '"MCowBQYDK2VwAyEA' + "A" * 43 + '" }\n',
            )
            # jwt validator instantiation itself may fail on a bogus
            # key at request time, but config *loading* must not be
            # blocked by the noop guard for a non-noop validator.
            cfg = cfg_module.load(f)
            self.assertEqual(cfg.oauth.validator, "jwt")

    def test_default_config_never_triggers_guard(self):
        cfg = cfg_module.default_config()
        self.assertFalse(cfg.oauth.enabled)
        self.assertFalse(cfg.oauth.allow_noop_validator)


if __name__ == "__main__":
    unittest.main()
