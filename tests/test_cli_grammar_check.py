"""
Tests for the ``--grammar-check`` CLI flag (catalog probe).

There is no separate probe header for verb admission;
``--grammar-check`` is a thin sugar layer over a normal invocation.
The catalog gate at the top of the dispatcher does the admission
check, and the CLI renders the response with operator-friendly
framing.

Three layers:

  * Argparse + run-time guards (mutex with --match-check / --negotiate;
    requires a positional method).
  * Local refusal — verbs not in the curated catalog are rejected
    *before* any network call, with close-match suggestions.
  * Response handling — admission codes (200/400/422), catalog refusal
    (459), missing handler (405 method-not-implemented), policy
    refusal (405 method-not-allowed-by-policy), and 403 — rendered
    without making real network calls (``core_client.invoke_method``
    is mocked).
  * Chained PROPOSE — when stdin is interactive AND the server replied
    with 405 method-not-implemented AND the user types ``y``, the
    run delegates to ``run_propose(args)`` with ``interactive=True``.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.cli import main as cli_main
from client.core_client import FetchResult


def _result(parsed: Dict[str, Any], status: int = 200) -> FetchResult:
    return FetchResult(
        ok=True,
        kind="method-response",
        status_code=status,
        status_text="OK" if status == 200 else "Error",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(parsed).encode("utf-8"),
        parsed=parsed,
    )


def _make_args(**overrides) -> Any:
    """Argparse-shaped namespace for the run loop."""
    base = dict(
        uri="agtp://acme.example",
        # RECONCILE is an approved verb in the catalog; tests that
        # need a typo override this explicitly.
        method="RECONCILE",
        param=None,
        data=None,
        params_file=None,
        propose=False,
        interactive=False,
        grammar_check=True,
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


# ===========================================================================
# Argparse + run-time guards.
# ===========================================================================


class ArgparseTests(unittest.TestCase):

    def setUp(self):
        self.parser = cli_main.build_parser()

    def test_grammar_check_parses(self):
        ns = self.parser.parse_args(
            ["agtp://x", "RECONCILE", "--grammar-check"]
        )
        self.assertTrue(ns.grammar_check)
        self.assertEqual(ns.method, "RECONCILE")

    def test_grammar_check_without_method_is_rejected(self):
        ns = self.parser.parse_args(["agtp://x", "--grammar-check"])
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)

    def test_grammar_check_with_match_check_is_rejected(self):
        ns = self.parser.parse_args(
            ["agtp://x", "RECONCILE", "--grammar-check", "--match-check"]
        )
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)

    def test_grammar_check_with_negotiate_is_rejected(self):
        ns = self.parser.parse_args(
            ["agtp://x", "RECONCILE", "--grammar-check", "--negotiate"]
        )
        rc = cli_main.run(ns)
        self.assertEqual(rc, 2)


# ===========================================================================
# Local refusal — unknown verbs are rejected before any network call.
# ===========================================================================


class LocalRefusalTests(unittest.TestCase):

    def test_unknown_verb_refused_locally_with_suggestions(self):
        # FROBNICATE is not in the AGTP catalog. The CLI should
        # refuse before reaching invoke_method.
        with mock.patch.object(
            cli_main.core_client, "invoke_method",
        ) as mocked:
            rc = cli_main.run(_make_args(method="FROBNICATE"))
        self.assertEqual(rc, 1)
        mocked.assert_not_called()

    def test_typo_close_to_known_verb_refused_locally(self):
        # RECNCIL is one edit away from RECONCILE; the local
        # find_close_matches should pick that up without sending a
        # request.
        with mock.patch.object(
            cli_main.core_client, "invoke_method",
        ) as mocked:
            rc = cli_main.run(_make_args(method="RECNCIL"))
        self.assertEqual(rc, 1)
        mocked.assert_not_called()


# ===========================================================================
# Server-response rendering — bridge mocked.
# ===========================================================================


class ResponseRenderingTests(unittest.TestCase):

    def test_200_admitted_returns_zero(self):
        # The probe reached a handler that returned a 200 — the verb
        # is fully wired on the server.
        resp = _result(
            {"method": "RECONCILE", "ok": True}, status=200,
        )
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ):
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 0)

    def test_400_missing_params_treated_as_admitted(self):
        # The dispatcher's catalog / path / policy gates all passed;
        # the handler refused the empty body with 400 missing-params.
        # That's proof the verb is admissible.
        resp = _result({
            "error": {
                "code": "missing-required-params",
                "explanation": "param 'account_id' is required",
            },
        }, status=400)
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ):
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 0)

    def test_459_catalog_violation_with_suggestions(self):
        # Dispatcher refused at the catalog gate. The suggestions
        # list comes from core.methods.find_close_matches.
        resp = _result({
            "error": {
                "code": "method-violation",
                "method": "FROBNICATE",
                "explanation": "not in catalog",
                "suggestions": ["FROBOZIFY"],
            },
        }, status=459)
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ):
            # Bypass the local-refusal path by patching is_approved_verb
            # (this exercises the rare case where the local catalog
            # disagrees with the server).
            with mock.patch(
                "client.cli.main.is_approved_verb", return_value=True,
            ):
                rc = cli_main.run(_make_args(method="RECONCILE"))
        self.assertEqual(rc, 1)

    def test_405_method_not_implemented_returns_zero(self):
        # Catalog admits, server has no handler. No PROPOSE chain is
        # offered when stdin is non-interactive; rc 0 because the
        # probe answered the operator's question.
        resp = _result({
            "error": {
                "code": "method-not-implemented",
                "explanation": "no handler",
            },
        }, status=405)
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ), mock.patch("sys.stdin.isatty", return_value=False):
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 0)

    def test_405_policy_refusal_returns_one(self):
        # policies.methods actively disallows the verb. No PROPOSE chain.
        resp = _result({
            "error": {
                "code": "method-not-allowed-by-policy",
                "explanation": "policy refuses",
            },
        }, status=405)
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ):
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 1)

    def test_403_forbidden_returns_one(self):
        resp = _result({
            "error": {
                "code": "method-not-permitted-for-agent",
                "explanation": "agent lacks capability",
            },
        }, status=403)
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ):
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 1)

    def test_unknown_status_falls_through(self):
        resp = _result({"surprise": True}, status=418)
        with mock.patch.object(
            cli_main.core_client, "invoke_method", return_value=resp,
        ):
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 1)


# ===========================================================================
# Chained PROPOSE on interactive TTY (405 method-not-implemented).
# ===========================================================================


class ChainedProposeTests(unittest.TestCase):

    def _not_implemented(self) -> FetchResult:
        return _result({
            "error": {
                "code": "method-not-implemented",
                "explanation": "no handler",
            },
        }, status=405)

    def test_y_chains_into_run_propose(self):
        with mock.patch.object(
            cli_main.core_client, "invoke_method",
            return_value=self._not_implemented(),
        ), mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch("client.cli.propose.run_propose", return_value=0) as mocked_propose:
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 0)
        mocked_propose.assert_called_once()
        invoked_args = mocked_propose.call_args.args[0]
        # The args passed to run_propose got mutated to the
        # interactive-PROPOSE shape.
        self.assertTrue(invoked_args.propose)
        self.assertTrue(invoked_args.interactive)

    def test_n_skips_chain(self):
        with mock.patch.object(
            cli_main.core_client, "invoke_method",
            return_value=self._not_implemented(),
        ), mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch("client.cli.propose.run_propose") as mocked_propose:
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 0)
        mocked_propose.assert_not_called()

    def test_no_tty_does_not_prompt(self):
        # Non-interactive stdin (piped, scripted) — render and exit
        # without prompting.
        with mock.patch.object(
            cli_main.core_client, "invoke_method",
            return_value=self._not_implemented(),
        ), mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("builtins.input") as mocked_input:
            rc = cli_main.run(_make_args())
        self.assertEqual(rc, 0)
        mocked_input.assert_not_called()


if __name__ == "__main__":
    unittest.main()
