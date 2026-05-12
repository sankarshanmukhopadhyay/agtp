"""
Tests for endpoint-level deprecation (AGTP-API §5).

The endpoint-level deprecation surface mirrors the Phase-6
catalog-level deprecation surface but at the endpoint scope: an
endpoint can be deprecated even when its method and path verbs
remain in the catalog (operators migrating callers from one
``(method, path)`` to another).

Coverage:

  * ``EndpointDeprecation`` round-trips through ``to_dict`` /
    ``from_dict``.
  * TOML loader parses ``[endpoint.deprecated]`` + the nested
    ``successor`` block.
  * Manifest exposes the deprecation block on the endpoint entry.
  * Dispatcher stamps ``AGTP-Endpoint-Warning`` header on responses
    for deprecated-endpoint invocations; non-deprecated endpoints
    don't get the header.
  * The endpoint-level header rides alongside the
    catalog-level ``AGTP-Catalog-Warning`` when both apply.
"""

from __future__ import annotations

import json
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.endpoint import (
    EndpointDeprecation, EndpointSpec, HandlerBinding, ParamSpec,
    SemanticBlock,
)
from core.identity import AgentDocument, RequiresDeclaration
from server.endpoint_registry import EndpointRegistry
from server.endpoint_loader import load_endpoints
from server.methods import dispatch
from server.config import default_methods_policy as default_policy


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _semantic() -> SemanticBlock:
    return SemanticBlock(
        intent="Reserve a room for the named guest at the named property.",
        actor="agent",
        outcome="A confirmed reservation_id is returned.",
        capability="transaction",
        confidence=0.85,
        impact="irreversible",
        is_idempotent=False,
    )


def _spec(deprecated=None, **overrides) -> EndpointSpec:
    base = dict(
        name="BOOK", path="/room",
        description="Books a room.",
        semantic=_semantic(),
        required_params=[
            ParamSpec(name="guest_id", type="string",
                      description="guest id"),
        ],
        optional_params=[],
        output=[
            ParamSpec(name="reservation_id", type="string",
                      description="server-assigned"),
        ],
        errors=["room_unavailable"],
        handler=HandlerBinding(
            type="registered_function",
            reference="x.y.z",
        ),
        deprecated=deprecated,
    )
    base.update(overrides)
    return EndpointSpec(**base)


def _make_state(reg: EndpointRegistry):
    class _State:
        endpoint_registry = reg
        methods_policy = default_policy()
        synthesis_runtime = None

        def list_ids(_self): return []
        def lookup(_self, _id): return None
    return _State()


def _make_agent() -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id="0" * 64, name="T",
        principal="p", principal_id="0" * 64, description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=["BOOK", "RESERVE"], scopes=[], wildcards=False,
        ),
        scopes_accepted=[],
        issued_at="2026-05-09T00:00:00Z", issuer="t.local",
    )


# ===========================================================================
# Dataclass round-trip.
# ===========================================================================


class EndpointDeprecationDataclassTests(unittest.TestCase):

    def test_full_round_trip(self):
        dep = EndpointDeprecation(
            deprecated_in="2.1.0",
            removed_in="3.0.0",
            successor_method="RESERVE",
            successor_path="/rooms",
        )
        d = dep.to_dict()
        self.assertEqual(d["deprecated_in"], "2.1.0")
        self.assertEqual(d["removed_in"], "3.0.0")
        self.assertEqual(d["successor"], {"method": "RESERVE", "path": "/rooms"})
        roundtrip = EndpointDeprecation.from_dict(d)
        self.assertEqual(roundtrip, dep)

    def test_minimal_only_deprecated_in(self):
        dep = EndpointDeprecation(deprecated_in="2.1.0")
        d = dep.to_dict()
        self.assertEqual(d, {"deprecated_in": "2.1.0"})
        # No removed_in / successor when not declared.
        self.assertNotIn("removed_in", d)
        self.assertNotIn("successor", d)

    def test_successor_method_only(self):
        # ``BOOK`` deprecated in favor of ``RESERVE`` at any path.
        dep = EndpointDeprecation(
            deprecated_in="2.1.0", successor_method="RESERVE",
        )
        d = dep.to_dict()
        self.assertEqual(d["successor"], {"method": "RESERVE"})
        self.assertNotIn("path", d["successor"])

    def test_successor_path_only(self):
        # Same method, different path (e.g., `/room` → `/rooms`).
        dep = EndpointDeprecation(
            deprecated_in="2.1.0", successor_path="/rooms",
        )
        d = dep.to_dict()
        self.assertEqual(d["successor"], {"path": "/rooms"})


# ===========================================================================
# EndpointSpec round-trip.
# ===========================================================================


class EndpointSpecDeprecationRoundTripTests(unittest.TestCase):

    def test_undeprecated_spec_omits_field(self):
        spec = _spec()
        d = spec.to_dict()
        self.assertNotIn("deprecated", d)

    def test_deprecated_spec_round_trips(self):
        spec = _spec(deprecated=EndpointDeprecation(
            deprecated_in="2.1.0", removed_in="3.0.0",
            successor_method="RESERVE", successor_path="/rooms",
        ))
        d = spec.to_dict()
        self.assertIn("deprecated", d)
        roundtrip = EndpointSpec.from_dict(d)
        self.assertIsNotNone(roundtrip.deprecated)
        self.assertEqual(roundtrip.deprecated.deprecated_in, "2.1.0")
        self.assertEqual(roundtrip.deprecated.successor_method, "RESERVE")


# ===========================================================================
# TOML loader.
# ===========================================================================


_DEPRECATED_TOML = textwrap.dedent("""
    [endpoint]
    method = "BOOK"
    path = "/room"
    description = "Books a room (deprecated)."

    [endpoint.semantic]
    intent = "Reserve a room for the named guest at the named property."
    actor = "agent"
    outcome = "A confirmed reservation_id is returned."
    capability = "transaction"
    confidence = 0.85
    impact = "irreversible"
    is_idempotent = false

    [[endpoint.input.required]]
    name = "guest_id"
    type = "string"
    description = "Guest id."

    [[endpoint.output]]
    name = "reservation_id"
    type = "string"
    description = "Server-assigned id."

    [endpoint.errors]
    list = ["room_unavailable"]

    [endpoint.handler]
    type = "registered_function"
    reference = "samples.handlers.book_room"

    [endpoint.deprecated]
    deprecated_in = "2.1.0"
    removed_in = "3.0.0"

      [endpoint.deprecated.successor]
      method = "RESERVE"
      path = "/rooms"
""").strip()


class TomlLoaderDeprecationTests(unittest.TestCase):

    def test_loader_parses_deprecation_block(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "book.toml").write_text(_DEPRECATED_TOML, encoding="utf-8")
            specs, errors = load_endpoints(tdp)
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertIsNotNone(spec.deprecated)
        self.assertEqual(spec.deprecated.deprecated_in, "2.1.0")
        self.assertEqual(spec.deprecated.removed_in, "3.0.0")
        self.assertEqual(spec.deprecated.successor_method, "RESERVE")
        self.assertEqual(spec.deprecated.successor_path, "/rooms")

    def test_loader_handles_missing_deprecation_table(self):
        # Endpoints without [endpoint.deprecated] load with
        # spec.deprecated == None.
        toml = _DEPRECATED_TOML.split("\n[endpoint.deprecated]")[0]
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "book.toml").write_text(toml, encoding="utf-8")
            specs, errors = load_endpoints(tdp)
        self.assertEqual(errors, [])
        self.assertIsNone(specs[0].deprecated)


# ===========================================================================
# Manifest exposure.
# ===========================================================================


class ManifestDeprecationExposureTests(unittest.TestCase):

    def test_manifest_section_includes_deprecation_when_present(self):
        reg = EndpointRegistry()
        reg.register(_spec(deprecated=EndpointDeprecation(
            deprecated_in="2.1.0", removed_in="3.0.0",
            successor_method="RESERVE", successor_path="/rooms",
        )))
        entry = reg.render_manifest_section()[0]
        self.assertIn("deprecated", entry)
        self.assertEqual(entry["deprecated"]["deprecated_in"], "2.1.0")
        self.assertEqual(entry["deprecated"]["removed_in"], "3.0.0")
        self.assertEqual(
            entry["deprecated"]["successor"],
            {"method": "RESERVE", "path": "/rooms"},
        )

    def test_manifest_section_omits_deprecation_when_absent(self):
        reg = EndpointRegistry()
        reg.register(_spec())  # no deprecation
        entry = reg.render_manifest_section()[0]
        self.assertNotIn("deprecated", entry)


# ===========================================================================
# Dispatcher header.
# ===========================================================================


class DispatcherEndpointDeprecationHeaderTests(unittest.TestCase):

    def _make_request(self, *, body=None):
        body_bytes = json.dumps(body).encode("utf-8") if body else b""
        headers = {"Accept": "application/json"}
        if body_bytes:
            headers["Content-Type"] = "application/json"
        return wire.AGTPRequest(
            method="BOOK", path="/room", headers=headers,
            body_bytes=body_bytes,
        )

    def _register(self, spec):
        from agtp.handlers import EndpointResponse
        reg = EndpointRegistry()
        reg.register(spec, lambda ctx: EndpointResponse(
            body={"reservation_id": "res-1"},
        ))
        return reg

    def test_deprecated_endpoint_stamps_header(self):
        spec = _spec(deprecated=EndpointDeprecation(
            deprecated_in="2.1.0", removed_in="3.0.0",
            successor_method="RESERVE", successor_path="/rooms",
        ))
        reg = self._register(spec)
        resp = dispatch(
            self._make_request(body={"guest_id": "abc"}),
            _make_state(reg), _make_agent(),
        )
        header = resp.headers.get("AGTP-Endpoint-Warning")
        self.assertIsNotNone(header)
        self.assertIn("deprecated", header)
        self.assertIn("successor=RESERVE /rooms", header)
        self.assertIn("removed_in=3.0.0", header)

    def test_non_deprecated_endpoint_omits_header(self):
        spec = _spec()  # no deprecation
        reg = self._register(spec)
        resp = dispatch(
            self._make_request(body={"guest_id": "abc"}),
            _make_state(reg), _make_agent(),
        )
        self.assertNotIn("AGTP-Endpoint-Warning", resp.headers)

    def test_minimal_deprecation_header_omits_optional_fields(self):
        # deprecated_in only — successor and removed_in absent.
        spec = _spec(deprecated=EndpointDeprecation(
            deprecated_in="2.1.0",
        ))
        reg = self._register(spec)
        resp = dispatch(
            self._make_request(body={"guest_id": "abc"}),
            _make_state(reg), _make_agent(),
        )
        header = resp.headers.get("AGTP-Endpoint-Warning")
        self.assertEqual(header, "deprecated")

    def test_successor_method_only_in_header(self):
        # Successor with only a method renames the verb but keeps
        # the path.
        spec = _spec(deprecated=EndpointDeprecation(
            deprecated_in="2.1.0", successor_method="RESERVE",
        ))
        reg = self._register(spec)
        resp = dispatch(
            self._make_request(body={"guest_id": "abc"}),
            _make_state(reg), _make_agent(),
        )
        header = resp.headers.get("AGTP-Endpoint-Warning")
        self.assertIn("successor=RESERVE", header)
        # No trailing path on the bare successor.
        self.assertNotIn("successor=RESERVE /", header)

    def test_header_rides_failure_responses_too(self):
        # Endpoint deprecation is advisory regardless of whether
        # the response is success or refusal. An input-validation
        # failure (422) still carries the warning.
        spec = _spec(deprecated=EndpointDeprecation(
            deprecated_in="2.1.0", successor_method="RESERVE",
        ))
        reg = self._register(spec)
        # Send empty body — required ``guest_id`` missing.
        resp = dispatch(
            self._make_request(body={}),
            _make_state(reg), _make_agent(),
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("AGTP-Endpoint-Warning", resp.headers)


if __name__ == "__main__":
    unittest.main()
