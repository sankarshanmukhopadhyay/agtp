"""
Tests for the Phase-6 catalog evolution surface.

Coverage:

  * ``core.methods`` exposes ``catalog_version``,
    ``catalog_versions_supported``, ``is_deprecated``,
    ``deprecation_metadata``, ``CatalogWarning``.
  * The dispatcher stamps ``AGTP-Catalog-Warning`` on responses
    for deprecated verbs; non-deprecated verbs don't get the
    header.
  * The manifest exposes ``catalog_version`` and
    ``catalog_versions_supported``.
  * ``SynthesisRuntime.invalidate_against_catalog`` expires plans
    that reference removed verbs and leaves clean plans untouched.
  * ``@method`` and ``register_custom`` emit ``CatalogWarning`` and
    skip registration when the verb isn't in the catalog
    (rather than aborting the boot sequence).
  * ``[policies.methods]`` loader skips allow / disallow / redirect
    entries referencing unknown verbs with a warning.
  * The CLI surfaces ``AGTP-Catalog-Warning`` to the user.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.methods import (
    CatalogWarning,
    _METHODS_DOC,
    catalog_version,
    catalog_versions_supported,
    deprecation_metadata,
    is_approved_verb,
    is_deprecated,
)


# ---------------------------------------------------------------------------
# Helper: mutate ``_METHODS_DOC`` in place, restored at teardown.
# ---------------------------------------------------------------------------


class _CatalogPatcher:
    """Context manager that injects deprecation metadata into the
    loaded ``_METHODS_DOC`` and restores it on exit. Most tests need
    a deprecated method to exercise the surface; the production
    catalog (methods.json) has none."""

    def __init__(self, deprecate: dict, version: str = "1.0.0"):
        self.deprecate = deprecate
        self.version = version
        self._snapshot: dict = {}
        self._old_version = None

    def __enter__(self):
        self._old_version = _METHODS_DOC.get("version")
        _METHODS_DOC["version"] = self.version
        for name, meta in self.deprecate.items():
            entry = _METHODS_DOC["methods"].get(name)
            if entry is None:
                raise RuntimeError(
                    f"_CatalogPatcher: method {name!r} not in catalog"
                )
            self._snapshot[name] = dict(entry)
            for k, v in meta.items():
                entry[k] = v
        return self

    def __exit__(self, *args):
        _METHODS_DOC["version"] = self._old_version
        for name, original in self._snapshot.items():
            _METHODS_DOC["methods"][name] = original


# ===========================================================================
# Catalog API.
# ===========================================================================


class CatalogAPITests(unittest.TestCase):

    def test_catalog_version_returns_loaded_version(self):
        # The shipped catalog is at 1.0.0; if it bumps, this test
        # follows the file rather than pinning a stale string.
        v = catalog_version()
        self.assertRegex(v, r"^\d+\.\d+\.\d+$")

    def test_catalog_versions_supported_contains_current(self):
        # Phase 6 ships single-version support; the list is exactly
        # one entry, the current version.
        self.assertEqual(
            catalog_versions_supported(), [catalog_version()],
        )

    def test_is_deprecated_false_for_active_verb(self):
        # Production catalog has no deprecated verbs; pick any
        # well-known approved verb and confirm.
        self.assertTrue(is_approved_verb("RECONCILE"))
        self.assertFalse(is_deprecated("RECONCILE"))

    def test_is_deprecated_false_for_unknown_verb(self):
        # Unknown verbs return False (they're not in the catalog at
        # all — different from "deprecated", which means present
        # but flagged).
        self.assertFalse(is_deprecated("NOT_A_REAL_VERB_QFZBM"))

    def test_is_deprecated_true_under_patched_catalog(self):
        with _CatalogPatcher({
            "RECONCILE": {
                "deprecated_in": "1.1.0",
                "removed_in": "2.0.0",
                "successor": "AUDIT",
            },
        }):
            self.assertTrue(is_deprecated("RECONCILE"))
            # is_approved_verb still returns True — deprecation
            # doesn't remove a verb from the catalog.
            self.assertTrue(is_approved_verb("RECONCILE"))

    def test_deprecation_metadata_returns_all_three_fields(self):
        with _CatalogPatcher({
            "RECONCILE": {
                "deprecated_in": "1.1.0",
                "removed_in": "2.0.0",
                "successor": "AUDIT",
            },
        }):
            meta = deprecation_metadata("RECONCILE")
            self.assertEqual(meta, {
                "deprecated_in": "1.1.0",
                "removed_in": "2.0.0",
                "successor": "AUDIT",
            })

    def test_deprecation_metadata_handles_missing_optional_fields(self):
        with _CatalogPatcher({
            "RECONCILE": {"deprecated_in": "1.1.0"},
        }):
            meta = deprecation_metadata("RECONCILE")
            self.assertEqual(meta["deprecated_in"], "1.1.0")
            self.assertIsNone(meta["removed_in"])
            self.assertIsNone(meta["successor"])

    def test_deprecation_metadata_returns_none_for_active_verb(self):
        self.assertIsNone(deprecation_metadata("RECONCILE"))

    def test_catalog_warning_is_deprecation_warning_subclass(self):
        # Callers that opt into stricter handling via
        # ``warnings.filterwarnings("error", DeprecationWarning)``
        # need CatalogWarning to inherit from it.
        self.assertTrue(issubclass(CatalogWarning, DeprecationWarning))


# ===========================================================================
# Dispatcher header.
# ===========================================================================


class DispatcherDeprecationHeaderTests(unittest.TestCase):
    """End-to-end check: invoking a deprecated verb stamps the
    ``AGTP-Catalog-Warning`` header on the response.

    We invoke against the in-process server fixture used by the
    rest of the suite, with the catalog patched to mark QUERY as
    deprecated for the duration of one request."""

    def _dispatch(self, method: str):
        from core.identity import AgentDocument, RequiresDeclaration
        from server.endpoint_registry import EndpointRegistry
        from server.methods import dispatch
        from server.config import default_methods_policy as default_policy

        agent = AgentDocument(
            agtp_version="1.0", agent_id="0" * 64, name="T",
            principal="p", principal_id="0" * 64, description="",
            status="active", skills=[],
            requires=RequiresDeclaration(
                methods=["QUERY", "RECONCILE"], scopes=[], wildcards=False,
            ),
            scopes_accepted=[],
            issued_at="2026-05-09T00:00:00Z", issuer="t.local",
        )

        class _State:
            endpoint_registry = EndpointRegistry()
            methods_policy = default_policy()
            synthesis_runtime = None

            def list_ids(_self): return []
            def lookup(_self, _id): return None

        body = json.dumps({"intent": "ping"}).encode("utf-8")
        req = wire.AGTPRequest(
            method=method,
            headers={"Content-Type": "application/json",
                     "Agent-ID": agent.agent_id, "Host": "localhost"},
            body_bytes=body, path="/",
        )
        return dispatch(req, _State(), agent)

    def test_deprecated_verb_stamps_header(self):
        with _CatalogPatcher({
            "QUERY": {
                "deprecated_in": "1.1.0",
                "removed_in": "2.0.0",
                "successor": "FETCH",
            },
        }):
            resp = self._dispatch("QUERY")
        header = resp.headers.get("AGTP-Catalog-Warning")
        self.assertIsNotNone(header)
        self.assertIn("deprecated", header)
        self.assertIn("successor=FETCH", header)
        self.assertIn("removed_in=2.0.0", header)

    def test_header_omits_optional_fields_when_missing(self):
        with _CatalogPatcher({
            "QUERY": {"deprecated_in": "1.1.0"},
        }):
            resp = self._dispatch("QUERY")
        header = resp.headers.get("AGTP-Catalog-Warning")
        self.assertIsNotNone(header)
        self.assertEqual(header, "deprecated")

    def test_non_deprecated_verb_gets_no_header(self):
        # No catalog patch → no deprecation → no header.
        resp = self._dispatch("QUERY")
        self.assertNotIn("AGTP-Catalog-Warning", resp.headers)


# ===========================================================================
# Manifest catalog version.
# ===========================================================================


class ManifestCatalogVersionTests(unittest.TestCase):

    def test_manifest_dict_includes_catalog_version(self):
        from core.identity import AgentDocument, RequiresDeclaration
        from server.config import (
            AgentsConfig, ServerConfig, ServerInfo, ServerPolicy,
        )
        from server.manifest import generate
        cfg = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact="",
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )
        m = generate(cfg, agents={})
        d = m.to_dict()
        self.assertIn("catalog_version", d)
        self.assertIn("catalog_versions_supported", d)
        self.assertEqual(d["catalog_version"], catalog_version())
        self.assertEqual(
            d["catalog_versions_supported"], catalog_versions_supported(),
        )

    def test_manifest_catalog_version_omitted_when_unset(self):
        # The dataclass default is ``""``; a manifest constructed
        # without a catalog version should NOT emit the field.
        from core.identity import utc_now_iso
        from core.manifest import (
            PolicyBlock, ServerInfoBlock, ServerManifest,
        )
        now = utc_now_iso()
        m = ServerManifest(
            agtp_version="1.0",
            agtp_api_version="1.0",
            document_version="v2",
            server=ServerInfoBlock(
                server_id="t", operator="o", contact="c",
                supported_features=[], issued=now, updated=now,
            ),
            embedded_methods=[],
            agent_disclosure="public",
            hosted_agents=[],
            policies=PolicyBlock(
                wildcards_accepted=True,
                anonymous_discovery=True,
                scope_required_for_invocation=False,
            ),
        )
        d = m.to_dict()
        self.assertNotIn("catalog_version", d)
        self.assertNotIn("catalog_versions_supported", d)


# ===========================================================================
# Synthesis invalidation.
# ===========================================================================


class SynthesisInvalidationTests(unittest.TestCase):

    def _runtime(self):
        from core.endpoint import EndpointSpec, ParamSpec, SemanticBlock
        from server.synthesis import (
            PassthroughPolicy, SynthesisRuntime,
        )
        from server.synthesis.plan import (
            CompositionStep, ParameterSource, SynthesisPlan,
        )
        runtime = SynthesisRuntime(
            policies=[PassthroughPolicy()],
            step_dispatcher=lambda *a: None,
        )

        def _spec(name):
            return EndpointSpec(
                name=name, path="/x",
                semantic=SemanticBlock(
                    intent="x.", actor="agent",
                    outcome="x.", capability="retrieval",
                    confidence=0.9,
                    impact="informational",
                    is_idempotent=True,
                ),
            )

        def _plan(name, *step_methods):
            return SynthesisPlan(
                proposed_method=_spec(name),
                steps=[
                    CompositionStep(
                        method_name=m,
                        parameter_source={},
                    )
                    for m in step_methods
                ],
                output_aggregation="last",
            )
        return runtime, _plan

    def test_clean_plan_survives(self):
        runtime, _plan = self._runtime()
        sid = runtime.instantiate(_plan("QUERY_AUDIT", "QUERY"))
        expired = runtime.invalidate_against_catalog()
        self.assertEqual(expired, [])
        self.assertIsNotNone(runtime.get(sid))

    def test_plan_with_removed_verb_is_expired(self):
        runtime, _plan = self._runtime()
        sid = runtime.instantiate(
            _plan("AUDIT", "QUERY", "FORGOTTEN_VERB_QFZBM"),
        )
        expired = runtime.invalidate_against_catalog()
        self.assertEqual(expired, [sid])
        self.assertIsNone(runtime.get(sid))

    def test_invalidation_returns_list_of_expired_ids(self):
        runtime, _plan = self._runtime()
        s1 = runtime.instantiate(_plan("AUDIT", "QUERY"))
        s2 = runtime.instantiate(_plan("AUDIT2", "FORGOTTEN_QFZBM"))
        s3 = runtime.instantiate(_plan("AUDIT3", "SUMMARIZE"))
        expired = runtime.invalidate_against_catalog()
        self.assertEqual(expired, [s2])
        # Surviving plans still resolve.
        self.assertIsNotNone(runtime.get(s1))
        self.assertIsNotNone(runtime.get(s3))

    def test_expire_records_reason_to_stderr(self):
        runtime, _plan = self._runtime()
        sid = runtime.instantiate(_plan("AUDIT", "QUERY"))
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            runtime.expire(sid, reason="catalog-evolution-removed-verb")
        self.assertIn("catalog-evolution-removed-verb", captured.getvalue())
        self.assertIn(sid, captured.getvalue())

    def test_expire_without_reason_stays_silent(self):
        runtime, _plan = self._runtime()
        sid = runtime.instantiate(_plan("AUDIT", "QUERY"))
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            runtime.expire(sid)
        # Pre-Phase-6 callers continue to work unchanged.
        self.assertEqual(captured.getvalue(), "")


# ===========================================================================
# Graceful @method / register_custom on unknown verbs.
# ===========================================================================


class GracefulRegistrationTests(unittest.TestCase):

    def test_method_decorator_emits_catalog_warning_for_unknown_verb(self):
        from server.methods import method, REGISTRY
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", CatalogWarning)

            @method(
                name="NOT_A_REAL_VERB_QFZBM",
                category="cognitive",
                semantic_class="action-intent",
                idempotent=True,
                state_modifying=False,
                required_params=[],
                error_codes=[400],
                description="bogus",
            )
            def _bogus(req, st, doc):
                return None

        self.assertTrue(any(
            issubclass(w.category, CatalogWarning) for w in caught
        ))
        # The function is returned unmodified — but NOT registered.
        self.assertNotIn("NOT_A_REAL_VERB_QFZBM", REGISTRY)
        # The decorator returned the original function.
        self.assertTrue(callable(_bogus))

    def test_register_custom_emits_catalog_warning_for_unknown_verb(self):
        from server.methods import register_custom, REGISTRY
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", CatalogWarning)
            spec = register_custom(
                lambda *a, **k: None,
                name="NOT_A_REAL_VERB_QFZBM",
                namespace="test",
                category="transact",
                semantic_class="action-intent",
                idempotent=False,
                state_modifying=True,
                required_params=["x"],
                error_codes=[400],
                description="bogus",
            )
        self.assertTrue(any(
            issubclass(w.category, CatalogWarning) for w in caught
        ))
        self.assertNotIn("NOT_A_REAL_VERB_QFZBM", REGISTRY)
        self.assertIsNone(spec)

    def test_method_decorator_registers_normally_for_approved_verb(self):
        from server.methods import method, REGISTRY, unregister
        # FORECAST is in the catalog and not yet registered.
        try:
            @method(
                name="FORECAST",
                category="cognitive",
                semantic_class="action-intent",
                idempotent=True,
                state_modifying=False,
                required_params=[],
                error_codes=[400],
                description="forecasting",
                namespace="test",
                intent="Forecast a future value from current data.",
                actor="agent",
                outcome="A forecast is returned.",
                capability="analysis",
                confidence=0.85,
                impact="informational",
                is_idempotent=True,
            )
            def _forecast(req, st, doc):
                return None
            self.assertIn("FORECAST", REGISTRY)
        finally:
            if "FORECAST" in REGISTRY:
                unregister("FORECAST")


# ===========================================================================
# [policies.methods] graceful skip of unknown verbs.
# ===========================================================================


class MethodPolicyGracefulSkipTests(unittest.TestCase):
    """The TOML [policies.methods] loader (§6) is catalog-graceful:
    directives referencing verbs the catalog has removed emit a
    CatalogWarning and skip silently."""

    def test_allow_unknown_verb_is_skipped_with_warning(self):
        from server.config import methods_policy_from_table
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", CatalogWarning)
            with mock.patch("sys.stderr"):
                p = methods_policy_from_table({
                    "allow": ["RECONCILE", "NOT_A_REAL_VERB_QFZBM"],
                })
        # The unknown verb is skipped; the known verb survives.
        self.assertEqual(p.allow, {"RECONCILE"})
        self.assertTrue(any(
            issubclass(w.category, CatalogWarning) for w in caught
        ))

    def test_disallow_unknown_verb_is_skipped(self):
        from server.config import methods_policy_from_table
        with warnings.catch_warnings(record=True), mock.patch("sys.stderr"):
            p = methods_policy_from_table({
                "allow": "*",
                "disallow": ["NOT_A_REAL_VERB_QFZBM", "RECONCILE"],
            })
        self.assertEqual(p.disallow, {"RECONCILE"})

    def test_disallow_legacy_verb_is_admitted(self):
        # Operators routinely write ``disallow = ["PATCH"]`` to
        # override a wildcard legacy opt-in. The graceful-skip layer
        # admits legacy names alongside catalog-approved ones.
        from server.config import methods_policy_from_table
        p = methods_policy_from_table({
            "allow": "*",
            "disallow": ["PATCH"],
        })
        self.assertEqual(p.disallow, {"PATCH"})

    def test_redirect_unknown_source_skipped(self):
        from server.config import methods_policy_from_table
        with warnings.catch_warnings(record=True), mock.patch("sys.stderr"):
            p = methods_policy_from_table({
                "redirects": [
                    {"from_method": "NOTAREALVERBQFZBM",
                     "to_method": "RECONCILE"},
                ],
            })
        # Skipped: redirects map empty.
        self.assertEqual(p.redirects, {})

    def test_redirect_unknown_destination_skipped(self):
        from server.config import methods_policy_from_table
        with warnings.catch_warnings(record=True), mock.patch("sys.stderr"):
            p = methods_policy_from_table({
                "redirects": [
                    {"from_method": "RECONCILE",
                     "to_method": "NOTAREALVERBQFZBM"},
                ],
            })
        self.assertEqual(p.redirects, {})


# ===========================================================================
# CLI surfaces AGTP-Catalog-Warning to user.
# ===========================================================================


class CLIWarningSurfaceTests(unittest.TestCase):

    def test_print_catalog_warning_includes_successor_and_removed_in(self):
        from client.cli.main import _print_catalog_warning
        from client.core_client import FetchResult
        result = FetchResult(
            ok=True, kind="method-response",
            status_code=200, status_text="OK",
            headers={
                "Content-Type": "application/json",
                "AGTP-Catalog-Warning":
                    "deprecated; successor=AUDIT; removed_in=2.0.0",
            },
            body_bytes=b"{}", parsed={},
        )
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            _print_catalog_warning(result, "AUDIT_LEGACY")
        out = captured.getvalue()
        self.assertIn("WARNING", out)
        self.assertIn("AUDIT_LEGACY", out)
        self.assertIn("AUDIT", out)
        self.assertIn("2.0.0", out)

    def test_print_catalog_warning_silent_when_header_absent(self):
        from client.cli.main import _print_catalog_warning
        from client.core_client import FetchResult
        result = FetchResult(
            ok=True, kind="method-response",
            status_code=200, status_text="OK",
            headers={"Content-Type": "application/json"},
            body_bytes=b"{}", parsed={},
        )
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            _print_catalog_warning(result, "QUERY")
        self.assertEqual(captured.getvalue(), "")

    def test_print_catalog_warning_handles_partial_header(self):
        # Header without successor / removed_in (deprecated-only)
        # still produces a clean message.
        from client.cli.main import _print_catalog_warning
        from client.core_client import FetchResult
        result = FetchResult(
            ok=True, kind="method-response",
            status_code=200, status_text="OK",
            headers={"AGTP-Catalog-Warning": "deprecated"},
            body_bytes=b"{}", parsed={},
        )
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            _print_catalog_warning(result, "AUDIT_LEGACY")
        out = captured.getvalue()
        self.assertIn("AUDIT_LEGACY", out)
        self.assertIn("deprecated", out)
        self.assertNotIn("Successor:", out)
        self.assertNotIn("Removed in:", out)


# ===========================================================================
# Drawer bridge surfaces deprecation in the catalog feed.
# ===========================================================================


class BridgeCatalogFeedTests(unittest.TestCase):

    def test_get_verb_catalog_marks_deprecated_entries(self):
        from client.elemen.bridge import Api
        api = Api()
        with _CatalogPatcher({
            "RECONCILE": {
                "deprecated_in": "1.1.0",
                "successor": "AUDIT",
                "removed_in": "2.0.0",
            },
        }):
            catalog = api.get_verb_catalog()
        recon = next(e for e in catalog if e["name"] == "RECONCILE")
        self.assertTrue(recon["deprecated"])
        self.assertEqual(recon["successor"], "AUDIT")
        self.assertEqual(recon["removed_in"], "2.0.0")
        self.assertEqual(recon["deprecated_in"], "1.1.0")

    def test_get_verb_catalog_active_entries_have_deprecated_false(self):
        from client.elemen.bridge import Api
        catalog = Api().get_verb_catalog()
        # Production catalog has no deprecated entries.
        self.assertTrue(all(
            entry.get("deprecated") is False
            for entry in catalog
        ))

    def test_get_catalog_version_returns_version_and_supported(self):
        from client.elemen.bridge import Api
        out = Api().get_catalog_version()
        self.assertIn("version", out)
        self.assertIn("supported", out)
        self.assertEqual(out["version"], catalog_version())


if __name__ == "__main__":
    unittest.main()
