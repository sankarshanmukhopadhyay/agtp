"""
Tests for the Phase-4 external_service handler.

Three layers:

  1. Pure helper functions (``translate_input``, ``translate_output``,
     ``resolve_headers``) tested in isolation.
  2. The HTTPS-enforcement / structural validation in
     :func:`server.handler_resolution.resolve_external_service` —
     bad scheme, missing method, bad timeout, etc., all surface
     as :class:`InvalidHandlerError` at registration.
  3. The closure handler in action — using a patched
     ``_do_external_request`` to inject synthetic outcomes for
     success, mapped error, unmapped error, transport timeout,
     connection error, malformed JSON, and 401 / 403.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import (
    DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS,
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from server.handler_resolution import (
    InvalidHandlerError,
    _UpstreamRequestOutcome,
    resolve_external_service,
    resolve_headers,
    translate_input,
    translate_output,
)


# ===========================================================================
# Pure translation helpers.
# ===========================================================================


class TranslateInputTests(unittest.TestCase):

    def test_renames_mapped_fields(self):
        out = translate_input(
            {"guest_id": "abc", "check_in": "2026-05-12"},
            {"guest_id": "guestId", "check_in": "checkInDate"},
        )
        self.assertEqual(out, {
            "guestId": "abc", "checkInDate": "2026-05-12",
        })

    def test_passes_unmapped_fields_through(self):
        out = translate_input(
            {"guest_id": "abc", "extra": "passthrough"},
            {"guest_id": "guestId"},
        )
        self.assertEqual(out["guestId"], "abc")
        self.assertEqual(out["extra"], "passthrough")

    def test_empty_map_returns_copy(self):
        original = {"a": 1, "b": 2}
        out = translate_input(original, {})
        self.assertEqual(out, original)
        self.assertIsNot(out, original)  # defensive copy

    def test_non_dict_body_returns_empty(self):
        self.assertEqual(translate_input(None, {"a": "b"}), {})
        self.assertEqual(translate_input("string", {}), {})


class TranslateOutputTests(unittest.TestCase):

    def test_inverts_map_to_rename_http_fields(self):
        # output_map is "AGTP -> HTTP"; the helper inverts at
        # response time so HTTP keys come back as AGTP keys.
        out = translate_output(
            {"id": "res-1", "bookingStatus": "confirmed"},
            {"reservation_id": "id", "status": "bookingStatus"},
        )
        self.assertEqual(out, {
            "reservation_id": "res-1", "status": "confirmed",
        })

    def test_passes_unmapped_http_fields_through(self):
        out = translate_output(
            {"id": "res-1", "createdAt": "2026-05-12"},
            {"reservation_id": "id"},
        )
        self.assertEqual(out["reservation_id"], "res-1")
        # The HTTP-side ``createdAt`` key wasn't mapped, so it rides
        # through verbatim.
        self.assertEqual(out["createdAt"], "2026-05-12")

    def test_non_dict_body_wraps_under_result(self):
        out = translate_output([1, 2, 3], {})
        self.assertEqual(out, {"result": [1, 2, 3]})


class ResolveHeadersTests(unittest.TestCase):

    def test_substitutes_env_var(self):
        headers, missing = resolve_headers(
            {"X-API-Key": "${API_KEY}"},
            environ={"API_KEY": "abc-123"},
        )
        self.assertEqual(headers, {"X-API-Key": "abc-123"})
        self.assertEqual(missing, [])

    def test_missing_var_yields_empty_value(self):
        headers, missing = resolve_headers(
            {"X-API-Key": "${API_KEY}"},
            environ={},  # API_KEY not set
        )
        self.assertEqual(headers, {"X-API-Key": ""})
        self.assertEqual(missing, ["API_KEY"])

    def test_substitutes_within_a_value(self):
        headers, missing = resolve_headers(
            {"Authorization": "Bearer ${TOKEN}"},
            environ={"TOKEN": "deadbeef"},
        )
        self.assertEqual(
            headers, {"Authorization": "Bearer deadbeef"},
        )

    def test_no_substitution_in_static_values(self):
        headers, missing = resolve_headers(
            {"User-Agent": "agtp-server/0.4"},
            environ={},
        )
        self.assertEqual(headers, {"User-Agent": "agtp-server/0.4"})
        self.assertEqual(missing, [])

    def test_collects_distinct_missing_vars(self):
        # When the same variable appears twice in different headers,
        # it's reported once.
        headers, missing = resolve_headers(
            {"X-One": "${VAR}", "X-Two": "static-${VAR}"},
            environ={},
        )
        self.assertEqual(missing, ["VAR"])


# ===========================================================================
# resolve_external_service — registration-time validation.
# ===========================================================================


def _spec(**overrides) -> EndpointSpec:
    base = dict(
        name="FETCH", path="/article/{id}",
        description="Fetch an article from the upstream.",
        semantic=SemanticBlock(
            intent="Retrieve the article identified by the given id.",
            actor="agent",
            outcome="An article record with title and body is returned.",
            capability="retrieval",
            confidence=0.95,
            impact="informational",
            is_idempotent=True,
        ),
        required_params=[
            ParamSpec(name="article_id", type="integer",
                      description="article identifier"),
        ],
        output=[
            ParamSpec(name="title", type="string",
                      description="article title"),
        ],
        errors=[
            "article_not_found",
            "upstream_timeout",
            "upstream_connection_error",
            "upstream_malformed_response",
            "upstream_authentication_failed",
            "upstream_error",
        ],
    )
    base.update(overrides)
    return EndpointSpec(**base)


def _binding(**overrides) -> HandlerBinding:
    base = dict(
        type="external_service",
        reference="https://api.example.com/v1/articles/1",
        method="GET",
        timeout_seconds=10.0,
    )
    base.update(overrides)
    return HandlerBinding(**base)


class RegistrationValidationTests(unittest.TestCase):

    def test_https_required_at_registration(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_external_service(
                _binding(reference="http://api.example.com/v1"),
                spec=_spec(),
            )
        self.assertEqual(
            ctx.exception.detail, "external-service-bad-scheme",
        )

    def test_method_required(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_external_service(
                _binding(method=None),
                spec=_spec(),
            )
        self.assertEqual(
            ctx.exception.detail, "external-service-missing-method",
        )

    def test_empty_reference_refused(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_external_service(
                _binding(reference=""),
                spec=_spec(),
            )
        self.assertEqual(ctx.exception.detail, "empty-reference")

    def test_timeout_must_be_positive(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_external_service(
                _binding(timeout_seconds=0),
                spec=_spec(),
            )
        self.assertEqual(
            ctx.exception.detail, "external-service-bad-timeout",
        )

    def test_wrong_binding_type_refused(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_external_service(
                HandlerBinding(
                    type="registered_function",
                    reference="x.y.z",
                ),
                spec=_spec(),
            )
        self.assertEqual(ctx.exception.detail, "bad-binding-type")

    def test_returns_callable_with_metadata(self):
        handler = resolve_external_service(_binding(), spec=_spec())
        self.assertTrue(callable(handler))
        self.assertEqual(
            handler.__agtp_handler_kind__, "external_service",
        )
        self.assertEqual(
            handler.__agtp_upstream_method__, "GET",
        )


# ===========================================================================
# Closure handler — patched ``_do_external_request``.
# ===========================================================================


def _ctx(input_body: dict = None) -> EndpointContext:
    return EndpointContext(
        input=dict(input_body or {}),
        agent_id="0" * 64,
        method="FETCH",
        path="/article/1",
        headers={},
    )


def _patch_outcome(*, outcome: _UpstreamRequestOutcome):
    """Helper: returns a context manager patching
    _do_external_request to return ``outcome``."""
    return mock.patch(
        "server.handler_resolution._do_external_request",
        return_value=outcome,
    )


class ClosureSuccessTests(unittest.TestCase):

    def test_success_returns_endpoint_response_with_translated_body(self):
        body = {"id": 1, "userId": 7, "title": "T", "body": "B"}
        outcome = _UpstreamRequestOutcome(
            status=200,
            body_bytes=json.dumps(body).encode("utf-8"),
        )
        binding = _binding(output_map={"author_id": "userId"})
        handler = resolve_external_service(binding, spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertIsInstance(result, EndpointResponse)
        # output_map renames userId -> author_id; other keys ride
        # through.
        self.assertEqual(result.body["author_id"], 7)
        self.assertEqual(result.body["id"], 1)
        self.assertEqual(result.status, 200)

    def test_success_passes_status_through_for_201(self):
        outcome = _UpstreamRequestOutcome(
            status=201,
            body_bytes=json.dumps({"id": 99}).encode("utf-8"),
        )
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertEqual(result.status, 201)

    def test_success_with_empty_body_returns_empty_dict(self):
        outcome = _UpstreamRequestOutcome(status=204, body_bytes=b"")
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertIsInstance(result, EndpointResponse)
        # Empty upstream body becomes an empty AGTP body. The
        # endpoint's output schema decides whether that's allowed.
        self.assertEqual(result.body, {})


class ClosureInputTranslationTests(unittest.TestCase):

    def test_input_map_renames_request_body(self):
        captured = {}

        def fake_request(**kwargs):
            captured.update(kwargs)
            return _UpstreamRequestOutcome(
                status=200, body_bytes=b"{}",
            )

        binding = _binding(
            method="POST",
            input_map={"guest_id": "guestId", "check_in": "checkInDate"},
        )
        handler = resolve_external_service(binding, spec=_spec())
        with mock.patch(
            "server.handler_resolution._do_external_request",
            side_effect=fake_request,
        ):
            handler(_ctx({
                "guest_id": "abc",
                "check_in": "2026-05-12",
                "extra": "rides-through",
            }))

        sent_body = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(sent_body["guestId"], "abc")
        self.assertEqual(sent_body["checkInDate"], "2026-05-12")
        self.assertEqual(sent_body["extra"], "rides-through")

    def test_request_carries_resolved_headers(self):
        captured = {}

        def fake_request(**kwargs):
            captured.update(kwargs)
            return _UpstreamRequestOutcome(
                status=200, body_bytes=b"{}",
            )

        binding = _binding(
            headers={
                "X-API-Key": "${TEST_API_KEY}",
                "User-Agent": "agtp-test",
            },
        )
        with mock.patch.dict("os.environ", {"TEST_API_KEY": "secret-123"},
                             clear=False):
            handler = resolve_external_service(binding, spec=_spec())
        with mock.patch(
            "server.handler_resolution._do_external_request",
            side_effect=fake_request,
        ):
            handler(_ctx({"x": 1}))

        # Header substitution happened at registration time; the
        # captured headers carry the resolved value.
        self.assertEqual(captured["headers"]["X-API-Key"], "secret-123")
        self.assertEqual(captured["headers"]["User-Agent"], "agtp-test")
        # JSON Content-Type defaults in when the operator didn't
        # provide one.
        self.assertEqual(
            captured["headers"]["Content-Type"], "application/json",
        )

    def test_missing_env_var_logs_warning_but_resolves_to_empty(self):
        binding = _binding(
            headers={"X-API-Key": "${UNSET_VAR_QFZBM}"},
        )
        with mock.patch.dict("os.environ", clear=True):
            captured_stderr = []
            with mock.patch.object(
                sys, "stderr",
                SimpleNamespace(write=captured_stderr.append),
            ):
                handler = resolve_external_service(binding, spec=_spec())
        self.assertTrue(callable(handler))
        joined = "".join(captured_stderr)
        self.assertIn("missing environment variables", joined)
        self.assertIn("UNSET_VAR_QFZBM", joined)


class ClosureErrorMappingTests(unittest.TestCase):

    def test_mapped_error_uses_configured_code(self):
        outcome = _UpstreamRequestOutcome(
            status=404,
            body_bytes=json.dumps({"detail": "no such article"}).encode("utf-8"),
        )
        binding = _binding(error_map={"404": "article_not_found"})
        handler = resolve_external_service(binding, spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertIsInstance(result, EndpointError)
        self.assertEqual(result.code, "article_not_found")
        self.assertEqual(result.details["upstream_status"], 404)
        self.assertEqual(
            result.details["upstream_body"], {"detail": "no such article"},
        )

    def test_unmapped_4xx_falls_through_to_upstream_error(self):
        outcome = _UpstreamRequestOutcome(status=410, body_bytes=b"{}")
        binding = _binding(error_map={"404": "article_not_found"})
        handler = resolve_external_service(binding, spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertIsInstance(result, EndpointError)
        self.assertEqual(result.code, "upstream_error")

    def test_unmapped_5xx_falls_through_to_upstream_error(self):
        outcome = _UpstreamRequestOutcome(status=503, body_bytes=b"{}")
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertEqual(result.code, "upstream_error")

    def test_401_unmapped_uses_authentication_failed(self):
        outcome = _UpstreamRequestOutcome(status=401, body_bytes=b"{}")
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertEqual(result.code, "upstream_authentication_failed")

    def test_403_unmapped_uses_authentication_failed(self):
        outcome = _UpstreamRequestOutcome(status=403, body_bytes=b"{}")
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertEqual(result.code, "upstream_authentication_failed")

    def test_403_mapped_overrides_authentication_failed(self):
        # When error_map explicitly handles 403, the mapped code wins
        # over the authentication-failed default.
        outcome = _UpstreamRequestOutcome(status=403, body_bytes=b"{}")
        binding = _binding(error_map={"403": "article_not_found"})
        handler = resolve_external_service(binding, spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertEqual(result.code, "article_not_found")


class ClosureTransportFailureTests(unittest.TestCase):

    def test_timeout_returns_upstream_timeout(self):
        outcome = _UpstreamRequestOutcome(
            failure_kind="upstream_timeout", detail="read timed out",
        )
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertIsInstance(result, EndpointError)
        self.assertEqual(result.code, "upstream_timeout")
        self.assertIn("timed out", result.message)

    def test_connection_error_returns_upstream_connection_error(self):
        outcome = _UpstreamRequestOutcome(
            failure_kind="upstream_connection_error",
            detail="Name or service not known",
        )
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertEqual(result.code, "upstream_connection_error")

    def test_malformed_json_response_returns_upstream_malformed_response(self):
        outcome = _UpstreamRequestOutcome(
            status=200, body_bytes=b"<html>not json</html>",
        )
        handler = resolve_external_service(_binding(), spec=_spec())
        with _patch_outcome(outcome=outcome):
            result = handler(_ctx())
        self.assertIsInstance(result, EndpointError)
        self.assertEqual(result.code, "upstream_malformed_response")


# ===========================================================================
# urllib transport — verify _do_external_request distinguishes
# transport failures correctly. (One test per shape, smoke-only.)
# ===========================================================================


class TransportShapeTests(unittest.TestCase):

    def test_timeout_socket_error_classified_as_timeout(self):
        import socket
        import urllib.error
        from server.handler_resolution import _do_external_request

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.timeout("read timed out")),
        ):
            outcome = _do_external_request(
                method="GET", url="https://example.invalid",
                body=b"", headers={}, timeout=0.1,
            )
        self.assertEqual(outcome.failure_kind, "upstream_timeout")

    def test_dns_error_classified_as_connection_error(self):
        import urllib.error
        from server.handler_resolution import _do_external_request

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Name or service not known"),
        ):
            outcome = _do_external_request(
                method="GET", url="https://example.invalid",
                body=b"", headers={}, timeout=10,
            )
        self.assertEqual(outcome.failure_kind, "upstream_connection_error")

    def test_4xx_response_returns_outcome_not_failure(self):
        # urllib raises HTTPError for 4xx/5xx; the helper smooths
        # that into a regular outcome with the status + body so the
        # handler closure can run error_map.
        import urllib.error
        from io import BytesIO
        from server.handler_resolution import _do_external_request

        http_error = urllib.error.HTTPError(
            "https://example.invalid", 404, "Not Found",
            {}, BytesIO(b'{"err": "no"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            outcome = _do_external_request(
                method="GET", url="https://example.invalid",
                body=b"", headers={}, timeout=10,
            )
        self.assertIsNone(outcome.failure_kind)
        self.assertEqual(outcome.status, 404)
        self.assertEqual(outcome.body_bytes, b'{"err": "no"}')


if __name__ == "__main__":
    unittest.main()
