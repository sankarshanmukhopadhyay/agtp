"""
``agtp-import-openapi`` — convert an OpenAPI 3.x spec into a
directory of AGTP endpoint TOML files.

This is the on-ramp tool from Phase 5. Most organizations with HTTP
APIs already publish an OpenAPI spec; the converter takes that
spec and emits one TOML file per operation, pre-configured to use
the Phase-4 ``external_service`` handler binding pointing at the
underlying HTTP API. Developers then review the output (the
converter flags ambiguity with ``# REVIEW:`` comments), tune the
edge cases, and point their AGTP server at the directory.

Usage::

    agtp-import-openapi openapi-spec.yaml --output endpoints/
    agtp-import-openapi spec.json --base-url https://api.example.com
    agtp-import-openapi spec.yaml --strict   # fail on ambiguity

Library usage::

    from tools.openapi_import import (
        Conversion, convert_spec, load_openapi_spec,
    )

    spec = load_openapi_spec("openapi.yaml")
    conversion = convert_spec(spec, base_url="https://api.example.com")
    for op in conversion.operations:
        print(op.toml_filename, op.review_comments)

The library API is small and stable; the CLI is a thin wrapper.

Design notes
------------

The converter is intentionally heuristic. AGTP verbs encode intent
(``BOOK``, ``CANCEL``, ``RECONCILE``); HTTP methods encode the
transport. A POST in OpenAPI could legitimately become any of
``CREATE``, ``BOOK``, ``ORDER``, ``PURCHASE``, ``REGISTER``,
``SUBMIT`` — picking the right one is the developer's job. The
converter's job is:

  1. Pick the most defensible default given the operation's path,
     summary, and operation_id.
  2. Surface alternatives via ``# REVIEW:`` comments so the
     developer knows where to look.
  3. Refuse to silently lose information — every OpenAPI feature
     the converter can't translate (callbacks, complex security
     schemes, oneOf/anyOf at the top level) leaves a review comment.

Tests embed small synthetic specs (Petstore-shaped + edge cases)
rather than reaching for ``petstore.swagger.io``; ``tools/`` stays
network-free.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class OpenAPILoadError(Exception):
    """Raised when the OpenAPI spec file can't be loaded or parsed."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class ConvertedOperation:
    """One operation's worth of conversion output.

    Carries the TOML body string, the suggested filename, and the
    list of review comments the converter emitted at conversion
    time. Validation results (whether the AGTP endpoint validator
    accepted the generated spec) are filled in by the orchestrator,
    not the converter itself.
    """

    http_method: str
    path: str
    agtp_verb: str
    agtp_path: str
    toml_filename: str
    toml_body: str
    review_comments: List[str] = field(default_factory=list)
    validation_error: Optional[str] = None


@dataclass
class Conversion:
    """Top-level result of converting one OpenAPI spec."""

    source_path: Optional[str]
    base_url: str
    operations: List[ConvertedOperation] = field(default_factory=list)
    spec_warnings: List[str] = field(default_factory=list)

    @property
    def review_comment_count(self) -> int:
        return sum(len(op.review_comments) for op in self.operations)

    @property
    def validation_failed_count(self) -> int:
        return sum(1 for op in self.operations if op.validation_error)


# ---------------------------------------------------------------------------
# Spec loading.
# ---------------------------------------------------------------------------


def load_openapi_spec(path: Any) -> Dict[str, Any]:
    """Read ``path`` and return the parsed OpenAPI spec as a dict.

    Supports JSON and YAML (the latter requires ``pyyaml``;
    install via the project's ``[yaml]`` extra). OpenAPI 2.0
    (Swagger) is detected and refused with a pointer to the
    OpenAPI converter; only 3.0 / 3.1 are supported.
    """
    p = Path(path)
    if not p.exists():
        raise OpenAPILoadError(f"OpenAPI spec file not found: {p}")
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError as exc:
                raise OpenAPILoadError(
                    "YAML OpenAPI specs require PyYAML; install via "
                    "`pip install pyyaml` or the project's [yaml] "
                    "extra."
                ) from exc
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except (json.JSONDecodeError, Exception) as exc:
        # PyYAML's YAMLError isn't a JSONDecodeError; flatten both
        # via the broad except.
        if isinstance(exc, OpenAPILoadError):
            raise
        raise OpenAPILoadError(
            f"failed to parse {p}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise OpenAPILoadError(
            f"{p}: top-level OpenAPI document must be an object"
        )

    swagger_version = data.get("swagger")
    if swagger_version:
        raise OpenAPILoadError(
            f"{p}: OpenAPI 2.0 (Swagger) is not supported. Convert "
            f"the spec to OpenAPI 3.x first (e.g., via "
            f"`swagger2openapi`)."
        )

    openapi_version = str(data.get("openapi", ""))
    if not openapi_version:
        raise OpenAPILoadError(
            f"{p}: missing 'openapi' field at the document root; "
            f"expected '3.0.x' or '3.1.x'"
        )
    if not openapi_version.startswith(("3.0", "3.1")):
        raise OpenAPILoadError(
            f"{p}: unsupported OpenAPI version {openapi_version!r}; "
            f"this converter handles 3.0.x and 3.1.x"
        )

    return data


# ---------------------------------------------------------------------------
# HTTP method → AGTP verb mapping.
# ---------------------------------------------------------------------------


# Default mapping when no operation-context heuristic matches.
_DEFAULT_METHOD_TO_VERB: Dict[str, str] = {
    "GET":     "FETCH",
    "POST":    "CREATE",
    "PUT":     "REPLACE",
    "DELETE":  "REMOVE",
    "PATCH":   "MODIFY",
    "HEAD":    "DESCRIBE",
    "OPTIONS": "DESCRIBE",
}


# Keyword → verb override. Each entry is ``(http_methods, keyword,
# agtp_verb)`` — the converter picks the first entry whose keyword
# appears in the operation's path or summary AND whose http_method
# matches.
_KEYWORD_OVERRIDES: List[Tuple[Tuple[str, ...], str, str]] = [
    (("POST",),         "cancel",   "CANCEL"),
    (("POST",),         "confirm",  "CONFIRM"),
    (("POST",),         "approve",  "CONFIRM"),
    (("POST",),         "purchase", "PURCHASE"),
    (("POST",),         "buy",      "PURCHASE"),
    (("POST",),         "order",    "ORDER"),
    (("POST",),         "book",     "BOOK"),
    (("POST",),         "reserve",  "RESERVE"),
    (("POST",),         "pay",      "PAY"),
    (("POST",),         "submit",   "SUBMIT"),
    (("POST",),         "register", "REGISTER"),
    (("POST",),         "publish",  "PUBLISH"),
    (("POST",),         "send",     "SEND"),
    (("POST",),         "notify",   "NOTIFY"),
    (("POST", "PATCH"), "validate", "VALIDATE"),
    (("POST", "PATCH"), "audit",    "AUDIT"),
    (("POST", "PATCH"), "evaluate", "EVALUATE"),
    (("POST", "PATCH"), "verify",   "VERIFY"),
]


# Multi-verb alternatives the converter mentions in the
# review-comment when only the default mapping applies. Helps
# developers discover catalog verbs they may not have considered.
_VERB_ALTERNATIVES: Dict[str, List[str]] = {
    "CREATE":   ["SUBMIT", "REGISTER", "PUBLISH", "BOOK", "ORDER"],
    "FETCH":    ["QUERY", "DESCRIBE", "LIST"],
    "REPLACE":  ["UPDATE", "MODIFY"],
    "REMOVE":   ["DELETE", "CANCEL", "REVOKE"],
    "MODIFY":   ["UPDATE", "ADJUST", "AMEND"],
    "DESCRIBE": ["FETCH"],
}


@dataclass
class VerbMappingResult:
    verb: str
    review_comments: List[str] = field(default_factory=list)


def map_http_method_to_agtp_verb(
    http_method: str,
    path: str,
    operation: Optional[Dict[str, Any]] = None,
) -> VerbMappingResult:
    """
    Pick the AGTP verb for an OpenAPI operation.

    Decision order:

      1. Keyword override on path / summary / operation_id.
      2. GET single-resource vs collection heuristic.
      3. Default ``HTTP_METHOD → AGTP_VERB`` mapping.

    Whatever the mapping picks, ``VerbMappingResult.review_comments``
    carries any alternatives the developer should consider.
    """
    method_upper = http_method.upper()
    operation = operation or {}
    summary = str(operation.get("summary") or "")
    operation_id = str(operation.get("operationId") or "")
    path_lower = path.lower()
    haystack = " ".join((path_lower, summary.lower(), operation_id.lower()))

    review_comments: List[str] = []

    # Keyword overrides: first match wins, in declared order.
    for methods, keyword, verb in _KEYWORD_OVERRIDES:
        if method_upper not in methods:
            continue
        # Match whole-word at segment / token boundaries so e.g.
        # 'order' doesn't match inside 'reorder'. Path uses '/'-segments;
        # text uses word boundaries.
        if (
            re.search(rf"(?:^|[/_\-\s]){re.escape(keyword)}(?:[/_\-\s]|$)",
                      haystack)
        ):
            return VerbMappingResult(verb=verb, review_comments=review_comments)

    # GET single-resource vs collection heuristic.
    if method_upper == "GET":
        # Path ending in a parameter ({id}, {pet_id}, etc.) usually
        # means single-resource fetch. No trailing param → collection
        # fetch (LIST).
        last_segment = path.rstrip("/").rsplit("/", 1)[-1]
        if last_segment.startswith("{") and last_segment.endswith("}"):
            verb = "FETCH"
        else:
            verb = "LIST"
            review_comments.append(
                "Heuristic mapped HTTP GET (collection) to LIST. "
                "Consider QUERY if the operation has search-style "
                "filters, or DISCOVER for capability discovery."
            )
        return VerbMappingResult(verb=verb, review_comments=review_comments)

    # Default mapping. Add alternatives for the verbs developers
    # commonly want to swap.
    verb = _DEFAULT_METHOD_TO_VERB.get(method_upper)
    if verb is None:
        # Unrecognized HTTP method — flag prominently.
        return VerbMappingResult(
            verb="FETCH",
            review_comments=[
                f"HTTP method {method_upper!r} is not in the converter's "
                f"default mapping. Defaulting to FETCH; review and pick "
                f"a more accurate AGTP verb."
            ],
        )
    alternatives = _VERB_ALTERNATIVES.get(verb)
    if alternatives:
        review_comments.append(
            f"Heuristic mapped HTTP {method_upper} to {verb}. "
            f"Other plausible AGTP verbs: {', '.join(alternatives)}."
        )
    return VerbMappingResult(verb=verb, review_comments=review_comments)


# ---------------------------------------------------------------------------
# Path translation.
# ---------------------------------------------------------------------------


def translate_path(openapi_path: str) -> Tuple[str, List[str]]:
    """
    Translate an OpenAPI path to AGTP shape.

    Returns ``(translated_path, review_comments)``. The translated
    path strips trailing slashes (except the root) and detects
    verb-in-path violations against the AGTP catalog so the
    developer can tighten the path manually.

    Two layers of verb-in-path detection:

      1. The strict path-grammar layer (used at registration time
         by ``core.path_grammar.validate_path``): rejects whole
         segments whose normalized form (uppercase, dashes /
         underscores stripped) is in the AGTP verb set. Catches
         ``/get/orders``, ``/orders/cancel``, ``/users/list``.
      2. The converter-side hyphen / underscore token check:
         splits each segment on ``-`` / ``_`` and flags any *part*
         that matches a catalog verb. Catches the cases the strict
         layer can't (because stripping dashes turns
         ``get-history`` into a single non-verb token), e.g.
         ``/users/{id}/get-history`` or ``/orders_create``.
    """
    review_comments: List[str] = []
    if not openapi_path:
        return "/", ["Path was empty; defaulted to '/'."]

    path = openapi_path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        review_comments.append(
            f"Path {openapi_path!r} had a trailing slash; the AGTP "
            f"path grammar refuses these. The converter stripped it."
        )
        path = path.rstrip("/")

    # Layer 1: strict path-grammar match.
    try:
        from core.path_grammar import PathGrammarError, validate_path
        validate_path(path)
    except PathGrammarError as exc:
        if exc.code == "verb-in-path":
            review_comments.append(
                f"Path segment {exc.segment!r} contains a recognized "
                f"AGTP verb. Verbs belong in the method, not the path. "
                f"Consider rewriting (e.g., '/orders/cancel' -> "
                f"'/orders' with method 'CANCEL'). The converter left "
                f"the path as-is so the registry's 460 fires loudly."
            )
        else:
            review_comments.append(
                f"Path {path!r} fails AGTP path grammar: {exc.message}."
            )

    # Layer 2: hyphen / underscore token check. The strict layer
    # normalizes ``get-history`` to ``GETHISTORY`` (not a verb);
    # this layer catches the leak by inspecting each token.
    try:
        from core.methods import is_approved_verb, is_legacy_verb
    except ImportError:
        is_approved_verb = is_legacy_verb = None  # type: ignore[assignment]
    if is_approved_verb is not None:
        for segment in [s for s in path.split("/") if s]:
            if segment.startswith("{") and segment.endswith("}"):
                continue
            tokens = re.split(r"[-_]+", segment)
            if len(tokens) <= 1:
                continue  # whole-segment case already handled above
            for token in tokens:
                up = token.upper()
                if not up:
                    continue
                if is_approved_verb(up) or (
                    is_legacy_verb and is_legacy_verb(up)
                ):
                    review_comments.append(
                        f"Path segment {segment!r} contains the verb "
                        f"token {up!r}. AGTP verbs belong in the "
                        f"method, not the path. Consider rewriting "
                        f"(e.g., '/users/{{id}}/get-history' -> "
                        f"'/users/{{id}}/history' with method 'FETCH')."
                    )
                    break  # one warning per segment is enough

    return path, review_comments


# ---------------------------------------------------------------------------
# Schema translation: OpenAPI schema → ParamSpec list.
# ---------------------------------------------------------------------------


_OPENAPI_TYPE_TO_PARAM_TYPE: Dict[str, str] = {
    "string":  "string",
    "integer": "integer",
    "number":  "number",
    "boolean": "boolean",
    "array":   "array",
    "object":  "object",
}


@dataclass
class FieldShape:
    """One translated field from an OpenAPI schema."""

    name: str
    type: str
    description: str
    schema: Optional[Dict[str, Any]] = None
    enum: Optional[List[Any]] = None
    format: Optional[str] = None


def translate_schema_to_fields(
    schema: Optional[Dict[str, Any]],
) -> Tuple[List[FieldShape], List[str], List[str]]:
    """
    Translate an OpenAPI ``object`` schema into a list of
    :class:`FieldShape` entries.

    Returns ``(required_fields, optional_fields, review_comments)``.

    Only the top level of the schema is unrolled into fields; any
    nested ``object`` / ``array`` is preserved verbatim under the
    field's ``schema`` so the dispatcher's input/output validators
    enforce the inner shape via JSON Schema.

    Top-level schemas that aren't ``type=object`` (a bare string,
    a bare array, etc.) become a single field named ``body``
    carrying the schema verbatim — that's the conventional shape
    AGTP endpoints use for ``output`` slots whose response is a
    list, scalar, or polymorphic.
    """
    review_comments: List[str] = []
    if not isinstance(schema, dict):
        return [], [], review_comments

    # oneOf / anyOf at the top level — flag and return a single
    # passthrough field; developers usually want to manually unfold
    # these into separate operations or wrap them.
    for combinator in ("oneOf", "anyOf", "allOf"):
        if combinator in schema:
            review_comments.append(
                f"Schema uses top-level {combinator!r}; the converter "
                f"emits a single passthrough field. Consider unfolding "
                f"into separate operations or refining manually."
            )
            return (
                [FieldShape(
                    name="body", type="object",
                    description=str(schema.get("description")
                                    or f"{combinator} body"),
                    schema=dict(schema),
                )],
                [],
                review_comments,
            )

    schema_type = schema.get("type")
    if schema_type and schema_type != "object":
        # Bare scalar / array body. Wrap under a canonical name.
        param_type = _OPENAPI_TYPE_TO_PARAM_TYPE.get(schema_type, "object")
        return (
            [FieldShape(
                name="body", type=param_type,
                description=str(schema.get("description")
                                or f"{schema_type} body"),
                schema=dict(schema),
            )],
            [],
            review_comments,
        )

    # Object schema — unroll properties.
    properties = schema.get("properties") or {}
    required_names = set(schema.get("required") or [])
    if not isinstance(properties, dict):
        return [], [], review_comments

    required: List[FieldShape] = []
    optional: List[FieldShape] = []

    for name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        prop_type_raw = prop_schema.get("type") or "string"
        param_type = _OPENAPI_TYPE_TO_PARAM_TYPE.get(prop_type_raw, "string")
        description = str(
            prop_schema.get("description") or f"the {name} field"
        )
        enum_vals = prop_schema.get("enum")
        format_str = prop_schema.get("format")
        # For nested complex types (object / array), preserve the
        # full schema so the validator enforces the inner shape.
        embedded_schema: Optional[Dict[str, Any]] = None
        if param_type in ("object", "array"):
            embedded_schema = dict(prop_schema)

        shape = FieldShape(
            name=str(name),
            type=param_type,
            description=description,
            schema=embedded_schema,
            enum=list(enum_vals) if isinstance(enum_vals, list) else None,
            format=str(format_str) if format_str else None,
        )
        if name in required_names:
            required.append(shape)
        else:
            optional.append(shape)

    return required, optional, review_comments


# ---------------------------------------------------------------------------
# Semantic block heuristics.
# ---------------------------------------------------------------------------


# AGTP catalog category → semantic block 'capability' value. The
# catalog's categories are richer (analysis / domain_spanning / ...)
# than the semantic block's allowed capabilities (six values). Map
# the closest fit.
_CATEGORY_TO_CAPABILITY: Dict[str, str] = {
    "discovery":     "discovery",
    "transaction":   "transaction",
    "modification":  "modification",
    "retrieval":     "retrieval",
    "analysis":      "analysis",
    "notification":  "notification",
}


def _first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (split on ``.``,
    fallback to whole string)."""
    if not text:
        return ""
    return text.strip().split(".", 1)[0].strip()


def derive_semantic_block(
    http_method: str,
    agtp_verb: str,
    operation: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Heuristic semantic-block defaults from the operation's metadata.

    Returns ``(semantic_dict, review_comments)``. The dict has
    every required key populated (so the registry validator
    accepts it) but every value is a guess; review-comments
    enumerate which values the operator should sanity-check.
    """
    review_comments: List[str] = []
    summary = str(operation.get("summary") or "").strip()
    description = str(operation.get("description") or "").strip()

    intent = (
        summary
        or _first_sentence(description)
        or f"Invoke the {agtp_verb} endpoint."
    )
    if len(intent) < 20:
        intent = (
            f"{intent}. (Converter-generated; please refine the "
            f"intent to single-sentence agent-goal voice.)"
        )

    # Outcome heuristic: response[200].description first; fall back
    # to a generic post-condition phrasing.
    responses = operation.get("responses") or {}
    success_resp = responses.get("200") or responses.get("201") or {}
    response_desc = ""
    if isinstance(success_resp, dict):
        response_desc = str(success_resp.get("description") or "").strip()
    outcome = (
        response_desc
        or f"The {agtp_verb} operation returns its declared response shape."
    )

    # Capability — derive from the AGTP verb's catalog category.
    capability = "retrieval"  # safe default
    try:
        from core.methods import categorize
        categories = categorize(agtp_verb) or []
        for cat in categories:
            if cat in _CATEGORY_TO_CAPABILITY:
                capability = _CATEGORY_TO_CAPABILITY[cat]
                break
    except Exception:  # noqa: BLE001
        pass  # capability stays 'retrieval'

    # Impact — heuristic from method.
    method_upper = http_method.upper()
    if method_upper == "GET" or method_upper == "HEAD":
        impact = "informational"
    elif method_upper in ("POST", "PATCH"):
        impact = "irreversible"
        review_comments.append(
            f"impact defaulted to 'irreversible' for "
            f"{method_upper}. Confirm; many writes are reversible "
            f"(e.g., PATCH that flips a flag)."
        )
    elif method_upper in ("PUT", "DELETE"):
        impact = "irreversible"
        review_comments.append(
            f"impact defaulted to 'irreversible' for "
            f"{method_upper}. Confirm; PUT may be reversible if the "
            f"operation overwrites with a previously-known value."
        )
    else:
        impact = "informational"

    # Confidence — bump for irreversible ops.
    confidence = 0.95 if impact == "irreversible" else 0.85

    # is_idempotent — HTTP semantics defaults.
    is_idempotent = method_upper in ("GET", "HEAD", "PUT", "DELETE", "OPTIONS")
    if method_upper == "POST":
        review_comments.append(
            "is_idempotent defaulted to false for POST. Confirm; "
            "some POST endpoints are idempotent in practice "
            "(e.g., 'create or get'-style)."
        )
    if method_upper == "DELETE":
        # DELETE is technically idempotent per RFC 7231 but in
        # practice many APIs treat second-DELETE as 404. Surface.
        review_comments.append(
            "is_idempotent defaulted to true for DELETE per HTTP "
            "semantics. Some APIs return 404 on second DELETE; "
            "review the endpoint contract."
        )

    semantic = {
        "intent": intent,
        "actor": "agent",
        "outcome": outcome,
        "capability": capability,
        "confidence": confidence,
        "impact": impact,
        "is_idempotent": is_idempotent,
    }
    return semantic, review_comments


# ---------------------------------------------------------------------------
# Handler binding generation.
# ---------------------------------------------------------------------------


def _security_headers(
    spec: Dict[str, Any],
    operation: Dict[str, Any],
) -> Tuple[Dict[str, str], List[str]]:
    """Derive ``${VAR}``-templated headers from the operation's
    declared security schemes.

    Phase 5 supports HTTP bearer + API-key-in-header schemes with
    sensible env-var conventions. Other schemes (oauth2, openIdConnect,
    HTTP basic, API key in cookie/query) get a review-comment.
    """
    headers: Dict[str, str] = {}
    review_comments: List[str] = []

    components = spec.get("components") or {}
    schemes = components.get("securitySchemes") or {}
    if not isinstance(schemes, dict):
        schemes = {}

    operation_security = operation.get("security")
    if operation_security is None:
        operation_security = spec.get("security") or []
    if not isinstance(operation_security, list):
        operation_security = []

    for requirement in operation_security:
        if not isinstance(requirement, dict):
            continue
        for scheme_name in requirement.keys():
            scheme = schemes.get(scheme_name) or {}
            if not isinstance(scheme, dict):
                continue
            stype = scheme.get("type")
            if stype == "http" and scheme.get("scheme", "").lower() == "bearer":
                env = f"AGTP_UPSTREAM_{scheme_name.upper()}_TOKEN"
                headers["Authorization"] = "Bearer ${" + env + "}"
            elif stype == "apiKey" and scheme.get("in") == "header":
                header_name = str(scheme.get("name") or "X-API-Key")
                env = f"AGTP_UPSTREAM_{scheme_name.upper()}"
                headers[header_name] = "${" + env + "}"
            else:
                review_comments.append(
                    f"Security scheme {scheme_name!r} (type {stype!r}) "
                    f"is not auto-translated. Add the necessary "
                    f"upstream auth header(s) to "
                    f"[endpoint.handler.headers] manually."
                )

    return headers, review_comments


def _resolve_base_url(
    spec: Dict[str, Any],
    base_url_override: Optional[str],
) -> Tuple[str, List[str]]:
    """Resolve the base URL the external_service handler should
    target. Order of precedence:

      1. ``base_url_override`` from the CLI flag.
      2. ``servers[0].url`` from the spec.
      3. ``""`` with a review-comment.
    """
    review_comments: List[str] = []
    if base_url_override:
        return base_url_override.rstrip("/"), review_comments
    servers = spec.get("servers") or []
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and first.get("url"):
            url = str(first["url"]).rstrip("/")
            if not url.startswith("https://"):
                review_comments.append(
                    f"servers[0].url is {url!r}; AGTP refuses non-HTTPS "
                    f"upstreams. Override with --base-url at convert "
                    f"time, or edit the generated handler.url."
                )
            return url, review_comments
    review_comments.append(
        "OpenAPI spec has no servers[].url. Override with --base-url "
        "or edit handler.url manually."
    )
    return "", review_comments


def _interpolate_path_into_url(base_url: str, openapi_path: str) -> str:
    """Concatenate base URL + path. Path parameters stay as-is; the
    AGTP runtime can fill them in once it has a per-request path."""
    if not base_url:
        return openapi_path
    return base_url.rstrip("/") + openapi_path


def _error_map_from_responses(
    responses: Dict[str, Any],
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Build (error_map, declared_errors, review_comments) from the
    OpenAPI ``responses`` block.

    Each non-2xx response becomes one error_map entry. The
    AGTP-side error code is derived from the response's
    description (slugified) when available; falls back to
    ``upstream_<status>``."""
    error_map: Dict[str, str] = {}
    declared: List[str] = []
    review: List[str] = []

    if not isinstance(responses, dict):
        return error_map, declared, review

    for status, resp_body in responses.items():
        status_str = str(status)
        if not status_str.isdigit():
            continue
        status_code = int(status_str)
        if status_code < 400:
            continue
        if not isinstance(resp_body, dict):
            continue
        desc = str(resp_body.get("description") or "")
        if desc:
            agtp_code = _slugify(desc)
        else:
            agtp_code = f"upstream_{status_code}"
        if not agtp_code:
            agtp_code = f"upstream_{status_code}"
        error_map[status_str] = agtp_code
        if agtp_code not in declared:
            declared.append(agtp_code)

    return error_map, declared, review


def _slugify(text: str) -> str:
    """Lowercased, underscore-separated identifier from arbitrary
    text. Used to derive AGTP error codes from response
    descriptions."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s\-]+", "_", text)
    text = text.strip("_")
    if not text:
        return ""
    # Drop a leading digit (TOML keys can be numbered, but AGTP
    # error codes read better without it).
    if text[0].isdigit():
        text = "_" + text
    return text[:64]


# ---------------------------------------------------------------------------
# TOML emission.
# ---------------------------------------------------------------------------


def _toml_string(value: str) -> str:
    """Render ``value`` as a TOML string. Uses double quotes with
    JSON-style escaping; multi-line strings get triple quotes."""
    if "\n" in value:
        return '"""\n' + value.replace('"""', '\\"""') + '"""'
    return json.dumps(value)


_BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: Any) -> str:
    """Render ``key`` as a TOML key. Bare keys (ASCII alnum / ``_`` /
    ``-``) emit unquoted; anything else (e.g. ``$ref``, dotted keys
    inside a JSON Schema) gets a quoted-key rendering."""
    text = str(key)
    if _BARE_KEY_PATTERN.match(text):
        return text
    return _toml_string(text)


def _toml_inline_value(value: Any) -> str:
    """Render ``value`` as a TOML inline literal (string / number /
    boolean / array / inline table)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline_value(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return (
            "{ "
            + ", ".join(
                f"{_toml_key(k)} = {_toml_inline_value(v)}"
                for k, v in value.items()
            )
            + " }"
        )
    return _toml_string(str(value))


def _emit_review_comment(comment: str) -> str:
    """Render a single review comment, wrapping at 72 columns."""
    prefix = "# REVIEW: "
    indent = "#          "
    out_lines: List[str] = []
    words = comment.split()
    current = prefix
    for word in words:
        if len(current) + len(word) + 1 > 80:
            out_lines.append(current.rstrip())
            current = indent + word
        else:
            current += (" " if current not in (prefix, indent) else "") + word
    out_lines.append(current.rstrip())
    return "\n".join(out_lines)


def _emit_field_table(field_shape: FieldShape, table_kind: str) -> str:
    """Emit one ``[[endpoint.input.required]]`` (or similar) table
    for a :class:`FieldShape`."""
    parts: List[str] = []
    parts.append(f"[[endpoint.{table_kind}]]")
    parts.append(f"name = {_toml_string(field_shape.name)}")
    parts.append(f"type = {_toml_string(field_shape.type)}")
    parts.append(f"description = {_toml_string(field_shape.description)}")
    if field_shape.format:
        parts.append(f"format = {_toml_string(field_shape.format)}")
    if field_shape.enum is not None:
        parts.append(f"enum = {_toml_inline_value(field_shape.enum)}")
    if field_shape.schema is not None:
        # Inline-table render; fine for small schemas. Larger schemas
        # may warrant a multi-line emission, but inline is what the
        # existing samples use.
        parts.append(
            f"schema = {_toml_inline_value(field_shape.schema)}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Conversion driver.
# ---------------------------------------------------------------------------


def _toml_filename_for(verb: str, path: str) -> str:
    """Generate a stable filename for the converted operation."""
    slug = path.strip("/")
    slug = re.sub(r"[{}]", "", slug)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug)
    slug = slug.strip("_") or "root"
    return f"{verb.lower()}_{slug}.toml"[:120]


_HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def convert_operation(
    *,
    http_method: str,
    openapi_path: str,
    operation: Dict[str, Any],
    spec: Dict[str, Any],
    base_url: str,
    review_comments_global: Optional[List[str]] = None,
) -> ConvertedOperation:
    """Convert one OpenAPI operation into a single AGTP TOML file.

    All ambiguity surfaces as ``# REVIEW:`` comments inside the TOML
    body and on the returned :class:`ConvertedOperation`'s
    ``review_comments`` list (the same list, exposed two ways).
    """
    review: List[str] = list(review_comments_global or [])

    # ---- verb + path ----
    mapping = map_http_method_to_agtp_verb(
        http_method, openapi_path, operation,
    )
    review.extend(mapping.review_comments)

    agtp_path, path_review = translate_path(openapi_path)
    review.extend(path_review)

    # ---- input schema ----
    request_body = operation.get("requestBody") or {}
    request_schema = None
    if isinstance(request_body, dict):
        content = request_body.get("content") or {}
        if isinstance(content, dict):
            json_content = content.get("application/json") or {}
            if isinstance(json_content, dict):
                request_schema = json_content.get("schema")
    required_fields, optional_fields, schema_review = (
        translate_schema_to_fields(request_schema)
    )
    review.extend(schema_review)

    # OpenAPI also declares path / query parameters separately.
    # Promote each to a required input field. Path parameters are
    # always required; query parameters honor their ``required`` flag.
    for param in operation.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        if param.get("in") not in ("path", "query"):
            continue  # skip header/cookie params for now
        pname = str(param.get("name") or "").strip()
        if not pname:
            continue
        pschema = param.get("schema") or {}
        ptype_raw = pschema.get("type") or "string"
        ptype = _OPENAPI_TYPE_TO_PARAM_TYPE.get(ptype_raw, "string")
        shape = FieldShape(
            name=pname,
            type=ptype,
            description=str(
                param.get("description") or f"the {pname} parameter"
            ),
            format=str(pschema.get("format")) if pschema.get("format") else None,
            enum=(
                list(pschema["enum"])
                if isinstance(pschema.get("enum"), list)
                else None
            ),
        )
        if param.get("in") == "path" or param.get("required"):
            required_fields.append(shape)
        else:
            optional_fields.append(shape)

    # ---- output schema ----
    responses = operation.get("responses") or {}
    success_status = None
    for status in ("200", "201", "202", "204"):
        if status in responses:
            success_status = status
            break
    success_response = responses.get(success_status, {}) if success_status else {}
    output_schema = None
    if isinstance(success_response, dict):
        content = success_response.get("content") or {}
        if isinstance(content, dict):
            json_content = content.get("application/json") or {}
            if isinstance(json_content, dict):
                output_schema = json_content.get("schema")
    output_fields, _output_optional, output_review = (
        translate_schema_to_fields(output_schema)
    )
    # We treat all output declarations as the canonical shape; the
    # registry's output validator allows additionalProperties.
    review.extend(output_review)

    # If multiple 2xx responses are declared, flag — the converter
    # picked the first one.
    success_responses = [
        s for s in responses.keys()
        if isinstance(s, str) and s.startswith("2")
    ]
    if len(success_responses) > 1:
        review.append(
            f"Multiple 2xx responses declared "
            f"({', '.join(sorted(success_responses))}); the converter "
            f"used {success_status!r} for the output schema. Confirm "
            f"this is the canonical success shape."
        )

    # ---- error map ----
    error_map, declared_errors, error_review = _error_map_from_responses(responses)
    review.extend(error_review)

    # Always declare the public upstream-failure codes the handler
    # may produce so the registry accepts the spec.
    for code in (
        "upstream_timeout",
        "upstream_connection_error",
        "upstream_malformed_response",
        "upstream_authentication_failed",
        "upstream_error",
    ):
        if code not in declared_errors:
            declared_errors.append(code)

    # ---- semantic block ----
    semantic, semantic_review = derive_semantic_block(
        http_method, mapping.verb, operation,
    )
    review.extend(semantic_review)

    # ---- handler binding ----
    upstream_url = _interpolate_path_into_url(base_url, openapi_path)
    headers, header_review = _security_headers(spec, operation)
    review.extend(header_review)

    # ---- emit TOML ----
    body_lines: List[str] = []
    body_lines.append("# Generated by agtp-import-openapi.")
    body_lines.append(
        f"# Source: {http_method.upper()} {openapi_path}"
    )
    if operation.get("operationId"):
        body_lines.append(
            f"# OpenAPI operationId: {operation.get('operationId')}"
        )
    body_lines.append("")

    body_lines.append("[endpoint]")
    body_lines.append(_emit_review_comment(
        f"Heuristic mapped HTTP {http_method.upper()} on "
        f"{openapi_path!r} to AGTP {mapping.verb!r}. Review and "
        f"adjust if a different verb fits the operation's intent."
    ))
    body_lines.append(f"method = {_toml_string(mapping.verb)}")
    body_lines.append(f"path = {_toml_string(agtp_path)}")
    description = (
        str(operation.get("summary") or "").strip()
        or _first_sentence(str(operation.get("description") or ""))
        or f"{mapping.verb} {agtp_path}"
    )
    body_lines.append(f"description = {_toml_string(description)}")
    namespace_tag = (
        operation.get("tags") and operation["tags"][0]
        if isinstance(operation.get("tags"), list)
        else None
    )
    if namespace_tag:
        body_lines.append(f"namespace = {_toml_string(str(namespace_tag))}")
    body_lines.append("")

    body_lines.append("[endpoint.semantic]")
    body_lines.append(_emit_review_comment(
        "Semantic-block defaults are heuristic. Confirm intent / "
        "outcome / capability / impact / confidence "
        "/ is_idempotent before deploying."
    ))
    body_lines.append(f"intent = {_toml_string(semantic['intent'])}")
    body_lines.append(f"actor = {_toml_string(semantic['actor'])}")
    body_lines.append(f"outcome = {_toml_string(semantic['outcome'])}")
    body_lines.append(f"capability = {_toml_string(semantic['capability'])}")
    body_lines.append(
        f"confidence = {semantic['confidence']}"
    )
    body_lines.append(
        f"impact = {_toml_string(semantic['impact'])}"
    )
    body_lines.append(
        f"is_idempotent = "
        f"{'true' if semantic['is_idempotent'] else 'false'}"
    )
    body_lines.append("")

    if required_fields:
        for shape in required_fields:
            body_lines.append(_emit_field_table(shape, "input.required"))
            body_lines.append("")
    if optional_fields:
        for shape in optional_fields:
            body_lines.append(_emit_field_table(shape, "input.optional"))
            body_lines.append("")
    if output_fields:
        for shape in output_fields:
            body_lines.append(_emit_field_table(shape, "output"))
            body_lines.append("")

    body_lines.append("[endpoint.errors]")
    body_lines.append(
        f"list = {_toml_inline_value(declared_errors)}"
    )
    body_lines.append("")

    body_lines.append("[endpoint.handler]")
    body_lines.append('type = "external_service"')
    if upstream_url:
        body_lines.append(f"url = {_toml_string(upstream_url)}")
        if not upstream_url.startswith("https://"):
            body_lines.append(_emit_review_comment(
                "Upstream URL is not HTTPS. AGTP refuses non-HTTPS "
                "URLs at registration; override with "
                "--base-url=https://... or edit before loading."
            ))
    else:
        body_lines.append(_emit_review_comment(
            "No upstream URL was resolvable from the spec. Set "
            "handler.url to the upstream HTTPS URL before "
            "registering this endpoint."
        ))
        body_lines.append('url = ""')
    body_lines.append(f"method = {_toml_string(http_method.upper())}")
    body_lines.append("timeout_seconds = 30")
    body_lines.append("")

    if headers:
        body_lines.append("[endpoint.handler.headers]")
        for hname, hvalue in headers.items():
            body_lines.append(f"{_toml_string(hname)} = {_toml_string(hvalue)}")
        body_lines.append("")
    if error_map:
        body_lines.append("[endpoint.handler.error_map]")
        for status, code in error_map.items():
            body_lines.append(
                f"{_toml_string(str(status))} = {_toml_string(code)}"
            )
        body_lines.append("")

    toml_body = "\n".join(body_lines).rstrip() + "\n"

    return ConvertedOperation(
        http_method=http_method.upper(),
        path=openapi_path,
        agtp_verb=mapping.verb,
        agtp_path=agtp_path,
        toml_filename=_toml_filename_for(mapping.verb, agtp_path),
        toml_body=toml_body,
        review_comments=review,
    )


def convert_spec(
    spec: Dict[str, Any],
    *,
    base_url: Optional[str] = None,
    source_path: Optional[str] = None,
) -> Conversion:
    """Walk every operation in ``spec`` and produce a
    :class:`Conversion` carrying one :class:`ConvertedOperation`
    per operation."""
    resolved_base, base_review = _resolve_base_url(spec, base_url)
    conversion = Conversion(
        source_path=source_path,
        base_url=resolved_base,
        operations=[],
        spec_warnings=list(base_review),
    )

    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        conversion.spec_warnings.append(
            "Spec has no 'paths' object; nothing to convert."
        )
        return conversion

    for openapi_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            converted = convert_operation(
                http_method=method,
                openapi_path=openapi_path,
                operation=operation,
                spec=spec,
                base_url=resolved_base,
            )
            conversion.operations.append(converted)

    return conversion


# ---------------------------------------------------------------------------
# Validation against the AGTP endpoint validator.
# ---------------------------------------------------------------------------


def validate_converted(conversion: Conversion) -> Conversion:
    """Run each converted TOML body through the AGTP endpoint loader
    + registry validator. Stamps each :class:`ConvertedOperation`
    with a ``validation_error`` (or ``None``) and returns the same
    Conversion."""
    try:
        import tomllib as _toml  # py3.11+
    except ImportError:  # pragma: no cover
        import tomli as _toml  # type: ignore[no-redef]

    from server.endpoint_loader import _spec_from_toml
    from server.endpoint_registry import (
        InvalidEndpointError, _validate_spec,
    )

    for op in conversion.operations:
        try:
            data = _toml.loads(op.toml_body)
        except Exception as exc:  # noqa: BLE001
            op.validation_error = f"toml-parse: {exc}"
            continue
        try:
            spec_obj = _spec_from_toml(data)
        except ValueError as exc:
            op.validation_error = f"shape: {exc}"
            continue
        try:
            _validate_spec(spec_obj)
        except InvalidEndpointError as exc:
            op.validation_error = f"validate: {exc.detail} ({exc})"
            continue
        op.validation_error = None
    return conversion


# ---------------------------------------------------------------------------
# Output writer.
# ---------------------------------------------------------------------------


def write_conversion(
    conversion: Conversion,
    output_dir: Any,
    *,
    overwrite: bool = True,
) -> List[str]:
    """Write each operation's TOML to ``output_dir/<filename>``.
    Returns the list of paths written."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for op in conversion.operations:
        target = out / op.toml_filename
        if target.exists() and not overwrite:
            continue
        target.write_text(op.toml_body, encoding="utf-8")
        written.append(str(target))
    return written


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agtp-import-openapi",
        description=(
            "Convert an OpenAPI 3.x spec into a directory of AGTP "
            "endpoint TOML files (one per operation). Generated TOML "
            "uses external_service handler bindings pointing at the "
            "underlying HTTP API; review the # REVIEW: comments in "
            "the output before deploying."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  agtp-import-openapi openapi.yaml --output endpoints/\n"
            "  agtp-import-openapi spec.json --base-url https://api.staging.example.com\n"
            "  agtp-import-openapi spec.yaml --strict   # fail on ambiguity\n"
        ),
    )
    parser.add_argument("spec", help="OpenAPI 3.x spec (JSON or YAML)")
    parser.add_argument(
        "--output", default="endpoints",
        help="Target directory for generated TOML files "
             "(default: ./endpoints).",
    )
    parser.add_argument(
        "--base-url", default=None,
        help="Override the base URL from the spec's 'servers'. "
             "Useful when the spec lists staging URLs and you want "
             "production (or vice versa).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Treat any review-comment scenario as a hard failure. "
             "Default is permissive (review-comments are added but "
             "the converter writes the TOML anyway).",
    )
    parser.add_argument(
        "--no-review-comments", action="store_true",
        help="Suppress # REVIEW: comments in the generated TOML. "
             "Off by default — review-comments are how the converter "
             "tells you where to look.",
    )
    parser.add_argument(
        "--no-validate", action="store_true",
        help="Skip the post-generation validator pass. Default is "
             "to validate every generated TOML.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code:

      * 0 — every operation generated and validated cleanly.
      * 1 — at least one operation failed validation OR ``--strict``
            was set and any review-comment fired.
      * 2 — argparse / IO / parse error before conversion started.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        spec = load_openapi_spec(args.spec)
    except OpenAPILoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    conversion = convert_spec(
        spec, base_url=args.base_url, source_path=str(args.spec),
    )

    if args.no_review_comments:
        for op in conversion.operations:
            op.toml_body = "\n".join(
                line for line in op.toml_body.splitlines()
                if not line.lstrip().startswith("#")
            ) + "\n"

    if not args.no_validate:
        validate_converted(conversion)

    write_conversion(conversion, args.output)

    # Summary.
    total_ops = len(conversion.operations)
    review_count = conversion.review_comment_count
    failed = conversion.validation_failed_count
    passed = total_ops - failed

    print()
    print("agtp-import-openapi finished.")
    print(f"  Input:               {args.spec}")
    print(f"  Output:              {args.output}/")
    print(f"  Base URL:            {conversion.base_url or '(unset)'}")
    print(f"  Operations processed: {total_ops}")
    print(f"  Endpoints generated: {total_ops}")
    print(f"  Validation passed:   {passed}")
    print(f"  Validation failed:   {failed}")
    print(f"  Review-comments:     {review_count}")
    if conversion.spec_warnings:
        print()
        print("Spec-level warnings:")
        for w in conversion.spec_warnings:
            print(f"  - {w}")
    if failed:
        print()
        print("Validation failures (file → reason):")
        for op in conversion.operations:
            if op.validation_error:
                print(f"  - {op.toml_filename}: {op.validation_error}")

    if args.strict and (review_count or failed):
        print(
            "\n--strict was set and at least one review-comment "
            "fired. Treating as failure.",
            file=sys.stderr,
        )
        return 1
    return 1 if failed else 0


__all__ = [
    "Conversion",
    "ConvertedOperation",
    "FieldShape",
    "OpenAPILoadError",
    "VerbMappingResult",
    "build_parser",
    "convert_operation",
    "convert_spec",
    "derive_semantic_block",
    "load_openapi_spec",
    "main",
    "map_http_method_to_agtp_verb",
    "translate_path",
    "translate_schema_to_fields",
    "validate_converted",
    "write_conversion",
]


if __name__ == "__main__":
    sys.exit(main())
