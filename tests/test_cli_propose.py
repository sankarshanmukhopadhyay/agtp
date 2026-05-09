"""
Tests for the interactive PROPOSE CLI flow (``client/cli/propose.py``)
and the new flags it adds to ``client/cli/main.py``.

The flow has three entry shapes:

  * ``--propose --interactive``    walkthrough
  * ``--propose -d '<json>'``      inline body
  * ``--propose --params-file F``  JSON or YAML file

Tests cover:

  * argparse: --propose / --interactive parse correctly,
    mutex with positional method, requirement of one of
    -d / --params-file / --interactive, .yaml/.json extension
    handling on --params-file.
  * Non-interactive: valid -d, malformed JSON, validation
    failure, successful compose.
  * Interactive: mocked stdin scripts walk through the
    prompts, edit-mode preserves defaults, save writes file.
  * Response handling: 200 (synthesis), 422 negotiation-refused,
    422 counter-proposal, counter-acceptance re-issues PROPOSE.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.cli import main as cli_main
from client.cli import propose as cli_propose
from client.amg import AMGMethodSpec, compose_method
from client.core_client import FetchResult


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


FIXTURES = REPO_ROOT / "tests" / "fixtures" / "amg"


def _make_args(**overrides) -> Any:
    """Minimal namespace mirroring argparse output for the propose flow."""
    base = dict(
        uri="agtp://abc123",
        method=None,
        param=None,
        data=None,
        params_file=None,
        propose=True,
        interactive=False,
        json=False,
        yaml=False,
        html=False,
        no_open=False,
        match_check=False,
        negotiate=False,
        auto_accept_counter=False,
        verbose=False,
        registry="https://registry.agtp.io",
        insecure=False,
        insecure_skip_verify=False,
    )
    base.update(overrides)
    ns = type("Args", (), {})()
    for k, v in base.items():
        setattr(ns, k, v)
    return ns


def _ok_result(parsed: Dict[str, Any], status: int = 200) -> FetchResult:
    return FetchResult(
        ok=True,
        kind="method-response",
        status_code=status,
        status_text="OK",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(parsed).encode("utf-8"),
        parsed=parsed,
    )


def _interactive_inputs() -> List[str]:
    """Default valid input script for the interactive walkthrough.

    Object/array params would require a JSON Schema (which the
    walkthrough does not yet prompt for); we use scalar types
    throughout so the composition succeeds at preview time.
    """
    return [
        "EVALUATE",                                      # name
        "Evaluates the input against a declared ruleset",   # intent
        "agent",                                          # actor
        "A structured assessment with pass/fail per rule is returned",  # outcome
        "analysis",                                       # capability
        "0.85",                                           # confidence
        "informational",                                  # impact tier
        "y",                                              # idempotent
        "input:string:The data to evaluate",              # required param 1
        "ruleset:string:Identifier of the ruleset",       # required param 2
        "",                                               # end required
        "",                                               # no optional
        "acme-quality",                                   # namespace
        "",                                               # no substitutes_for
    ]


# ===========================================================================
# Argparse
# ===========================================================================


class ArgparseTests(unittest.TestCase):

    def setUp(self):
        self.parser = cli_main.build_parser()

    def test_propose_alone_parses(self):
        ns = self.parser.parse_args(["agtp://abc", "--propose", "-i"])
        self.assertTrue(ns.propose)
        self.assertTrue(ns.interactive)
        self.assertIsNone(ns.method)

    def test_propose_with_data(self):
        ns = self.parser.parse_args([
            "agtp://abc", "--propose", "-d", '{"name":"X"}',
        ])
        self.assertTrue(ns.propose)
        self.assertEqual(ns.data, '{"name":"X"}')

    def test_propose_with_yaml_params_file(self):
        ns = self.parser.parse_args([
            "agtp://abc", "--propose",
            "--params-file", "fixture.yaml",
        ])
        self.assertTrue(ns.propose)
        self.assertEqual(ns.params_file, Path("fixture.yaml"))

    def test_propose_with_positional_method_is_rejected_at_run(self):
        # Argparse accepts the combo; main.run rejects it.
        ns = self.parser.parse_args([
            "agtp://abc", "QUERY", "--propose", "-i",
        ])
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)

    def test_propose_without_input_source_is_rejected(self):
        ns = self.parser.parse_args(["agtp://abc", "--propose"])
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)

    def test_interactive_without_propose_is_rejected(self):
        ns = self.parser.parse_args(["agtp://abc", "-i"])
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)


# ===========================================================================
# Non-interactive PROPOSE
# ===========================================================================


class NonInteractiveProposeTests(unittest.TestCase):

    def test_inline_data_ok(self):
        body = {
            "name": "EVALUATE",
            "semantic": {
                "intent": "Evaluates the input against a declared ruleset",
                "actor": "agent",
                "outcome": "A structured assessment per rule is returned",
                "capability": "analysis",
                "confidence_guidance": 0.8,
                "impact_tier": "informational",
                "is_idempotent": True,
            },
            "description": "Run a ruleset against the input and report.",
            "category": "transact",
            "required_params": [
                {"name": "input", "type": "object",
                 "description": "data to evaluate",
                 "schema": {"type": "object"}},
                {"name": "ruleset", "type": "string",
                 "description": "ruleset id"},
            ],
            "error_codes": [400, 422],
            "source": "amg/1.0",
            "namespace": "acme-quality",
        }
        args = _make_args(data=json.dumps(body))
        out = io.StringIO()
        with mock.patch.object(
            cli_propose.core_client, "invoke_method",
            return_value=_ok_result({
                "synthesis": {
                    "synthesis_id": "S-123",
                    "target_method": "QUERY",
                    "parameter_mapping": {"input": "q"},
                }
            }),
        ) as mocked:
            rc = cli_propose.run_propose(args, out=out)
        self.assertEqual(rc, 0, msg=out.getvalue())
        self.assertEqual(mocked.call_count, 1)
        self.assertIn("Synthesis", out.getvalue())

    def test_inline_data_invalid_json(self):
        args = _make_args(data="{not json}")
        rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 2)

    def test_inline_data_local_validation_refusal(self):
        # lowercase name fails Pass 1 -> CompositionError before the wire.
        body = {
            "name": "evaluate",
            "semantic": {
                "intent": "Evaluates the input against a declared ruleset",
                "actor": "agent",
                "outcome": "A structured assessment is returned",
            },
            "description": "Run a ruleset against the input and report.",
            "category": "transact",
            "required_params": [
                {"name": "input", "type": "string", "description": "x"},
            ],
            "error_codes": [400, 422],
            "source": "amg/1.0",
            "namespace": "acme-quality",
        }
        args = _make_args(data=json.dumps(body))
        with mock.patch.object(
            cli_propose.core_client, "invoke_method"
        ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 1)
        mocked.assert_not_called()

    def test_params_file_yaml(self):
        args = _make_args(params_file=FIXTURES / "reconcile_propose.method.yaml")
        out = io.StringIO()
        with mock.patch.object(
            cli_propose.core_client, "invoke_method",
            return_value=_ok_result({
                "synthesis": {
                    "synthesis_id": "S-y1",
                    "target_method": "QUERY",
                }
            }),
        ) as mocked:
            rc = cli_propose.run_propose(args, out=out)
        self.assertEqual(rc, 0, msg=out.getvalue())
        self.assertEqual(mocked.call_count, 1)
        # Ensure the body posted contains the AMG semantic block.
        sent_body = mocked.call_args.kwargs["body"]
        self.assertEqual(sent_body["name"], "RECONCILE")
        self.assertIn("semantic", sent_body)

    def test_params_file_missing(self):
        args = _make_args(params_file=Path("/no/such/file.json"))
        rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 2)


# ===========================================================================
# Interactive PROPOSE
# ===========================================================================


class InteractiveProposeTests(unittest.TestCase):

    def test_walkthrough_happy_path_submits(self):
        args = _make_args(interactive=True)
        out = io.StringIO()
        scripted = _interactive_inputs() + ["y"]   # confirm submission
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
                return_value=_ok_result({
                    "synthesis": {
                        "synthesis_id": "S-int",
                        "target_method": "QUERY",
                    }
                }),
            ) as mocked:
            rc = cli_propose.run_propose(args, out=out)
        self.assertEqual(rc, 0, msg=out.getvalue())
        self.assertEqual(mocked.call_count, 1)

    def test_walkthrough_invalid_then_valid_name_retries(self):
        args = _make_args(interactive=True)
        out = io.StringIO()
        scripted = (
            ["evaluate"]                       # invalid (lowercase)
            + _interactive_inputs()
            + ["y"]
        )
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
                return_value=_ok_result({
                    "synthesis": {"synthesis_id": "S-1"},
                }),
            ):
            rc = cli_propose.run_propose(args, out=out)
        self.assertEqual(rc, 0, msg=out.getvalue())
        # The validator's lowercase suggestion should have surfaced.
        self.assertIn("EVALUATE", out.getvalue())

    def test_walkthrough_decline_does_not_submit(self):
        args = _make_args(interactive=True)
        scripted = _interactive_inputs() + ["n"]   # decline
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method"
            ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 1)
        mocked.assert_not_called()

    def test_walkthrough_save_writes_yaml(self):
        args = _make_args(interactive=True)
        with mock.patch.object(
            cli_propose.core_client, "invoke_method"
        ) as mocked:
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "evaluate.method.yaml"
                scripted = (
                    _interactive_inputs()
                    + ["s", str(target)]   # save then path
                )
                with mock.patch("builtins.input", side_effect=scripted):
                    try:
                        import yaml  # noqa: F401
                        rc = cli_propose.run_propose(args, out=io.StringIO())
                    except ImportError:
                        self.skipTest("pyyaml not installed")
                self.assertEqual(rc, 1)   # save returns 1 (no submit)
                self.assertTrue(target.exists())
                text = target.read_text(encoding="utf-8")
                self.assertIn("EVALUATE", text)
                self.assertIn("intent:", text)
        mocked.assert_not_called()

    def test_walkthrough_edit_mode_preserves_defaults(self):
        args = _make_args(interactive=True)
        out = io.StringIO()
        # First pass: full inputs. Then user picks 'e' to edit. Edit
        # round: press Enter (accept defaults) for every prompt.
        scripted = (
            _interactive_inputs()
            + ["e"]                          # edit
            + [""] * 8                       # name, intent, actor, outcome,
                                             # capability, confidence,
                                             # impact_tier, idempotent
            + [""]                           # required params (keep prior)
            + [""]                           # optional params (keep prior)
            + [""]                           # namespace
            + [""]                           # substitutes_for
            + ["y"]                          # confirm
        )
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
                return_value=_ok_result({"synthesis": {"synthesis_id": "S-e"}}),
            ) as mocked:
            rc = cli_propose.run_propose(args, out=out)
        self.assertEqual(rc, 0, msg=out.getvalue())
        sent_body = mocked.call_args.kwargs["body"]
        self.assertEqual(sent_body["name"], "EVALUATE")
        # Required params from first pass survived edit-mode default-keep.
        self.assertEqual(len(sent_body["required_params"]), 2)


# ===========================================================================
# Response rendering
# ===========================================================================


class ResponseRenderTests(unittest.TestCase):

    def _spec(self) -> AMGMethodSpec:
        return compose_method(
            "EVALUATE",
            intent="Evaluates the input against a declared ruleset",
            actor="agent",
            outcome="A structured assessment with pass/fail per rule is returned",
            capability="analysis",
            confidence_guidance=0.85,
            impact_tier="informational",
            is_idempotent=True,
            namespace="acme-quality",
            required_params=[
                {"name": "ruleset", "type": "string",
                 "description": "ruleset id"},
            ],
        )

    def test_200_renders_synthesis(self):
        out = io.StringIO()
        rc = cli_propose._render_propose_response(
            _ok_result({
                "synthesis": {
                    "synthesis_id": "S-7",
                    "target_method": "QUERY",
                    "parameter_mapping": {"input": "q"},
                }
            }, status=200),
            "agtp://abc",
            spec=self._spec(),
            out=out,
        )
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("S-7", text)
        self.assertIn("QUERY", text)
        # Mapping reads "QUERY -> q mapped from 'input'" (target param
        # filled FROM proposal param). Sanity-check both halves appear
        # in the right order on a single line.
        mapping_line = next(
            (ln for ln in text.splitlines() if "mapped from" in ln), ""
        )
        self.assertIn("q", mapping_line)
        self.assertIn("'input'", mapping_line)
        self.assertLess(mapping_line.index("q"), mapping_line.index("'input'"))

    def test_422_counter_renders_differences(self):
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="n"):
            cli_propose._render_propose_response(
                _ok_result({
                    "counter_proposal": {
                        "name": "ASSESS",
                        "description": "ASSESS is canonical",
                        "required_params": [
                            {"name": "ruleset", "type": "string",
                             "description": "ruleset id"},
                        ],
                        "idempotent": True,
                    }
                }, status=422),
                "agtp://abc",
                spec=self._spec(),
                out=out,
            )
        text = out.getvalue()
        self.assertIn("Differences:", text)
        self.assertIn("EVALUATE", text)
        self.assertIn("ASSESS", text)

    def test_422_renders_refusal(self):
        # PROPOSE refusal now rides 422 with error.code='negotiation-refused'.
        out = io.StringIO()
        rc = cli_propose._render_propose_response(
            _ok_result({
                "error": {
                    "code": "negotiation-refused",
                    "reason": "ambiguous",
                    "explanation": "intent overlaps with QUERY",
                }
            }, status=422),
            "agtp://abc",
            spec=self._spec(),
            out=out,
        )
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("ambiguous", text)

    def test_422_counter_accept_reproposes(self):
        # Counter-acceptance issues a second PROPOSE under the
        # suggested name, not a method invocation: the user is
        # composing, so they have a spec but no parameter values.
        out = io.StringIO()
        synth_response = _ok_result(
            {"synthesis": {"synthesis_id": "S-c1", "target_method": "QUERY"}},
            status=200,
        )
        with mock.patch("builtins.input", return_value="y"), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
                return_value=synth_response,
            ) as mocked:
            rc = cli_propose._render_propose_response(
                _ok_result({
                    "counter_proposal": {
                        "name": "ASSESS",
                        "description": "ASSESS is the canonical verb here",
                    }
                }, status=422),
                "agtp://abc",
                spec=self._spec(),
                out=out,
            )
        self.assertEqual(rc, 0, msg=out.getvalue())
        self.assertEqual(mocked.call_count, 1)
        # Second call is a PROPOSE carrying the suggested name in the body.
        self.assertEqual(mocked.call_args.args[1], "PROPOSE")
        self.assertEqual(mocked.call_args.kwargs["body"]["name"], "ASSESS")

    def test_422_counter_decline_returns_1(self):
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="n"), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method"
            ) as mocked:
            rc = cli_propose._render_propose_response(
                _ok_result({
                    "counter_proposal": {"name": "QUERY", "description": "x"}
                }, status=422),
                "agtp://abc",
                spec=self._spec(),
                out=out,
            )
        self.assertEqual(rc, 1)
        mocked.assert_not_called()


# ===========================================================================
# Per-field validators
# ===========================================================================


class FieldValidatorTests(unittest.TestCase):

    def test_check_name_lowercase_suggests_uppercase(self):
        outcome = cli_propose._check_name("evaluate")
        self.assertFalse(outcome.ok)
        self.assertTrue(any("EVALUATE" in s for s in outcome.suggestions))

    def test_check_name_http_method_rejected(self):
        outcome = cli_propose._check_name("GET")
        self.assertFalse(outcome.ok)
        self.assertIn("HTTP", outcome.message or "")

    def test_check_name_stoplist_rejected(self):
        outcome = cli_propose._check_name("STATUS")
        self.assertFalse(outcome.ok)

    def test_check_name_valid_passes(self):
        self.assertTrue(cli_propose._check_name("RECONCILE").ok)

    def test_check_intent_too_short(self):
        outcome = cli_propose._check_intent("short")
        self.assertFalse(outcome.ok)

    def test_check_actor_invalid(self):
        self.assertFalse(cli_propose._check_actor("robot").ok)
        self.assertTrue(cli_propose._check_actor("agent").ok)

    def test_check_confidence_range(self):
        self.assertFalse(cli_propose._check_confidence("1.5").ok)
        self.assertFalse(cli_propose._check_confidence("not a number").ok)
        self.assertTrue(cli_propose._check_confidence("0.7").ok)
        self.assertTrue(cli_propose._check_confidence("").ok)

    def test_check_param_triple(self):
        self.assertFalse(cli_propose._check_param_triple("name").ok)
        self.assertFalse(cli_propose._check_param_triple("Name:string:desc").ok)
        self.assertFalse(cli_propose._check_param_triple("name:bogus:desc").ok)
        self.assertTrue(cli_propose._check_param_triple("name:string:desc").ok)


if __name__ == "__main__":
    unittest.main()
