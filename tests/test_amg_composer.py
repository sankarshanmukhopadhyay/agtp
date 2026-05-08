"""
Tests for the AMG composer (``client.amg.composer``).

Three modes (function, builder, document) plus the CompositionError
suggestion engine plus the ``agtp-amg compose`` CLI surface.

The fixture set lives in ``tests/fixtures/amg/`` and doubles as
documentation: each *.method.yaml file is a real-world example a
caller can copy.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.amg import (
    AMGMethodSpec,
    CompositionError,
    MethodBuilder,
    ParamSpec,
    SemanticBlock,
    SubstitutionHint,
    compose_from_dict,
    compose_from_json,
    compose_from_yaml,
    compose_method,
    suggest_fix,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "amg"
EXTRA_KNOWN = {"VALIDATE", "BOOK"}


def _good_compose_kwargs(**overrides) -> Dict[str, Any]:
    """Baseline kwargs that produce a valid spec when passed to compose_method."""
    base = dict(
        intent="Reconciles transactions for the named account and period",
        actor="agent",
        outcome="A reconciliation summary listing matched and unmatched entries is returned",
        capability="transaction",
        confidence_guidance=0.9,
        impact_tier="reversible",
        is_idempotent=False,
        category="transact",
        namespace="acme-finance",
        required_params=[
            ParamSpec(name="account_id", type="string",
                      description="the ledger account to reconcile"),
            ParamSpec(name="period", type="string",
                      description="time window like 2026-Q1"),
        ],
        error_codes=[400, 422, 451],
    )
    base.update(overrides)
    return base


# ===========================================================================
# Mode 1: compose_method (function-style).
# ===========================================================================


class ComposeMethodTests(unittest.TestCase):

    def test_valid_composition_returns_spec(self):
        spec = compose_method("RECONCILE", **_good_compose_kwargs())
        self.assertIsInstance(spec, AMGMethodSpec)
        self.assertEqual(spec.name, "RECONCILE")
        self.assertIsNotNone(spec.semantic)
        self.assertEqual(spec.semantic.actor, "agent")

    def test_description_defaults_to_intent(self):
        kwargs = _good_compose_kwargs()
        kwargs.pop("intent", None)
        spec = compose_method(
            "RECONCILE",
            intent="Reconciles transactions for the named account and period",
            **kwargs,
        )
        self.assertEqual(spec.description, spec.semantic.intent)

    def test_error_codes_default_includes_422(self):
        kwargs = _good_compose_kwargs()
        kwargs.pop("error_codes", None)
        spec = compose_method("RECONCILE", **kwargs)
        self.assertIn(422, spec.error_codes)

    def test_missing_intent_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["intent"] = ""
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("intent", str(cm.exception).lower())

    def test_missing_outcome_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["outcome"] = ""
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("outcome", str(cm.exception).lower())

    def test_invalid_actor_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["actor"] = "robot"
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("actor", str(cm.exception).lower())

    def test_invalid_capability_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["capability"] = "telepathy"
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("capability", str(cm.exception).lower())

    def test_invalid_impact_tier_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["impact_tier"] = "catastrophic"
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("impact_tier", str(cm.exception).lower())

    def test_confidence_out_of_range_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["confidence_guidance"] = 1.7
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("confidence_guidance", str(cm.exception).lower())

    def test_idempotent_state_modifying_contradiction_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["is_idempotent"] = True
        kwargs["state_modifying"] = True
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("idempotent", str(cm.exception).lower())

    def test_idempotent_disagrees_with_protocol_idempotent_raises(self):
        kwargs = _good_compose_kwargs()
        kwargs["is_idempotent"] = True
        kwargs["idempotent"] = False  # protocol-level disagrees
        kwargs["state_modifying"] = False
        with self.assertRaises(CompositionError) as cm:
            compose_method("RECONCILE", **kwargs)
        self.assertIn("disagrees", str(cm.exception).lower())

    def test_irreversible_low_confidence_warns_but_succeeds(self):
        kwargs = _good_compose_kwargs()
        kwargs["impact_tier"] = "irreversible"
        kwargs["confidence_guidance"] = 0.50
        spec = compose_method("RECONCILE", **kwargs)
        warnings = spec.__dict__.get("_composer_warnings", [])
        self.assertTrue(
            any("irreversible" in w.lower() for w in warnings),
            f"expected warning about irreversible/low-confidence, got {warnings}",
        )

    def test_lowercase_name_raises_with_uppercase_suggestion(self):
        kwargs = _good_compose_kwargs()
        with self.assertRaises(CompositionError) as cm:
            compose_method("reconcile", **kwargs)
        self.assertTrue(
            any("uppercase" in s.lower() for s in cm.exception.suggestions),
            f"expected uppercase suggestion, got {cm.exception.suggestions}",
        )

    def test_http_method_name_raises_with_action_verb_suggestion(self):
        kwargs = _good_compose_kwargs()
        with self.assertRaises(CompositionError) as cm:
            compose_method("GET", **kwargs)
        joined = "\n".join(cm.exception.suggestions).upper()
        self.assertIn("FETCH", joined)

    def test_stoplist_name_raises_with_substitution_suggestion(self):
        kwargs = _good_compose_kwargs()
        with self.assertRaises(CompositionError) as cm:
            compose_method("STATUS", **kwargs)
        # Should suggest an action verb (CHECK / REPORT or similar).
        self.assertTrue(
            any(("CHECK" in s) or ("REPORT" in s) or ("PROBE" in s)
                for s in cm.exception.suggestions),
            f"expected action-verb suggestion, got {cm.exception.suggestions}",
        )

    def test_amg_source_without_semantic_block_raises(self):
        # The compose_method always builds a semantic block; this
        # case targets the document path more directly. Build a spec
        # without semantic via the dataclass and run validate ourselves.
        spec = AMGMethodSpec(
            name="RECONCILE",
            semantic_class="action-intent",
            category="transact",
            description="A perfectly reasonable description that is long enough.",
            idempotent=False,
            state_modifying=True,
            required_params=[
                ParamSpec(name="account_id", type="string",
                          description="the account"),
            ],
            optional_params=[],
            error_codes=[400, 422],
            source="amg/1.0",
            namespace="acme-finance",
            semantic=None,
        )
        # The composer's coherence check is invoked indirectly by
        # compose_from_dict; here we exercise the same rule with a
        # dict missing the semantic block.
        with self.assertRaises(CompositionError) as cm:
            compose_from_dict({
                **spec.to_dict(),
                # to_dict omits semantic when None; that's the case under test
            })
        self.assertIn("semantic block", str(cm.exception).lower())


# ===========================================================================
# Mode 2: MethodBuilder (fluent builder).
# ===========================================================================


class MethodBuilderTests(unittest.TestCase):

    def test_builder_chains_state(self):
        b = (MethodBuilder("RECONCILE")
             .with_intent("Reconciles transactions for the named account")
             .with_actor("agent")
             .with_outcome("A reconciliation summary is returned")
             .with_capability("transaction")
             .with_idempotent(False)
             .with_namespace("acme-finance")
             .with_required_param("account_id", "string", "the ledger account")
             .with_required_param("period", "string", "time window")
             .with_error_code(400).with_error_code(422).with_error_code(451))
        spec = b.build()
        self.assertEqual(spec.name, "RECONCILE")
        self.assertEqual(len(spec.required_params), 2)
        self.assertIn(422, spec.error_codes)

    def test_builder_build_raises_on_invalid(self):
        b = (MethodBuilder("get")  # lowercase fails Pass 1
             .with_intent("retrieves data")
             .with_actor("agent")
             .with_outcome("data is returned"))
        with self.assertRaises(CompositionError):
            b.build()

    def test_preview_returns_unvalidated_spec(self):
        b = MethodBuilder("INCOMPLETE").with_intent("partial state")
        spec = b.preview()
        self.assertIsInstance(spec, AMGMethodSpec)
        self.assertEqual(spec.name, "INCOMPLETE")
        # Preview does NOT validate; missing actor/outcome is fine here.
        self.assertEqual(spec.semantic.actor, "")

    def test_builder_substitution(self):
        spec = (MethodBuilder("RECONCILE")
                .with_intent("Reconciles transactions for an account")
                .with_actor("agent")
                .with_outcome("A reconciliation summary is returned")
                .with_idempotent(False)
                .with_namespace("acme-finance")
                .with_required_param("account_id", "string", "the account")
                .with_substitution("VALIDATE", "when ruleset is JSON Schema")
                .build(known_methods=EXTRA_KNOWN))
        self.assertEqual(len(spec.substitutes_for), 1)
        self.assertEqual(spec.substitutes_for[0].target_method, "VALIDATE")


# ===========================================================================
# Mode 3: document-form composition.
# ===========================================================================


class DocumentFormTests(unittest.TestCase):

    def test_compose_from_dict_round_trip(self):
        spec_a = compose_method("RECONCILE", **_good_compose_kwargs())
        as_dict = spec_a.to_dict()
        spec_b = compose_from_dict(as_dict)
        self.assertEqual(spec_a.name, spec_b.name)
        self.assertEqual(spec_a.semantic.intent, spec_b.semantic.intent)
        self.assertEqual(
            [p.name for p in spec_a.required_params],
            [p.name for p in spec_b.required_params],
        )

    def test_compose_from_yaml_evaluate_fixture(self):
        spec = compose_from_yaml(
            FIXTURES / "evaluate.method.yaml",
            known_methods=EXTRA_KNOWN,
        )
        self.assertEqual(spec.name, "EVALUATE")
        self.assertEqual(spec.semantic.capability, "analysis")
        self.assertEqual(spec.semantic.is_idempotent, True)
        self.assertEqual(spec.namespace, "acme-quality")

    def test_compose_from_yaml_reserve_fixture(self):
        spec = compose_from_yaml(
            FIXTURES / "reserve.method.yaml",
            known_methods=EXTRA_KNOWN,
        )
        self.assertEqual(spec.name, "RESERVE")
        self.assertEqual(spec.semantic.impact_tier, "reversible")
        self.assertEqual(len(spec.substitutes_for), 1)
        self.assertEqual(spec.substitutes_for[0].target_method, "BOOK")

    def test_compose_from_yaml_lowercase_fixture_fails(self):
        with self.assertRaises(CompositionError) as cm:
            compose_from_yaml(FIXTURES / "lowercase_name.method.yaml")
        self.assertEqual(
            cm.exception.validation_result.error.code, "malformed-name"
        )

    def test_compose_from_yaml_stoplist_fixture_fails(self):
        with self.assertRaises(CompositionError) as cm:
            compose_from_yaml(FIXTURES / "stoplist_name.method.yaml")
        self.assertEqual(
            cm.exception.validation_result.error.code, "non-action-intent"
        )

    def test_compose_from_yaml_missing_outcome_fixture_fails(self):
        with self.assertRaises(CompositionError) as cm:
            compose_from_yaml(FIXTURES / "missing_outcome.method.yaml")
        # Missing outcome fails coherence (composer-side), not validator;
        # validation_result is None in that case.
        self.assertIn("outcome", str(cm.exception).lower())

    def test_compose_from_json_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "x.method.json"
            spec = compose_method("RECONCILE", **_good_compose_kwargs())
            json_path.write_text(
                json.dumps(spec.to_dict(), indent=2), encoding="utf-8"
            )
            reloaded = compose_from_json(json_path)
            self.assertEqual(reloaded.name, spec.name)

    def test_compose_from_json_handles_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.json"
            bad.write_text("{this is not json", encoding="utf-8")
            with self.assertRaises(CompositionError) as cm:
                compose_from_json(bad)
            self.assertIn("invalid json", str(cm.exception).lower())


# ===========================================================================
# Suggestion engine.
# ===========================================================================


class SuggestionEngineTests(unittest.TestCase):

    def _result_for(self, name: str, **overrides) -> Any:
        from client.amg import validate
        from client.amg.grammar import AMGMethodSpec, ParamSpec, SemanticBlock
        spec = AMGMethodSpec(
            name=name,
            semantic_class="action-intent",
            category="transact",
            description="A description that is long enough to clear Pass 6 cleanly.",
            idempotent=False,
            state_modifying=True,
            required_params=[
                ParamSpec(name="x", type="string", description="placeholder"),
            ],
            optional_params=[],
            error_codes=[400, 422],
            source="amg/1.0",
            namespace="t",
            semantic=SemanticBlock(intent="i", actor="agent", outcome="o"),
        )
        # Apply overrides on the spec dict and rebuild.
        return validate(spec)

    def test_lowercase_suggestion_proposes_uppercase(self):
        from client.amg import validate
        from client.amg.grammar import AMGMethodSpec, ParamSpec, SemanticBlock
        spec = AMGMethodSpec(
            name="reconcile",
            semantic_class="action-intent",
            category="transact",
            description="A real description that is plenty long to pass.",
            idempotent=False,
            state_modifying=True,
            required_params=[ParamSpec(name="x", type="string", description="x")],
            optional_params=[],
            error_codes=[400, 422],
            source="amg/1.0",
            namespace="t",
            semantic=SemanticBlock(intent="i", actor="agent", outcome="o"),
        )
        result = validate(spec)
        suggestions = suggest_fix(result, "reconcile")
        self.assertTrue(any("RECONCILE" in s for s in suggestions))

    def test_http_suggestion_proposes_action_verbs(self):
        from client.amg import validate
        from client.amg.grammar import AMGMethodSpec, ParamSpec, SemanticBlock
        spec = AMGMethodSpec(
            name="GET",
            semantic_class="action-intent",
            category="transact",
            description="A real description that is plenty long to pass.",
            idempotent=True,
            state_modifying=False,
            required_params=[ParamSpec(name="x", type="string", description="x")],
            optional_params=[],
            error_codes=[400, 422],
            source="amg/1.0",
            namespace="t",
            semantic=SemanticBlock(intent="i", actor="agent", outcome="o"),
        )
        result = validate(spec)
        suggestions = suggest_fix(result, "GET")
        joined = "\n".join(suggestions).upper()
        self.assertTrue(
            "FETCH" in joined or "RETRIEVE" in joined or "QUERY" in joined,
            f"expected action-verb suggestion, got {suggestions}",
        )

    def test_short_description_suggestion(self):
        from client.amg import validate
        from client.amg.grammar import AMGMethodSpec, ParamSpec, SemanticBlock
        spec = AMGMethodSpec(
            name="RECONCILE",
            semantic_class="action-intent",
            category="transact",
            description="too short",  # under 20 chars
            idempotent=False,
            state_modifying=True,
            required_params=[ParamSpec(name="x", type="string", description="x")],
            optional_params=[],
            error_codes=[400, 422],
            source="amg/1.0",
            namespace="t",
            semantic=SemanticBlock(intent="i", actor="agent", outcome="o"),
        )
        result = validate(spec)
        suggestions = suggest_fix(result, "RECONCILE")
        self.assertTrue(any("expand" in s.lower() or "20" in s for s in suggestions))


# ===========================================================================
# CLI integration.
# ===========================================================================


class ComposeCLITests(unittest.TestCase):

    def _run_cli(self, *args):
        return subprocess.run(
            [PYTHON, "-m", "client.amg.cli", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_compose_from_yaml_fixture_succeeds(self):
        # The known set must include VALIDATE so the substitution
        # target in evaluate.method.yaml resolves.
        with tempfile.TemporaryDirectory() as tmp:
            known = Path(tmp) / "known.json"
            known.write_text(json.dumps(["VALIDATE", "BOOK"]), encoding="utf-8")
            out = self._run_cli(
                "compose",
                "--from", str(FIXTURES / "evaluate.method.yaml"),
                "--known-methods", str(known),
            )
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["name"], "EVALUATE")
        self.assertIn("semantic", payload)

    def test_compose_from_invalid_yaml_fails(self):
        out = self._run_cli(
            "compose",
            "--from", str(FIXTURES / "lowercase_name.method.yaml"),
        )
        self.assertEqual(out.returncode, 1)
        self.assertIn("COMPOSITION FAILED", out.stderr)
        self.assertIn("malformed-name", out.stderr)

    def test_compose_inline_args_succeeds(self):
        out = self._run_cli(
            "compose",
            "--name", "RECONCILE",
            "--intent", "Reconciles transactions for the named account",
            "--actor", "agent",
            "--outcome", "A reconciliation summary is returned",
            "--capability", "transaction",
            "--no-idempotent",
            "--namespace", "acme-finance",
            "--required-param", "account_id:string:the ledger account",
            "--required-param", "period:string:time window",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["name"], "RECONCILE")

    def test_validate_subcommand_still_works(self):
        # Backward-compat: existing 'validate' surface preserved.
        with tempfile.TemporaryDirectory() as tmp:
            spec = compose_method("RECONCILE", **_good_compose_kwargs())
            path = Path(tmp) / "ok.method.json"
            path.write_text(json.dumps(spec.to_dict(), indent=2), encoding="utf-8")
            out = self._run_cli("validate", str(path))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("VALID", out.stdout)

    def test_default_subcommand_falls_through_to_validate(self):
        # The bare positional path (no 'validate' or 'compose' word)
        # should still validate, preserving the original CLI surface.
        with tempfile.TemporaryDirectory() as tmp:
            spec = compose_method("RECONCILE", **_good_compose_kwargs())
            path = Path(tmp) / "ok.method.json"
            path.write_text(json.dumps(spec.to_dict(), indent=2), encoding="utf-8")
            out = self._run_cli(str(path))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("VALID", out.stdout)


if __name__ == "__main__":
    unittest.main()
