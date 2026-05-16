"""
Drift detection between the frozen public-contract schemas in
``core/schemas/`` and the Python dataclasses they were lifted from.

The contract is defined in ``core/schemas/README.md``: a small set of
shapes (``EndpointContext``, ``EndpointResponse``, ``EndpointError``,
``AgentDocument``, ``ServerManifest``) cross the gateway socket or
ride on the AGTP wire and are therefore versioned. This test fails
the build when those shapes drift from their schemas.

Drift the test treats as a failure:

  * A field present on the dataclass that is missing from the schema's
    ``properties`` block.
  * A field listed in the schema's ``required`` array that is missing
    from the dataclass.
  * A required-vs-optional flip (dataclass field with no default
    must be ``required`` in the schema).

Drift the test deliberately tolerates:

  * Adding an optional field to the dataclass with a safe default —
    that's accretive and forward-compatible. Bump the schema minor
    version in a follow-up; failing CI for it just discourages
    contributors from adding fields cleanly.
  * Schema properties not present on the dataclass — sometimes the
    schema documents wire-only fields (e.g. computed values, legacy
    keys) that no dataclass carries.

If the drift is intentional (a schema bump is in flight), update the
schema file in the same commit. The CI failure is the prompt to
make that explicit.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest


SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "core" / "schemas"


def _load_schema(filename: str) -> Dict[str, Any]:
    return json.loads((SCHEMAS_DIR / filename).read_text(encoding="utf-8"))


def _dataclass_field_names(cls: type) -> Set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _dataclass_required_field_names(cls: type) -> Set[str]:
    """Fields without a default — required at construction time."""
    required: Set[str] = set()
    for f in dataclasses.fields(cls):
        has_default = (
            f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        if not has_default:
            required.add(f.name)
    return required


def _schema_property_names(schema: Dict[str, Any]) -> Set[str]:
    return set((schema.get("properties") or {}).keys())


def _schema_required_names(schema: Dict[str, Any]) -> Set[str]:
    return set(schema.get("required") or [])


def _check_alignment(
    dataclass_cls: type,
    schema: Dict[str, Any],
    *,
    ignore_dataclass_fields: Set[str] = frozenset(),
    ignore_schema_fields: Set[str] = frozenset(),
) -> List[str]:
    """Return a list of human-readable drift messages."""
    dc_fields = _dataclass_field_names(dataclass_cls) - ignore_dataclass_fields
    dc_required = _dataclass_required_field_names(dataclass_cls) - ignore_dataclass_fields
    sc_fields = _schema_property_names(schema) - ignore_schema_fields
    sc_required = _schema_required_names(schema) - ignore_schema_fields

    problems: List[str] = []

    missing_in_schema = dc_fields - sc_fields
    if missing_in_schema:
        problems.append(
            f"dataclass fields not declared in schema: {sorted(missing_in_schema)}"
        )

    required_in_schema_not_dc = sc_required - dc_fields
    if required_in_schema_not_dc:
        problems.append(
            f"schema requires fields the dataclass does not have: "
            f"{sorted(required_in_schema_not_dc)}"
        )

    flipped_to_optional = dc_required - sc_required
    if flipped_to_optional:
        problems.append(
            f"dataclass fields are required (no default) but the schema "
            f"lists them as optional: {sorted(flipped_to_optional)}"
        )

    return problems


# ---------------------------------------------------------------------------
# EndpointContext / EndpointResponse / EndpointError.
# ---------------------------------------------------------------------------


def test_endpoint_context_alignment() -> None:
    from agtp.handlers import EndpointContext
    schema = _load_schema("endpoint-context.schema.json")
    problems = _check_alignment(
        EndpointContext,
        schema,
        # server_state is the daemon's runtime handle — never serialized,
        # not part of the wire contract.
        ignore_dataclass_fields={"server_state"},
    )
    assert not problems, "\n".join(problems)


def test_endpoint_response_alignment() -> None:
    from agtp.handlers import EndpointResponse
    schema = _load_schema("endpoint-response.schema.json")
    problems = _check_alignment(EndpointResponse, schema)
    assert not problems, "\n".join(problems)


def test_endpoint_error_alignment() -> None:
    from agtp.handlers import EndpointError
    schema = _load_schema("endpoint-error.schema.json")
    problems = _check_alignment(EndpointError, schema)
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# AgentDocument.
# ---------------------------------------------------------------------------


def test_agent_document_alignment() -> None:
    from core.identity import AgentDocument
    schema = _load_schema("agent-document.schema.json")
    problems = _check_alignment(AgentDocument, schema)
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# ServerManifest.
# ---------------------------------------------------------------------------


def test_server_manifest_alignment() -> None:
    from core.manifest import ServerManifest
    schema = _load_schema("server-manifest.schema.json")
    problems = _check_alignment(ServerManifest, schema)
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# Sanity: every schema file in core/schemas/ is well-formed JSON Schema.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema_path", sorted(SCHEMAS_DIR.glob("*.schema.json")))
def test_schema_is_valid_jsonschema(schema_path: Path) -> None:
    """Each schema parses as JSON and self-declares a $schema dialect."""
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{schema_path.name} is not a JSON object"
    assert "$schema" in data, f"{schema_path.name} missing $schema declaration"
    assert "$id" in data, f"{schema_path.name} missing $id declaration"
    assert data["$schema"].startswith("https://json-schema.org/draft/"), (
        f"{schema_path.name} uses an unrecognized JSON Schema dialect: "
        f"{data['$schema']!r}"
    )
