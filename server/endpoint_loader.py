"""
TOML loader for endpoint declarations.

Reads ``*.toml`` files from a directory, parses each, attempts to
construct an :class:`~core.endpoint.EndpointSpec`. Returns parsed
specs and structured load errors; the caller (typically
``server.main`` at startup) decides what to do with them — usually
register the valid ones, log the errors, fail-fast on the first
validation problem, etc.

This module does NOT register endpoints into
:class:`~server.endpoint_registry.EndpointRegistry` directly. The
loader is deliberately one-step removed so:

  * Tests can exercise the parsing layer without spinning up a
    registry.
  * Tooling (linters, schema validators, IDE plugins) can re-use
    the loader without depending on the registry's lock.
  * A future hot-reload path can diff the new file set against the
    current registry before applying changes.

TOML schema
-----------

See :doc:`docs/endpoint-toml.md` for the canonical reference. The
short form::

    [endpoint]
    method = "BOOK"
    path = "/room"
    description = "..."
    namespace = "reservations"      # optional

    [endpoint.semantic]
    intent = "..."
    actor = "agent"
    outcome = "..."
    capability = "transaction"
    confidence = 0.85
    impact = "irreversible"
    is_idempotent = false

    [[endpoint.input.required]]
    name = "guest_id"
    type = "string"
    description = "..."
    format = "uuid"                 # optional
    enum = ["…"]                    # optional

    [[endpoint.input.optional]]
    name = "special_requests"
    type = "string"
    description = "..."

    [[endpoint.output]]
    name = "reservation_id"
    type = "string"
    description = "..."

    [endpoint.errors]
    list = ["room_unavailable", "invalid_dates"]

    [endpoint.handler]
    type = "registered_function"
    reference = "staybeta.handlers.book_room"

For complex types where a JSON Schema is needed (nested objects,
arrays of objects), an entry may carry a ``schema`` field whose
value is the full JSON Schema as an inline TOML table.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# tomllib is stdlib in 3.11+; fall back to tomli when running on
# older interpreters (the rest of the code base sets the minimum
# at 3.10 for elemen, so the fallback path is used in CI for older
# Pythons).
try:
    import tomllib as _toml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only on 3.10
    import tomli as _toml  # type: ignore[import-not-found, no-redef]

from core.endpoint import (
    DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS,
    EndpointDeprecation,
    EndpointSpec,
    HandlerBinding,
    ParamSpec,
    SemanticBlock,
)
from server.endpoint_registry import (
    InvalidEndpointError,
    _validate_spec,  # used by tests; re-exported for callers that
                     # want pre-flight validation without a registry
)


# ---------------------------------------------------------------------------
# LoadError.
# ---------------------------------------------------------------------------


@dataclass
class LoadError:
    """
    Structured error describing why one TOML file failed to load.

    Fields:

      * ``file_path``   absolute path to the offending file.
      * ``error_type``  ``"parse"`` (TOML couldn't parse),
                        ``"validation"`` (parsed but the constructed
                        spec failed a registry rule), or ``"io"``
                        (couldn't open / read the file at all).
      * ``message``     operator-facing explanation.
      * ``detail``      stable tag for ``"validation"`` errors,
                        carried through from
                        :class:`InvalidEndpointError.detail`. ``None``
                        for parse / io errors.
      * ``spec``        the partially-constructed ``EndpointSpec``
                        when parsing got far enough to build one,
                        otherwise ``None``. Useful for surfacing
                        what the operator wrote in error messages.
    """

    file_path: str
    error_type: str
    message: str
    detail: Optional[str] = None
    spec: Optional[EndpointSpec] = None


# ---------------------------------------------------------------------------
# Parsing helpers.
# ---------------------------------------------------------------------------


def _parse_param(entry: Any) -> ParamSpec:
    """Construct a :class:`ParamSpec` from one TOML table. The
    registry's downstream validator catches missing fields and bad
    types; here we only ferry the data into the dataclass."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"expected a TOML table for the param entry, got "
            f"{type(entry).__name__}"
        )
    return ParamSpec(
        name=str(entry.get("name", "")),
        type=str(entry.get("type", "string")),
        description=str(entry.get("description", "")),
        schema=(
            dict(entry["schema"])
            if isinstance(entry.get("schema"), dict)
            else None
        ),
        enum=(
            list(entry["enum"])
            if entry.get("enum") is not None
            else None
        ),
        format=(
            str(entry["format"])
            if entry.get("format")
            else None
        ),
    )


def _parse_semantic(table: Any) -> Optional[SemanticBlock]:
    """Construct a :class:`SemanticBlock` from the
    ``[endpoint.semantic]`` table. Missing tables return ``None``;
    the registry's validator surfaces that as
    ``semantic-missing``."""
    if not isinstance(table, dict):
        return None
    # Back-compat: pre-§4 TOMLs used ``confidence_guidance`` /
    # ``impact_tier``. Accept either key so existing endpoint files
    # keep loading; new TOMLs should use the renamed fields.
    confidence_raw = table.get("confidence")
    if confidence_raw is None:
        confidence_raw = table.get("confidence_guidance")
    impact_raw = table.get("impact")
    if impact_raw is None:
        impact_raw = table.get("impact_tier")
    return SemanticBlock(
        intent=str(table.get("intent", "")),
        actor=str(table.get("actor", "")),
        outcome=str(table.get("outcome", "")),
        capability=table.get("capability"),
        confidence=(
            float(confidence_raw) if confidence_raw is not None else None
        ),
        impact=impact_raw,
        is_idempotent=(
            bool(table["is_idempotent"])
            if table.get("is_idempotent") is not None
            else None
        ),
    )


#: Pre-§9 TOML key names mapped to their §9 replacements. The loader
#: accepts both with a deprecation warning when the legacy form is
#: used.
_LEGACY_HANDLER_KEY_RENAMES = {
    "reference": ("function | recipe | url (per binding type)", "reference"),
    "input_map": ("input_transform", "input_map"),
    "output_map": ("output_transform", "output_map"),
}


def _warn_deprecated_handler_field(
    legacy_key: str,
    canonical_key: str,
    *,
    source: Optional[str] = None,
) -> None:
    """Emit a deprecation warning when a pre-§9 TOML key is read.

    ``source`` is the file path of the offending endpoint TOML so
    operators can locate it; ``None`` is acceptable for ad-hoc
    fixtures (the warning still fires).
    """
    import warnings
    where = f"endpoint {source}: " if source else ""
    msg = (
        f"{where}handler.{legacy_key} is deprecated, use "
        f"handler.{canonical_key} instead"
    )
    warnings.warn(msg, DeprecationWarning, stacklevel=3)
    print(f"[server] Warning: {msg}", file=sys.stderr)


def _parse_handler(
    table: Any,
    *,
    source: Optional[str] = None,
) -> Optional[HandlerBinding]:
    """Construct a :class:`HandlerBinding` from the
    ``[endpoint.handler]`` table.

    §9 type-specific reference fields (the loader prefers the new
    name and falls back to the legacy ``reference`` with a
    deprecation warning):

      * ``function``  — registered_function: Python dotted path.
      * ``recipe``    — composition: recipe name.
      * ``url``       — external_service: HTTPS URL.

    For ``external_service`` bindings the loader also surfaces:

      * ``method``            — upstream HTTP verb (uppercased).
      * ``headers``           — table; values rendered as strings.
      * ``input_transform``   — table mapping AGTP→HTTP field names
                                (legacy: ``input_map``).
      * ``output_transform``  — table mapping AGTP→HTTP field names
                                (legacy: ``output_map``).
      * ``error_map``         — table mapping HTTP status code →
                                AGTP error code. Keys come back as
                                strings even when authored as TOML
                                integers.
      * ``timeout_seconds``   — number; defaults to
                                ``DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS``.
    """
    if not isinstance(table, dict):
        return None
    binding_type = str(table.get("type", ""))

    # §9 type-specific reference field with pre-§9 fallback.
    function = table.get("function")
    recipe = table.get("recipe")
    url = table.get("url")
    legacy_reference = table.get("reference")
    if legacy_reference is not None:
        if binding_type == "registered_function":
            _warn_deprecated_handler_field("reference", "function", source=source)
            if function is None:
                function = legacy_reference
        elif binding_type == "composition":
            _warn_deprecated_handler_field("reference", "recipe", source=source)
            if recipe is None:
                recipe = legacy_reference
        elif binding_type == "external_service":
            _warn_deprecated_handler_field("reference", "url", source=source)
            if url is None:
                url = legacy_reference

    # Phase-2/3 fields suffice for non-external bindings — keep the
    # construction terse so registered_function / composition TOMLs
    # don't accidentally pick up empty external-service fields.
    if binding_type != "external_service":
        return HandlerBinding(
            type=binding_type,
            function=str(function) if function else None,
            recipe=str(recipe) if recipe else None,
        )

    # external_service bindings carry the extra translation +
    # transport fields. Tables and maps default to empty so the
    # registry validator can complain about missing required fields
    # (rather than the loader failing with a KeyError).
    headers_raw = table.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ValueError(
            "[endpoint.handler.headers] must be a table of string values"
        )

    # input_transform / output_transform with pre-§9 fallback.
    input_transform_raw = table.get("input_transform")
    if input_transform_raw is None and "input_map" in table:
        _warn_deprecated_handler_field(
            "input_map", "input_transform", source=source,
        )
        input_transform_raw = table.get("input_map")
    output_transform_raw = table.get("output_transform")
    if output_transform_raw is None and "output_map" in table:
        _warn_deprecated_handler_field(
            "output_map", "output_transform", source=source,
        )
        output_transform_raw = table.get("output_map")

    input_transform_raw = input_transform_raw or {}
    output_transform_raw = output_transform_raw or {}
    error_map_raw = table.get("error_map") or {}
    if not isinstance(input_transform_raw, dict):
        raise ValueError(
            "[endpoint.handler.input_transform] must be a table"
        )
    if not isinstance(output_transform_raw, dict):
        raise ValueError(
            "[endpoint.handler.output_transform] must be a table"
        )
    if not isinstance(error_map_raw, dict):
        raise ValueError("[endpoint.handler.error_map] must be a table")

    timeout_raw = table.get("timeout_seconds")
    timeout = (
        float(timeout_raw)
        if timeout_raw is not None
        else DEFAULT_EXTERNAL_SERVICE_TIMEOUT_SECONDS
    )

    method_raw = table.get("method")
    return HandlerBinding(
        type=binding_type,
        url=str(url) if url else None,
        method=str(method_raw).upper() if method_raw else None,
        headers={str(k): str(v) for k, v in headers_raw.items()},
        input_transform={
            str(k): str(v) for k, v in input_transform_raw.items()
        },
        output_transform={
            str(k): str(v) for k, v in output_transform_raw.items()
        },
        error_map={str(k): str(v) for k, v in error_map_raw.items()},
        timeout_seconds=timeout,
    )


def _spec_from_toml(
    doc: Dict[str, Any], *, source: Optional[str] = None,
) -> EndpointSpec:
    """Build an :class:`EndpointSpec` from the parsed TOML document.

    Raises ``ValueError`` (caught by the loader and turned into a
    parse-shaped :class:`LoadError`) when the document doesn't have
    the expected top-level structure. Rich validation against the
    catalog / path grammar / semantic-block contract happens later
    in :func:`server.endpoint_registry._validate_spec`.

    ``source`` is the file path being loaded; used by §9 deprecation
    warnings (handler.reference → handler.function/recipe/url) so
    operators can locate the offending TOML.
    """
    endpoint = doc.get("endpoint")
    if not isinstance(endpoint, dict):
        raise ValueError("missing top-level [endpoint] table")

    method = str(endpoint.get("method", "")).upper()
    path = endpoint.get("path")
    description = str(endpoint.get("description", ""))
    namespace = endpoint.get("namespace")

    semantic = _parse_semantic(endpoint.get("semantic"))

    # Inputs nest two arrays: [[endpoint.input.required]] and
    # [[endpoint.input.optional]]. Both are optional; the registry's
    # validator checks they're lists.
    input_block = endpoint.get("input") or {}
    required_raw = input_block.get("required") or []
    optional_raw = input_block.get("optional") or []
    required = [_parse_param(p) for p in required_raw]
    optional = [_parse_param(p) for p in optional_raw]

    output_raw = endpoint.get("output") or []
    output = [_parse_param(p) for p in output_raw]

    # Errors are declared as [endpoint.errors] with a `list` array.
    # We accept the bare list form too (an array directly under
    # ``errors``) so authors who expect that idiom aren't surprised.
    errors_raw = endpoint.get("errors") or []
    if isinstance(errors_raw, dict):
        errors_raw = errors_raw.get("list") or []
    errors = [str(e) for e in errors_raw]

    handler = _parse_handler(endpoint.get("handler"), source=source)

    # required_scopes is a flat array of strings: ``required_scopes =
    # ["bookings:write"]``. Optional; the registry treats an empty
    # list as "no scope check".
    required_scopes_raw = endpoint.get("required_scopes") or []
    required_scopes = [str(s) for s in required_scopes_raw]

    # Endpoint-level deprecation. Mirrors the catalog's per-verb
    # deprecation metadata. TOML form::
    #
    #     [endpoint.deprecated]
    #     deprecated_in = "2.1.0"
    #     removed_in = "3.0.0"
    #
    #       [endpoint.deprecated.successor]
    #       method = "RESERVE"
    #       path = "/rooms"
    deprecated = _parse_deprecation(endpoint.get("deprecated"))

    return EndpointSpec(
        name=method,
        path=str(path) if path is not None else None,
        description=description,
        required_params=required,
        optional_params=optional,
        output=output,
        errors=errors,
        semantic=semantic,
        handler=handler,
        required_scopes=required_scopes,
        deprecated=deprecated,
        namespace=str(namespace) if namespace else None,
    )


def _parse_deprecation(table: Any) -> Optional[EndpointDeprecation]:
    """Construct an :class:`EndpointDeprecation` from the
    ``[endpoint.deprecated]`` table. Missing tables or tables
    without ``deprecated_in`` return ``None`` so undeclared
    endpoints stay untouched."""
    if not isinstance(table, dict):
        return None
    if not table.get("deprecated_in"):
        return None
    successor = table.get("successor") or {}
    if not isinstance(successor, dict):
        successor = {}
    return EndpointDeprecation(
        deprecated_in=str(table["deprecated_in"]),
        removed_in=(
            str(table["removed_in"]) if table.get("removed_in") else None
        ),
        successor_method=(
            str(successor["method"]).upper()
            if successor.get("method") else None
        ),
        successor_path=(
            str(successor["path"]) if successor.get("path") else None
        ),
    )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def load_endpoints(
    directory: Any,
) -> Tuple[List[EndpointSpec], List[LoadError]]:
    """
    Scan ``directory`` for ``*.toml`` files and parse each.

    Returns ``(specs, errors)``:

      * ``specs``  — every spec that parsed cleanly AND passed the
                     registry-side validator. The caller can hand
                     these straight to ``EndpointRegistry.register``
                     without re-validating.
      * ``errors`` — every :class:`LoadError` encountered, including
                     parse failures, validation failures, and any
                     IO failure on ``directory`` itself.

    The function is deliberately forgiving: a malformed file in the
    directory does not crash the loader, it produces a single
    ``LoadError`` and the loader keeps going. Files are processed in
    sorted order so the order of valid specs is stable across
    platforms.

    A nonexistent or non-directory path returns ``([], [<io error>])``.
    """
    path = Path(directory)
    specs: List[EndpointSpec] = []
    errors: List[LoadError] = []

    if not path.exists():
        errors.append(LoadError(
            file_path=str(path),
            error_type="io",
            message=f"directory {str(path)!r} does not exist",
        ))
        return specs, errors
    if not path.is_dir():
        errors.append(LoadError(
            file_path=str(path),
            error_type="io",
            message=f"{str(path)!r} is not a directory",
        ))
        return specs, errors

    toml_files = sorted(path.glob("*.toml"))
    for fp in toml_files:
        try:
            with fp.open("rb") as fh:
                doc = _toml.load(fh)
        except _toml.TOMLDecodeError as exc:
            errors.append(LoadError(
                file_path=str(fp),
                error_type="parse",
                message=f"TOML parse error: {exc}",
            ))
            continue
        except OSError as exc:
            errors.append(LoadError(
                file_path=str(fp),
                error_type="io",
                message=f"cannot read file: {exc}",
            ))
            continue

        try:
            spec = _spec_from_toml(doc, source=str(fp))
        except ValueError as exc:
            errors.append(LoadError(
                file_path=str(fp),
                error_type="parse",
                message=str(exc),
            ))
            continue

        try:
            _validate_spec(spec)
        except InvalidEndpointError as exc:
            errors.append(LoadError(
                file_path=str(fp),
                error_type="validation",
                message=str(exc),
                detail=exc.detail,
                spec=spec,
            ))
            continue

        specs.append(spec)

    return specs, errors


__all__ = [
    "LoadError",
    "load_endpoints",
]
