"""
Endpoint registry: the in-memory store of (method, path) -> (spec, handler).

The registry is populated at server startup from the TOML files
:mod:`server.endpoint_loader` parses, then frozen for the request
hot path. Mutations are guarded by a lock so a future hot-reload
mode can extend the data layer without revisiting concurrency.

Phase 1 wires the data layer only:

  * ``EndpointRegistry.register(spec, handler)`` validates a spec
    against the AGTP catalog (:func:`core.methods.is_approved_verb`)
    and the path grammar (:func:`core.path_grammar.validate_path`),
    plus the structural rules below.
  * ``lookup(method, path)`` answers the dispatcher's eventual
    "is this (method, path) bound?" question.
  * ``methods_for_path(path)`` powers the eventual 405 response
    that lists which verbs ARE accepted at a given path.
  * ``all_endpoints()`` and ``count()`` give telemetry / introspection
    a stable surface.
  * ``render_manifest_section()`` produces the JSON-ready list the
    server manifest will surface under its ``endpoints`` key. The
    manifest integration itself lands in Phase 2.

Validation rules at register time:

  1. ``spec.name`` (the AGTP verb) is in the curated catalog.
  2. ``spec.path`` is non-empty and passes ``validate_path``.
  3. ``spec.semantic`` has all seven required fields populated:
     ``intent``, ``actor``, ``outcome``, ``capability``,
     ``confidence``, ``impact``, ``is_idempotent``.
  4. ``spec.required_params`` and ``spec.optional_params`` are lists
     of :class:`~core.endpoint.ParamSpec`; each entry has a
     non-empty ``name``, a recognized ``type``, and a non-empty
     ``description``.
  5. ``spec.output`` is a list (may be empty); same per-entry rules
     as inputs.
  6. ``spec.errors`` is a list of strings (may be empty for
     endpoints that declare no error conditions).
  7. ``spec.handler`` is non-None;
     ``handler.type`` is one of the recognized binding kinds; and
     ``handler.function`` / ``handler.recipe`` / ``handler.url``
     (per §9 type-specific reference field) is a non-empty string.

Each rule failure raises an :class:`InvalidEndpointError` whose
``detail`` field carries a structured tag (e.g.
``"verb-not-in-catalog"``, ``"semantic-missing-field:capability"``)
so callers can branch programmatically rather than parsing text.

Duplicate ``(method, path)`` registration raises
:class:`DuplicateEndpointError`.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.endpoint import (
    ALL_CAPABILITIES,
    ALL_HANDLER_TYPES,
    ALL_IMPACTS,
    ALL_PARAM_TYPES,
    EndpointSpec,
    HandlerBinding,
    ParamSpec,
    SemanticBlock,
)
from core.methods import is_approved_verb
from core.path_grammar import PathGrammarError, validate_path


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class EndpointRegistryError(Exception):
    """Base class for registry-side refusals."""


class InvalidEndpointError(EndpointRegistryError):
    """
    Raised when an endpoint spec fails the registry's validation
    rules. The ``detail`` field is a stable tag suitable for tests
    and programmatic branching; the exception message is the
    operator-facing rendering.

    Stable detail tags:

      * ``verb-not-in-catalog``
      * ``path-missing``
      * ``path-grammar:<code>`` (e.g. ``path-grammar:verb-in-path``)
      * ``semantic-missing``
      * ``semantic-missing-field:<field>``
      * ``semantic-bad-actor``
      * ``semantic-bad-capability``
      * ``semantic-bad-impact``
      * ``semantic-bad-confidence``
      * ``param-bad-shape:<which>:<index>:<reason>``
      * ``output-bad-shape:<index>:<reason>``
      * ``errors-bad-shape:<index>``
      * ``handler-missing``
      * ``handler-bad-type``
      * ``handler-empty-reference``
      * ``required-scopes-bad-shape``
      * ``external-service-missing-method``
      * ``external-service-bad-scheme``
      * ``external-service-bad-method``
      * ``external-service-bad-timeout``
      * ``external-service-error-map-undeclared:<code>``
    """

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


class DuplicateEndpointError(EndpointRegistryError):
    """Raised when ``(method, path)`` is already registered."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(
            f"endpoint ({method!r}, {path!r}) is already registered"
        )
        self.method = method
        self.path = path


# ---------------------------------------------------------------------------
# Validation helpers (module-private).
# ---------------------------------------------------------------------------


def _validate_param(
    param: Any, *, kind: str, index: int,
) -> None:
    """Validate one :class:`ParamSpec` entry. ``kind`` is one of
    ``"input.required"`` / ``"input.optional"`` / ``"output"`` —
    used to namespace the detail tag in any raised error."""
    if not isinstance(param, ParamSpec):
        raise InvalidEndpointError(
            f"{kind} entry at index {index} is not a ParamSpec",
            detail=f"param-bad-shape:{kind}:{index}:not-paramspec",
        )
    if not param.name or not isinstance(param.name, str):
        raise InvalidEndpointError(
            f"{kind} entry at index {index} has empty 'name'",
            detail=f"param-bad-shape:{kind}:{index}:empty-name",
        )
    if param.type not in ALL_PARAM_TYPES:
        raise InvalidEndpointError(
            f"{kind} entry {param.name!r} has unrecognized type "
            f"{param.type!r}; expected one of "
            f"{', '.join(sorted(ALL_PARAM_TYPES))}",
            detail=f"param-bad-shape:{kind}:{index}:bad-type",
        )
    if not param.description or not isinstance(param.description, str):
        raise InvalidEndpointError(
            f"{kind} entry {param.name!r} has empty 'description'",
            detail=f"param-bad-shape:{kind}:{index}:empty-description",
        )


def _validate_semantic(semantic: Optional[SemanticBlock]) -> None:
    """Validate the semantic block. Every required field must be
    populated — that's the contract the manifest renderer leans on."""
    if semantic is None:
        raise InvalidEndpointError(
            "endpoint is missing the semantic block",
            detail="semantic-missing",
        )
    required_fields = (
        "intent", "actor", "outcome", "capability",
        "confidence", "impact", "is_idempotent",
    )
    for fname in required_fields:
        value = getattr(semantic, fname, None)
        # ``is_idempotent`` is a boolean — None is the only refusal.
        # ``confidence`` is a number; ``0`` is valid even though
        # falsy. The other fields are strings.
        missing = (
            value is None
            or (isinstance(value, str) and not value.strip())
        )
        if missing:
            raise InvalidEndpointError(
                f"endpoint semantic block missing field {fname!r}",
                detail=f"semantic-missing-field:{fname}",
            )
    # ``actor`` is a free-form identifier per agtp-api §6 — the
    # "missing-field" check above already rejected empty strings;
    # no enumerated set is enforced. Suggested vocabulary lives in
    # :data:`core.endpoint.SUGGESTED_ACTORS` for authoring surfaces.
    if semantic.capability not in ALL_CAPABILITIES:
        raise InvalidEndpointError(
            f"semantic.capability {semantic.capability!r} is not "
            f"one of {', '.join(sorted(ALL_CAPABILITIES))}",
            detail="semantic-bad-capability",
        )
    if semantic.impact not in ALL_IMPACTS:
        raise InvalidEndpointError(
            f"semantic.impact {semantic.impact!r} is not "
            f"one of {', '.join(sorted(ALL_IMPACTS))}",
            detail="semantic-bad-impact",
        )
    cg = semantic.confidence
    if not isinstance(cg, (int, float)) or not 0.0 <= float(cg) <= 1.0:
        raise InvalidEndpointError(
            "semantic.confidence must be a number in [0, 1]",
            detail="semantic-bad-confidence",
        )


#: Recognized HTTP methods for ``external_service`` bindings. The
#: registry refuses anything else so misconfigurations don't reach
#: the upstream call.
_EXTERNAL_SERVICE_METHODS: frozenset = frozenset({
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
})


def _validate_external_service(
    handler: HandlerBinding,
    *,
    declared_errors: List[str],
) -> None:
    """
    Phase-4 checks for external_service bindings: required
    ``method`` + HTTPS-only enforcement on the upstream URL +
    sane timeout + every error_map target lives in the
    endpoint's ``errors`` list.

    The HTTPS check is strict: ``http://`` URLs are refused at
    registration. Servers that genuinely need plaintext upstream
    calls (rare) can pre-process the spec before registering.
    """
    if not handler.method:
        raise InvalidEndpointError(
            "external_service handler is missing 'method' (the "
            "upstream HTTP verb)",
            detail="external-service-missing-method",
        )
    if handler.method not in _EXTERNAL_SERVICE_METHODS:
        raise InvalidEndpointError(
            f"external_service handler.method {handler.method!r} is "
            f"not a recognized HTTP method; expected one of "
            f"{', '.join(sorted(_EXTERNAL_SERVICE_METHODS))}",
            detail="external-service-bad-method",
        )

    ref = (handler.url or "").strip()
    if not (ref.startswith("https://") or ref.startswith("https:")):
        raise InvalidEndpointError(
            f"external_service handler.url must be an HTTPS URL "
            f"(got {ref!r}); plaintext upstream calls are refused at "
            f"registration",
            detail="external-service-bad-scheme",
        )

    if (
        not isinstance(handler.timeout_seconds, (int, float))
        or handler.timeout_seconds <= 0
    ):
        raise InvalidEndpointError(
            "external_service handler.timeout_seconds must be a "
            "positive number",
            detail="external-service-bad-timeout",
        )

    declared_set = set(declared_errors or [])
    for status_code, agtp_code in (handler.error_map or {}).items():
        if agtp_code not in declared_set:
            raise InvalidEndpointError(
                f"external_service error_map maps HTTP {status_code} "
                f"to {agtp_code!r}, but {agtp_code!r} is not in the "
                f"endpoint's errors list "
                f"({', '.join(declared_errors) or '(none)'})",
                detail=f"external-service-error-map-undeclared:{agtp_code}",
            )


def _validate_handler(
    handler: Optional[HandlerBinding],
    *,
    declared_errors: List[str],
) -> None:
    if handler is None:
        raise InvalidEndpointError(
            "endpoint is missing the handler binding",
            detail="handler-missing",
        )
    if handler.type not in ALL_HANDLER_TYPES:
        raise InvalidEndpointError(
            f"handler.type {handler.type!r} is not one of "
            f"{', '.join(sorted(ALL_HANDLER_TYPES))}",
            detail="handler-bad-type",
        )
    # §9 type-specific reference field. Each binding type uses its
    # own name: registered_function → function, composition →
    # recipe, external_service → url. The shared check is "the
    # type-specific value is a non-empty string."
    if handler.type == "registered_function":
        ref_field, ref_value = "function", handler.function
    elif handler.type == "composition":
        ref_field, ref_value = "recipe", handler.recipe
    elif handler.type == "external_service":
        ref_field, ref_value = "url", handler.url
    else:  # pragma: no cover - guarded above
        ref_field, ref_value = "reference", ""
    if not ref_value or not isinstance(ref_value, str):
        raise InvalidEndpointError(
            f"handler.{ref_field} must be a non-empty string",
            detail="handler-empty-reference",
        )
    if handler.type == "external_service":
        _validate_external_service(handler, declared_errors=declared_errors)


def _validate_spec(spec: EndpointSpec) -> None:
    """Run every registration-time check on ``spec``. Raises
    :class:`InvalidEndpointError` on the first failure."""
    method = (spec.name or "").upper()
    if not is_approved_verb(method):
        raise InvalidEndpointError(
            f"method {method!r} is not in the AGTP verb catalog",
            detail="verb-not-in-catalog",
        )

    if not spec.path:
        raise InvalidEndpointError(
            "endpoint is missing 'path'",
            detail="path-missing",
        )
    try:
        validate_path(spec.path)
    except PathGrammarError as exc:
        raise InvalidEndpointError(
            f"path {spec.path!r} violates AGTP path grammar: "
            f"{exc.message}",
            detail=f"path-grammar:{exc.code}",
        ) from exc

    _validate_semantic(spec.semantic)

    if not isinstance(spec.required_params, list):
        raise InvalidEndpointError(
            "input.required must be a list",
            detail="param-bad-shape:input.required:-:not-list",
        )
    for i, p in enumerate(spec.required_params):
        _validate_param(p, kind="input.required", index=i)

    if not isinstance(spec.optional_params, list):
        raise InvalidEndpointError(
            "input.optional must be a list",
            detail="param-bad-shape:input.optional:-:not-list",
        )
    for i, p in enumerate(spec.optional_params):
        _validate_param(p, kind="input.optional", index=i)

    if not isinstance(spec.output, list):
        raise InvalidEndpointError(
            "output must be a list",
            detail="param-bad-shape:output:-:not-list",
        )
    for i, p in enumerate(spec.output):
        _validate_param(p, kind="output", index=i)

    if not isinstance(spec.errors, list):
        raise InvalidEndpointError(
            "errors must be a list of strings",
            detail="errors-bad-shape:-",
        )
    for i, e in enumerate(spec.errors):
        if not isinstance(e, str) or not e.strip():
            raise InvalidEndpointError(
                f"errors[{i}] must be a non-empty string",
                detail=f"errors-bad-shape:{i}",
            )

    _validate_handler(spec.handler, declared_errors=spec.errors or [])

    # required_scopes is a list of strings (may be empty). Used by
    # the dispatcher's authority gate before the handler runs.
    if not isinstance(spec.required_scopes, list):
        raise InvalidEndpointError(
            "required_scopes must be a list of strings",
            detail="required-scopes-bad-shape",
        )
    for i, s in enumerate(spec.required_scopes):
        if not isinstance(s, str) or not s.strip():
            raise InvalidEndpointError(
                f"required_scopes[{i}] must be a non-empty string",
                detail="required-scopes-bad-shape",
            )


# ---------------------------------------------------------------------------
# Registry.
# ---------------------------------------------------------------------------


# Handlers are opaque to Phase 1 — the registry stores whatever
# callable (or None) the caller hands it. Phase 2 introduces the
# resolution machinery that turns a ``HandlerBinding`` into a real
# Python callable.
HandlerFn = Optional[Callable[..., Any]]


class EndpointRegistry:
    """
    In-memory map of ``(method, path) -> (EndpointSpec, handler)``.

    The registry is populated at server startup. Mutations
    (:meth:`register`) are serialized through an internal lock; reads
    (:meth:`lookup`, :meth:`methods_for_path`, etc.) are intended to
    be safe for concurrent callers under the GIL because the
    underlying dict is only ever extended, never mutated, after
    startup. The lock is kept around mutations so a future
    hot-reload mode can extend the data layer without revisiting
    concurrency.
    """

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], Tuple[EndpointSpec, HandlerFn]] = {}
        self._paths: Dict[str, Set[str]] = {}
        self._lock = threading.Lock()

    # ---- Mutation ----

    def register(
        self,
        spec: EndpointSpec,
        handler: HandlerFn = None,
    ) -> None:
        """
        Validate ``spec`` and add an ``(method, path) -> (spec, handler)``
        entry. ``handler`` is opaque to the registry and may be
        ``None`` while Phase 2 wires the resolution machinery.

        Raises :class:`InvalidEndpointError` on validation failure
        and :class:`DuplicateEndpointError` if the key is already
        registered.
        """
        _validate_spec(spec)
        method = spec.name.upper()
        path = spec.path  # validated non-empty by _validate_spec
        assert path is not None  # for type checkers; validation enforces this

        key = (method, path)
        with self._lock:
            if key in self._entries:
                raise DuplicateEndpointError(method, path)
            self._entries[key] = (spec, handler)
            self._paths.setdefault(path, set()).add(method)

    # ---- Read ----

    def lookup(
        self,
        method: str,
        path: str,
    ) -> Optional[Tuple[EndpointSpec, HandlerFn]]:
        """Return the ``(spec, handler)`` registered at
        ``(method, path)``, or ``None``."""
        return self._entries.get((method.upper(), path))

    def methods_for_path(self, path: str) -> Set[str]:
        """Return the set of methods registered at ``path``. Empty
        set when no method is registered there."""
        return set(self._paths.get(path, ()))

    def has_path(self, path: str) -> bool:
        """True when at least one method is registered at ``path``."""
        return bool(self._paths.get(path))

    def all_endpoints(self) -> List[EndpointSpec]:
        """Return every registered spec, in registration order (the
        underlying dict preserves insertion order)."""
        return [spec for spec, _ in self._entries.values()]

    def count(self) -> int:
        """Number of registered endpoints."""
        return len(self._entries)

    # ---- Manifest rendering ----

    def render_manifest_section(self) -> List[Dict[str, Any]]:
        """
        Render the full registry as the JSON-ready list the server
        manifest will surface under its ``endpoints`` key.

        Each entry carries the full contract:

          * ``method`` — the AGTP verb.
          * ``path`` — the URI path.
          * ``description`` — operator-facing prose.
          * ``namespace`` — optional grouping label.
          * ``semantic`` — the semantic block.
          * ``input_schema`` — a JSON Schema (Draft 2020-12) document
            describing the request body. Projected from the spec's
            ``required_params`` / ``optional_params`` parameter
            lists at render time so TOML authors can use the
            ergonomic parameter-list shape; the manifest exposes
            the standardized JSON Schema.
          * ``output_schema`` — a JSON Schema document describing
            the response body. Same projection rule.
          * ``errors`` — list of named error-condition strings.
          * ``handler`` — public binding metadata, currently just
            ``{"type": "registered_function" | "composition" |
            "external_service"}``. The handler's full ``reference``
            is implementation detail and stays out of the manifest.
          * ``required_scopes`` — list of scope identifiers the
            invoking agent must declare (when non-empty).
          * ``deprecated`` — endpoint-level deprecation metadata
            (when present), parallel to the catalog's per-verb
            deprecation. The dispatcher stamps an
            ``AGTP-Endpoint-Warning`` advisory header on responses
            for invocations of deprecated endpoints.

        The legacy ``name`` / ``required_params`` / ``optional_params``
        keys carried by :meth:`EndpointSpec.to_dict` are stripped
        here — the manifest section uses only the canonical names.
        """
        # Local import keeps the registry independent of the schema
        # validator at module load time (the registry is a data
        # layer; the validator is server-side machinery).
        from server.schema_validation import (
            spec_to_input_schema, spec_to_output_schema,
        )

        out: List[Dict[str, Any]] = []
        for spec, _ in self._entries.values():
            full = spec.to_dict()
            # Project to the canonical manifest shape.
            entry: Dict[str, Any] = {
                "method": full["method"],
                "path": full.get("path"),
                "description": full.get("description", ""),
                "input_schema": spec_to_input_schema(spec),
                "output_schema": spec_to_output_schema(spec),
                "errors": full["errors"],
            }
            if "namespace" in full:
                entry["namespace"] = full["namespace"]
            if "semantic" in full:
                entry["semantic"] = full["semantic"]
            if spec.handler is not None:
                # Public ``handler`` projection — strip the
                # ``reference`` and the external_service binding's
                # internal fields. Only ``type`` rides the wire.
                entry["handler"] = {"type": spec.handler.type}
            if "required_scopes" in full:
                entry["required_scopes"] = full["required_scopes"]
            if spec.deprecated is not None:
                entry["deprecated"] = spec.deprecated.to_dict()
            out.append(entry)
        return out


__all__ = [
    "DuplicateEndpointError",
    "EndpointRegistry",
    "EndpointRegistryError",
    "HandlerFn",
    "InvalidEndpointError",
]
