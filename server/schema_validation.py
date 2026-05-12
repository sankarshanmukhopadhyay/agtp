"""
JSON Schema validation for endpoint inputs and outputs.

The dispatcher runs every incoming request body through the
endpoint's input schema before calling the handler, and every
returned :class:`~agtp.handlers.EndpointResponse` body through the
output schema before sending it to the wire. Phase 2 runs both
checks unconditionally; the output check helps handler bugs surface
during development.

Schema construction
-------------------

An :class:`~core.endpoint.EndpointSpec` carries the input contract
as two lists of :class:`~core.endpoint.ParamSpec` (required and
optional) and the output contract as one list. Each ParamSpec
carries:

  * ``type``    — one of the recognized primitive types.
  * ``schema``  — optional full JSON Schema for ``object`` / ``array``
                  shapes. Drops in verbatim.
  * ``enum``    — optional value-constraint list.
  * ``format``  — optional named format (date, date-time, email,
                  uuid, ...). Mapped to JSON Schema's ``format``
                  keyword.

:func:`spec_to_input_schema` and :func:`spec_to_output_schema`
build a JSON Schema document of type ``object`` whose ``properties``
table reflects the param list. Required-input names go into the
``required`` array. ``additionalProperties`` is ``False`` so callers
catch typo'd inputs early.

Errors
------

:class:`InputValidationError` and :class:`OutputValidationError`
share a common :class:`ValidationError` base. Both carry:

  * ``field``     the JSON pointer to the offending field, e.g.
                  ``"/check_in"``. Empty string for top-level errors.
  * ``message``   the underlying jsonschema reason.
  * ``schema_path`` the path through the schema where the failure
                    fired (helps debug authoring problems).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# jsonschema is a runtime dependency of the server. Bail with a
# helpful message rather than a cryptic ImportError when it's not
# installed in a leaner deployment.
try:
    import jsonschema as _jsonschema
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import (
        ValidationError as _JSONSchemaValidationError,
    )
    _HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover — exercised only on lean installs
    _jsonschema = None  # type: ignore[assignment]
    Draft202012Validator = None  # type: ignore[assignment]
    _JSONSchemaValidationError = Exception  # type: ignore[assignment, misc]
    _HAVE_JSONSCHEMA = False

from core.endpoint import EndpointSpec, ParamSpec


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    """Base class for schema validation failures."""

    def __init__(
        self,
        message: str,
        *,
        field: str = "",
        schema_path: str = "",
    ) -> None:
        super().__init__(message)
        self.field = field
        self.schema_path = schema_path

    def to_dict(self) -> Dict[str, Any]:
        """Wire-friendly rendering used by the 422 response body."""
        return {
            "field": self.field,
            "message": str(self),
            "schema_path": self.schema_path,
        }


class InputValidationError(ValidationError):
    """Raised when the request body fails the input schema."""


class OutputValidationError(ValidationError):
    """Raised when the handler's response body fails the output schema."""


# ---------------------------------------------------------------------------
# Schema construction.
# ---------------------------------------------------------------------------


def _param_to_schema_fragment(param: ParamSpec) -> Dict[str, Any]:
    """Build the per-property fragment for one :class:`ParamSpec`.

    When the ParamSpec carries an explicit ``schema`` field we return
    that verbatim (operators may want full control over nested
    objects). Otherwise we synthesize ``{"type": ...}`` and layer in
    ``enum`` / ``format`` when present.
    """
    if isinstance(param.schema, dict) and param.schema:
        # Explicit schema wins — but layer in enum/format if the
        # ParamSpec also declared them and the inline schema didn't.
        fragment: Dict[str, Any] = dict(param.schema)
        if param.enum is not None and "enum" not in fragment:
            fragment["enum"] = list(param.enum)
        if param.format is not None and "format" not in fragment:
            fragment["format"] = param.format
        if "description" not in fragment and param.description:
            fragment["description"] = param.description
        return fragment
    fragment = {"type": param.type}
    if param.description:
        fragment["description"] = param.description
    if param.enum is not None:
        fragment["enum"] = list(param.enum)
    if param.format is not None:
        fragment["format"] = param.format
    return fragment


def spec_to_input_schema(spec: EndpointSpec) -> Dict[str, Any]:
    """
    Build the JSON Schema document the input body must satisfy.

    Required and optional ParamSpec entries flow into the
    ``properties`` table; required names also go into the
    ``required`` array. ``additionalProperties`` is ``False`` —
    typo'd inputs surface as schema errors rather than silently
    being dropped.
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for p in spec.required_params:
        properties[p.name] = _param_to_schema_fragment(p)
        required.append(p.name)
    for p in spec.optional_params:
        properties[p.name] = _param_to_schema_fragment(p)
    schema: Dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


def spec_to_output_schema(spec: EndpointSpec) -> Dict[str, Any]:
    """
    Build the JSON Schema document the response body must satisfy.

    Output entries are treated like required fields — every name in
    the spec's ``output`` list must appear in the response. This
    matches the prompt's "validate output too, in development" stance.
    Servers that want laxer output enforcement (Phase 3+) can opt
    out at the dispatcher level.
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for p in spec.output:
        properties[p.name] = _param_to_schema_fragment(p)
        required.append(p.name)
    schema: Dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


def _ensure_jsonschema() -> None:
    if not _HAVE_JSONSCHEMA:
        raise RuntimeError(
            "schema validation requires the `jsonschema` package; "
            "install it via `pip install jsonschema` (or `pip install "
            "agtp[server]` once the optional-dependencies group is "
            "published)."
        )


def _format_pointer(absolute_path) -> str:
    """Render the failing field's path as a JSON Pointer."""
    parts = [str(p) for p in absolute_path]
    return "/" + "/".join(parts) if parts else ""


def _convert(
    err: "_JSONSchemaValidationError",
    cls: type,
) -> ValidationError:
    return cls(
        err.message,
        field=_format_pointer(err.absolute_path),
        schema_path="/".join(str(p) for p in err.absolute_schema_path),
    )


def validate_input(
    spec: EndpointSpec,
    body: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Validate ``body`` against ``spec``'s input schema.

    Returns the validated body on success. Raises
    :class:`InputValidationError` on the first failure with the
    field path and message attached. ``body`` of ``None`` is
    treated as an empty dict (empty bodies are common for endpoints
    that take only optional parameters).
    """
    _ensure_jsonschema()
    schema = spec_to_input_schema(spec)
    payload = body if isinstance(body, dict) else {}
    validator = Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    if errors:
        raise _convert(errors[0], InputValidationError)
    return payload


def validate_output(
    spec: EndpointSpec,
    body: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Validate ``body`` against ``spec``'s output schema.

    Phase 2 runs this unconditionally so handler bugs surface
    quickly. Returns the validated body on success; raises
    :class:`OutputValidationError` on the first failure.
    """
    _ensure_jsonschema()
    schema = spec_to_output_schema(spec)
    payload = body if isinstance(body, dict) else {}
    validator = Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    if errors:
        raise _convert(errors[0], OutputValidationError)
    return payload


__all__ = [
    "InputValidationError",
    "OutputValidationError",
    "ValidationError",
    "spec_to_input_schema",
    "spec_to_output_schema",
    "validate_input",
    "validate_output",
]
