"""
Drift detection between client/amg and server/amg.

These two trees are intentionally parallel implementations of the
same AMG specification (the SMTP MTA pattern: same protocol, two
distinct user agents). They should remain functionally identical
unless a divergence is explicitly justified.

This suite is the gate. Three layers:

  1. **Module-level diff.** Each shared module
     (grammar / reserved / validator / substitution / synthesis /
     composer) must differ only in import-path prefix
     (``client.amg`` vs ``server.amg``). Anything else triggers a
     loud failure that names the file.

  2. **Behavioral parity.** Round the same fixture through both
     composers and the same spec through both validators; the
     resulting dataclasses / pass results must compare equal.

  3. **Public API parity.** ``client.amg.__all__`` and
     ``server.amg.__all__`` must list the same public names.

When a divergence is intentional (e.g., server-only catalog-aware
validation lands in a future revision), append the file or symbol
to the exception list at the top of this module with a short
comment explaining why.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Set

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CLIENT_AMG = REPO / "client" / "amg"
SERVER_AMG = REPO / "server" / "amg"
FIXTURES = REPO / "tests" / "fixtures" / "amg"

# Modules expected to be byte-identical (modulo import-path prefix).
SHARED_MODULES = (
    "grammar.py",
    "reserved.py",
    "validator.py",
    "substitution.py",
    "synthesis.py",
    "composer.py",
)

# Files where intentional divergence is allowed. Empty by design;
# add entries here ONLY when a divergence is justified, and document
# the reason in the comment alongside the entry.
#
#   MODULE_EXCEPTIONS = {
#       "validator.py": "server-only catalog-aware Pass 9 (added 2026-Q3)",
#   }
MODULE_EXCEPTIONS: dict = {}

# Public-API symbols where intentional one-sided exposure is allowed.
# Empty by design; same convention as MODULE_EXCEPTIONS.
API_EXCEPTIONS: Set[str] = set()


def _normalize_imports(text: str, side: str) -> str:
    """
    Return ``text`` with import-path prefixes from the named side
    replaced by a neutral placeholder, and line endings normalized
    to LF. The two normalized blobs should be byte-identical when
    the two trees are in sync.
    """
    # Line-ending agnostic: editors and Python file writers on
    # Windows sometimes inject CRLF; that does not count as drift.
    text = text.replace("\r\n", "\n")
    if side == "client":
        return (text
                .replace("from client.amg.", "from XXX.amg.")
                .replace("from client.amg ", "from XXX.amg ")
                .replace("import client.amg.", "import XXX.amg.")
                .replace("client.amg.cli", "XXX.amg.cli")
                .replace("``client.amg.", "``XXX.amg.")
                .replace("client.amg.composer", "XXX.amg.composer"))
    if side == "server":
        return (text
                .replace("from server.amg.", "from XXX.amg.")
                .replace("from server.amg ", "from XXX.amg ")
                .replace("import server.amg.", "import XXX.amg.")
                .replace("server.amg.cli", "XXX.amg.cli")
                .replace("``server.amg.", "``XXX.amg.")
                .replace("server.amg.composer", "XXX.amg.composer"))
    raise ValueError(side)


# ---------------------------------------------------------------------------
# Layer 1: module-level diff.
# ---------------------------------------------------------------------------


class ModuleDriftTests(unittest.TestCase):

    def test_modules_differ_only_in_import_paths(self):
        for module in SHARED_MODULES:
            with self.subTest(module=module):
                if module in MODULE_EXCEPTIONS:
                    self.skipTest(
                        f"{module} on the exception list: "
                        f"{MODULE_EXCEPTIONS[module]}"
                    )
                client_path = CLIENT_AMG / module
                server_path = SERVER_AMG / module
                self.assertTrue(
                    client_path.exists(),
                    f"missing client-side file: {client_path}",
                )
                self.assertTrue(
                    server_path.exists(),
                    f"missing server-side file: {server_path}",
                )
                client_text = client_path.read_text(encoding="utf-8")
                server_text = server_path.read_text(encoding="utf-8")
                client_normalized = _normalize_imports(client_text, "client")
                server_normalized = _normalize_imports(server_text, "server")
                if client_normalized != server_normalized:
                    self.fail(
                        f"\n{module} drifted between client/amg and "
                        f"server/amg beyond import-path differences.\n"
                        f"  Diff them and decide whether the divergence is "
                        f"intentional:\n"
                        f"    diff client/amg/{module} server/amg/{module}\n"
                        f"  If intentional, add {module!r} to "
                        f"MODULE_EXCEPTIONS in tests/test_amg_drift.py "
                        f"with a comment explaining why."
                    )


# ---------------------------------------------------------------------------
# Layer 2: behavioral parity.
# ---------------------------------------------------------------------------


def _import_both():
    from client import amg as client_amg
    from server import amg as server_amg
    return client_amg, server_amg


class BehavioralParityTests(unittest.TestCase):

    EXTRA_KNOWN = {"VALIDATE", "BOOK"}

    def test_compose_from_yaml_evaluate_produces_identical_specs(self):
        client_amg, server_amg = _import_both()
        client_spec = client_amg.compose_from_yaml(
            FIXTURES / "evaluate.method.yaml",
            known_methods=self.EXTRA_KNOWN,
        )
        server_spec = server_amg.compose_from_yaml(
            FIXTURES / "evaluate.method.yaml",
            known_methods=self.EXTRA_KNOWN,
        )
        self.assertEqual(client_spec.to_dict(), server_spec.to_dict())

    def test_compose_from_yaml_reserve_produces_identical_specs(self):
        client_amg, server_amg = _import_both()
        client_spec = client_amg.compose_from_yaml(
            FIXTURES / "reserve.method.yaml",
            known_methods=self.EXTRA_KNOWN,
        )
        server_spec = server_amg.compose_from_yaml(
            FIXTURES / "reserve.method.yaml",
            known_methods=self.EXTRA_KNOWN,
        )
        self.assertEqual(client_spec.to_dict(), server_spec.to_dict())

    def test_lexical_failure_is_identical(self):
        # lowercase fixture should fail Pass 1 the same way on both sides.
        client_amg, server_amg = _import_both()
        with self.assertRaises(client_amg.CompositionError) as cclient:
            client_amg.compose_from_yaml(
                FIXTURES / "lowercase_name.method.yaml"
            )
        with self.assertRaises(server_amg.CompositionError) as cserver:
            server_amg.compose_from_yaml(
                FIXTURES / "lowercase_name.method.yaml"
            )
        c_err = cclient.exception.validation_result.error
        s_err = cserver.exception.validation_result.error
        self.assertEqual(c_err.pass_name, s_err.pass_name)
        self.assertEqual(c_err.code, s_err.code)
        self.assertEqual(c_err.message, s_err.message)

    def test_stoplist_failure_is_identical(self):
        client_amg, server_amg = _import_both()
        with self.assertRaises(client_amg.CompositionError) as cclient:
            client_amg.compose_from_yaml(
                FIXTURES / "stoplist_name.method.yaml"
            )
        with self.assertRaises(server_amg.CompositionError) as cserver:
            server_amg.compose_from_yaml(
                FIXTURES / "stoplist_name.method.yaml"
            )
        self.assertEqual(
            cclient.exception.validation_result.error.code,
            cserver.exception.validation_result.error.code,
        )

    def test_missing_outcome_coherence_failure_is_identical(self):
        # missing_outcome.method.yaml fails coherence (composer-side),
        # not validator. Both sides should reject it identically.
        client_amg, server_amg = _import_both()
        with self.assertRaises(client_amg.CompositionError) as cclient:
            client_amg.compose_from_yaml(
                FIXTURES / "missing_outcome.method.yaml"
            )
        with self.assertRaises(server_amg.CompositionError) as cserver:
            server_amg.compose_from_yaml(
                FIXTURES / "missing_outcome.method.yaml"
            )
        self.assertEqual(str(cclient.exception), str(cserver.exception))

    def test_validate_partial_parity(self):
        # validate_partial drives the Elemen Compose drawer; both
        # sides must produce identical structured output for the
        # same draft so a UI authored against either side works
        # against the other.
        client_amg, server_amg = _import_both()
        draft = {
            "name": "STATUS",
            "description": "check the status of running things in the system",
            "semantic": {
                "intent": "checks the running status of a thing",
                "actor": "agent",
                "outcome": "the running status is reported back",
                "impact_tier": "irreversible",
                "confidence_guidance": 0.5,
            },
            "namespace": "acme-store",
            "source": "amg/1.0",
        }
        c_out = client_amg.validate_partial(draft)
        s_out = server_amg.validate_partial(draft)
        self.assertEqual(c_out, s_out)
        # Sanity: STATUS triggers the stoplist; should be invalid.
        self.assertFalse(c_out["valid"])
        self.assertIn("name", c_out["errors"])

    def test_irreversible_low_confidence_warning_is_identical(self):
        client_amg, server_amg = _import_both()
        params = dict(
            intent="Permanently destroys the named record",
            actor="agent",
            outcome="The record and any related state are erased",
            capability="modification",
            confidence_guidance=0.50,
            impact_tier="irreversible",
            is_idempotent=False,
            namespace="acme-store",
            required_params=[
                {"name": "record_id", "type": "string",
                 "description": "the record to destroy"},
            ],
        )
        c_spec = client_amg.compose_method("PURGE", **params)
        s_spec = server_amg.compose_method("PURGE", **params)
        c_warnings = c_spec.__dict__.get("_composer_warnings", [])
        s_warnings = s_spec.__dict__.get("_composer_warnings", [])
        self.assertEqual(c_warnings, s_warnings)
        # Sanity: at least one warning was raised on both sides.
        self.assertTrue(any("irreversible" in w.lower() for w in c_warnings))


# ---------------------------------------------------------------------------
# Layer 3: public API parity.
# ---------------------------------------------------------------------------


class PublicAPIParityTests(unittest.TestCase):

    def test_public_api_parity(self):
        client_amg, server_amg = _import_both()

        client_exports = (
            set(client_amg.__all__)
            if hasattr(client_amg, "__all__")
            else set(dir(client_amg))
        )
        server_exports = (
            set(server_amg.__all__)
            if hasattr(server_amg, "__all__")
            else set(dir(server_amg))
        )
        client_public = {s for s in client_exports if not s.startswith("_")}
        server_public = {s for s in server_exports if not s.startswith("_")}

        only_in_client = (client_public - server_public) - API_EXCEPTIONS
        only_in_server = (server_public - client_public) - API_EXCEPTIONS

        if only_in_client or only_in_server:
            self.fail(
                "client.amg and server.amg public APIs have diverged.\n"
                f"  client-only:  {sorted(only_in_client)}\n"
                f"  server-only:  {sorted(only_in_server)}\n"
                "  Mirror the addition to the other side, or add the "
                "symbol to API_EXCEPTIONS in tests/test_amg_drift.py "
                "with a comment explaining why."
            )

    def test_validate_signature_parity(self):
        # The validate function is the protocol's load-bearing entry
        # point. Sanity-check that both sides expose the same callable
        # contract so a server consumer and a client consumer can swap.
        from client.amg import validate as client_validate
        from server.amg import validate as server_validate
        import inspect
        c_sig = inspect.signature(client_validate)
        s_sig = inspect.signature(server_validate)
        self.assertEqual(str(c_sig), str(s_sig))

    def test_compose_method_signature_parity(self):
        from client.amg import compose_method as client_compose
        from server.amg import compose_method as server_compose
        import inspect
        c_sig = inspect.signature(client_compose)
        s_sig = inspect.signature(server_compose)
        self.assertEqual(str(c_sig), str(s_sig))


if __name__ == "__main__":
    unittest.main()
