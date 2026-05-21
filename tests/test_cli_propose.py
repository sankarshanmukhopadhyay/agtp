"""
Tests for the catalog-aware ``--propose`` flow in
``client/cli/propose.py``.

Covers the endpoint editor:

  * Per-field validators (verb / path / intent / actor / capability
    / param triple).
  * Non-interactive submission (``-d`` and ``--params-file``).
  * Interactive walkthrough (mocked stdin script).
  * Response rendering for 200 / 422 negotiation-refused / 422
    counter-proposal / 459 method-violation.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.cli import main as cli_main
from client.cli import propose as cli_propose
from client.core_client import FetchResult


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_args(**overrides) -> Any:
    base = dict(
        uri="agtp://abc123",
        method=None,
        param=None,
        data=None,
        params_file=None,
        propose=True,
        interactive=False,
        grammar_check=False,
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
    """Default valid input script for the walkthrough."""
    return [
        "RECONCILE",                                       # verb
        "/orders/{order_id}",                              # path
        "Reconciles transactions for the named account",   # intent
        "agent",                                           # actor
        "A reconciliation summary listing matched and unmatched entries is returned",  # outcome
        "analysis",                                        # capability
        "account_id:string:the ledger account",            # required #1
        "period:string:time window like 2026-Q1",          # required #2
        "",                                                # end required
        "tolerance:number:rounding tolerance",             # optional #1
        "",                                                # end optional
        "acme-finance",                                    # namespace
    ]


# ===========================================================================
# Per-field validators
# ===========================================================================


class FieldValidatorTests(unittest.TestCase):

    def test_verb_in_catalog_passes(self):
        self.assertTrue(cli_propose._check_verb("RECONCILE").ok)
        self.assertTrue(cli_propose._check_verb("query").ok)  # case-insensitive

    def test_verb_not_in_catalog_fails_with_suggestions(self):
        out = cli_propose._check_verb("PROPOSEX")
        self.assertFalse(out.ok)
        self.assertIn("PROPOSE", out.suggestions)

    def test_legacy_verb_fails_with_preferred_first(self):
        out = cli_propose._check_verb("GET")
        self.assertFalse(out.ok)
        # find_close_matches surfaces the legacy preferred replacement first.
        self.assertIn("FETCH", out.suggestions)

    def test_path_root_is_valid(self):
        self.assertTrue(cli_propose._check_path("/").ok)

    def test_path_simple_is_valid(self):
        self.assertTrue(cli_propose._check_path("/orders").ok)

    def test_path_with_trailing_slash_fails(self):
        self.assertFalse(cli_propose._check_path("/orders/").ok)

    def test_path_with_verb_token_fails(self):
        self.assertFalse(cli_propose._check_path("/fetch/x").ok)

    def test_path_empty_is_optional(self):
        # Empty path is allowed — paths are optional in the editor.
        self.assertTrue(cli_propose._check_path("").ok)

    def test_intent_too_short_fails(self):
        self.assertFalse(cli_propose._check_intent("short").ok)

    def test_actor_freeform_accepted(self):
        # Per agtp-api §6, ``actor`` is a free-form identifier — only
        # the empty string is rejected. Values outside the suggested
        # vocabulary (``agent`` / ``human`` / ``system`` / etc.) pass.
        self.assertTrue(cli_propose._check_actor("agent").ok)
        self.assertTrue(cli_propose._check_actor("merchant").ok)
        self.assertTrue(cli_propose._check_actor("robot").ok)
        self.assertFalse(cli_propose._check_actor("").ok)
        self.assertFalse(cli_propose._check_actor("   ").ok)

    def test_capability_optional(self):
        self.assertTrue(cli_propose._check_capability("").ok)
        self.assertTrue(cli_propose._check_capability("analysis").ok)
        self.assertFalse(cli_propose._check_capability("bogus").ok)

    def test_param_triple_validates_shape(self):
        self.assertFalse(cli_propose._check_param_triple("name").ok)
        self.assertFalse(cli_propose._check_param_triple("Name:string:desc").ok)
        self.assertFalse(cli_propose._check_param_triple("name:bogus:desc").ok)
        self.assertTrue(cli_propose._check_param_triple("name:string:desc").ok)


# ===========================================================================
# Argparse + dispatch
# ===========================================================================


class ArgparseTests(unittest.TestCase):

    def setUp(self):
        self.parser = cli_main.build_parser()

    def test_propose_alone_parses(self):
        ns = self.parser.parse_args(["agtp://abc", "--propose", "-i"])
        self.assertTrue(ns.propose)
        self.assertTrue(ns.interactive)

    def test_propose_with_data_parses(self):
        ns = self.parser.parse_args([
            "agtp://abc", "--propose", "-d", '{"name":"X"}',
        ])
        self.assertEqual(ns.data, '{"name":"X"}')

    def test_propose_without_input_source_rejected(self):
        ns = self.parser.parse_args(["agtp://abc", "--propose"])
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)

    def test_propose_with_positional_method_rejected(self):
        ns = self.parser.parse_args(["agtp://abc", "QUERY", "--propose", "-i"])
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)


# ===========================================================================
# Non-interactive submission
# ===========================================================================


class NonInteractiveProposeTests(unittest.TestCase):

    def test_inline_data_with_approved_verb_submits(self):
        body = {
            "name": "RECONCILE",
            "parameters": {"account_id": "string"},
            "outcome": "A reconciliation summary is returned",
        }
        args = _make_args(data=json.dumps(body))
        with mock.patch.object(
            cli_propose.core_client, "invoke_method",
            return_value=_ok_result({
                "synthesis": {"synthesis_id": "S-1", "target_method": "QUERY"}
            }),
        ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(mocked.call_args.args[1], "PROPOSE")

    def test_inline_data_with_unknown_verb_refused_locally(self):
        body = {
            "name": "FROBNICATE",
            "parameters": {},
            "outcome": "x",
        }
        args = _make_args(data=json.dumps(body))
        with mock.patch.object(
            cli_propose.core_client, "invoke_method",
        ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 1)
        # The local catalog gate refused before any wire call.
        mocked.assert_not_called()

    def test_inline_data_with_bad_path_refused_locally(self):
        body = {
            "name": "RECONCILE",
            "path": "/fetch/x",  # verb-in-path
            "parameters": {},
            "outcome": "x",
        }
        args = _make_args(data=json.dumps(body))
        with mock.patch.object(
            cli_propose.core_client, "invoke_method",
        ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 1)
        mocked.assert_not_called()

    def test_malformed_inline_json_returns_2(self):
        args = _make_args(data="{not json}")
        rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 2)

    def test_missing_params_file_returns_2(self):
        args = _make_args(params_file=Path("/no/such/file.json"))
        rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 2)


# ===========================================================================
# Interactive walkthrough
# ===========================================================================


class InteractiveProposeTests(unittest.TestCase):

    def test_walkthrough_happy_path_submits(self):
        args = _make_args(interactive=True)
        scripted = _interactive_inputs() + ["y"]   # confirm
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
                return_value=_ok_result({
                    "synthesis": {"synthesis_id": "S-int"}
                }),
            ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 0)
        sent_body = mocked.call_args.kwargs["body"]
        self.assertEqual(sent_body["name"], "RECONCILE")
        self.assertEqual(sent_body["path"], "/orders/{order_id}")
        self.assertIn("semantic", sent_body)
        self.assertEqual(sent_body["semantic"]["actor"], "agent")

    def test_walkthrough_invalid_then_valid_verb_retries(self):
        args = _make_args(interactive=True)
        scripted = (
            ["FROBNICATE"]                  # invalid (not in catalog)
            + _interactive_inputs()
            + ["y"]
        )
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
                return_value=_ok_result({"synthesis": {"synthesis_id": "S-r"}}),
            ):
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 0)

    def test_walkthrough_decline_does_not_submit(self):
        args = _make_args(interactive=True)
        scripted = _interactive_inputs() + ["n"]   # decline
        with mock.patch("builtins.input", side_effect=scripted), \
             mock.patch.object(
                cli_propose.core_client, "invoke_method",
            ) as mocked:
            rc = cli_propose.run_propose(args, out=io.StringIO())
        self.assertEqual(rc, 1)
        mocked.assert_not_called()

    def test_walkthrough_save_writes_yaml(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        args = _make_args(interactive=True)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "reconcile.endpoint.yaml"
            scripted = _interactive_inputs() + ["s", str(target)]
            with mock.patch("builtins.input", side_effect=scripted), \
                 mock.patch.object(
                    cli_propose.core_client, "invoke_method",
                ) as mocked:
                rc = cli_propose.run_propose(args, out=io.StringIO())
            self.assertEqual(rc, 1)  # save → no submit
            self.assertTrue(target.exists())
            text = target.read_text(encoding="utf-8")
            self.assertIn("RECONCILE", text)
            mocked.assert_not_called()


# ===========================================================================
# Response rendering
# ===========================================================================


class ResponseRenderingTests(unittest.TestCase):

    def _draft(self):
        return cli_propose._Draft(
            verb="RECONCILE",
            path="/orders",
            intent="reconciles orders against the ledger",
            actor="agent",
            outcome="A reconciliation summary is returned",
            capability="analysis",
        )

    def test_200_renders_synthesis_id(self):
        out = io.StringIO()
        rc = cli_propose._render_propose_response(
            _ok_result({
                "synthesis": {"synthesis_id": "S-7", "target_method": "QUERY"}
            }, status=200),
            "agtp://abc",
            draft=self._draft(),
            out=out,
        )
        self.assertEqual(rc, 0)
        self.assertIn("S-7", out.getvalue())

    def test_422_negotiation_refused_renders(self):
        out = io.StringIO()
        rc = cli_propose._render_propose_response(
            _ok_result({
                "error": {
                    "code": "negotiation-refused",
                    "reason": "out_of_scope",
                    "explanation": "no close match for ZBLARGON",
                }
            }, status=422),
            "agtp://abc",
            draft=self._draft(),
            out=out,
        )
        self.assertEqual(rc, 1)
        self.assertIn("out_of_scope", out.getvalue())

    def test_422_counter_proposal_accepts_reproposes(self):
        out = io.StringIO()
        synth_response = _ok_result(
            {"synthesis": {"synthesis_id": "S-c", "target_method": "QUERY"}},
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
                        "description": "ASSESS is canonical",
                    }
                }, status=422),
                "agtp://abc",
                draft=self._draft(),
                out=out,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(mocked.call_args.kwargs["body"]["name"], "ASSESS")

    def test_459_renders_suggestions(self):
        out = io.StringIO()
        rc = cli_propose._render_propose_response(
            _ok_result({
                "error": {
                    "code": "method-violation",
                    "method": "FROBNICATE",
                    "message": "'FROBNICATE' is not a recognized AGTP verb.",
                    "suggestions": ["FETCH"],
                }
            }, status=459),
            "agtp://abc",
            draft=self._draft(),
            out=out,
        )
        self.assertEqual(rc, 1)
        self.assertIn("459", out.getvalue())
        self.assertIn("FETCH", out.getvalue())


if __name__ == "__main__":
    unittest.main()
