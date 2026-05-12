# OpenAPI → AGTP converter

Phase 5 ships `agtp-import-openapi`, the on-ramp tool that turns an OpenAPI 3.x spec into a directory of AGTP endpoint TOML files. Every operation in the OpenAPI document becomes one TOML file pre-configured to use the Phase-4 `external_service` handler binding pointing at the underlying HTTP API. A 200-operation API converts in seconds; the developer reviews `# REVIEW:` comments in the generated output, tunes ambiguous mappings, and points their AGTP server at the directory.

This is the wrap-and-expose path that makes AGTP usable for organizations with existing infrastructure.

## Quick start

```bash
# Convert a spec into ./endpoints/
agtp-import-openapi openapi.yaml --output endpoints/

# Override the base URL (useful for staging vs production)
agtp-import-openapi openapi.yaml --output endpoints/ \
    --base-url https://api.staging.example.com

# Fail on any review-comment scenario instead of writing TOML with notes
agtp-import-openapi openapi.yaml --output endpoints/ --strict

# Suppress the # REVIEW: comments in the generated TOML
agtp-import-openapi openapi.yaml --output endpoints/ --no-review-comments
```

The tool exits with code:

- **0** — every operation generated and validated cleanly.
- **1** — at least one operation failed validation, or `--strict` was set and any review-comment fired.
- **2** — argparse / I/O / parse error before conversion started.

## What the converter does

For each operation in the OpenAPI spec:

1. **Picks an AGTP verb** for the HTTP method, biased by the operation's path / summary / `operationId`. Defaults to a conservative mapping; surfaces alternatives via `# REVIEW:`.
2. **Translates the path**, stripping trailing slashes and flagging verb-in-path leakage.
3. **Translates the request body schema** to `[[endpoint.input.required]]` / `[[endpoint.input.optional]]` tables. Preserves nested object / array shapes via inline JSON Schema.
4. **Translates the success response schema** to `[[endpoint.output]]` tables (using the first 2xx response).
5. **Builds the error map** from non-2xx response descriptions (slugified into AGTP error codes).
6. **Derives a semantic block** with heuristic defaults (intent, outcome, capability, impact, confidence, is_idempotent), each accompanied by a review-comment so the operator confirms.
7. **Generates an `external_service` handler binding** with the OpenAPI HTTP method, the spec's `servers[0].url` + path, security headers translated to `${VAR}`-templated values, and the error map.
8. **Validates** every generated TOML through the Phase-1 endpoint validator. Failures stamp the operation with a structured error message.
9. **Writes** one TOML file per operation to the output directory.

## HTTP method → AGTP verb mapping

The converter's defaults pick a reasonable starting point; the `# REVIEW:` comment lists alternatives for the developer.

| HTTP | Default AGTP | Heuristic adjustments |
|---|---|---|
| `GET` (path ends in `{param}`) | `FETCH` | — |
| `GET` (collection) | `LIST` | review-comment offers `QUERY` (search-style) and `DISCOVER` (capability discovery) |
| `POST` | `CREATE` | review-comment offers `SUBMIT`, `REGISTER`, `PUBLISH`, `BOOK`, `ORDER` |
| `PUT` | `REPLACE` | — |
| `DELETE` | `REMOVE` | — |
| `PATCH` | `MODIFY` | — |
| `HEAD` / `OPTIONS` | `DESCRIBE` | — |

Keyword overrides (matched against path / summary / `operationId`):

| Keyword | HTTP method | AGTP verb |
|---|---|---|
| `cancel` | `POST` | `CANCEL` |
| `confirm` / `approve` | `POST` | `CONFIRM` |
| `purchase` / `buy` | `POST` | `PURCHASE` |
| `order` | `POST` | `ORDER` |
| `book` | `POST` | `BOOK` |
| `reserve` | `POST` | `RESERVE` |
| `pay` | `POST` | `PAY` |
| `submit` | `POST` | `SUBMIT` |
| `register` | `POST` | `REGISTER` |
| `publish` | `POST` | `PUBLISH` |
| `send` | `POST` | `SEND` |
| `notify` | `POST` | `NOTIFY` |
| `validate` | `POST` / `PATCH` | `VALIDATE` |
| `audit` | `POST` / `PATCH` | `AUDIT` |
| `evaluate` | `POST` / `PATCH` | `EVALUATE` |
| `verify` | `POST` / `PATCH` | `VERIFY` |

Keyword matches are word-boundary aware: `reorder` does NOT match the `order` keyword (the leading `re` would obscure the intent).

## Path translation

| Input | Output | Note |
|---|---|---|
| `/users/{id}` | `/users/{id}` | identity |
| `/users/` | `/users` | trailing slash stripped + review-comment |
| `/users/list/{id}` | `/users/list/{id}` (validation fails) | strict path-grammar refusal: `LIST` is a verb |
| `/users/{id}/get-history` | `/users/{id}/get-history` (validation passes) | converter-side review-comment fires; the path-grammar layer doesn't refuse hyphenated tokens |
| `` (empty) | `/` | defaulted + review-comment |

Two layers of verb-in-path detection:

1. **Strict (registry-side):** `core.path_grammar.validate_path` refuses whole segments whose normalized form (uppercase, dashes/underscores stripped) is in the AGTP verb catalog. Catches `/orders/cancel`, `/users/list`. Validation failure surfaces as a registration-time refusal.
2. **Soft (converter-side):** the converter splits each segment on `-` / `_` and flags any *part* matching a catalog verb. Catches `/users/{id}/get-history`, `/orders_create`. The path is left as-is so validation passes; the developer decides whether to rewrite.

## Schema translation

OpenAPI request bodies become AGTP input schemas:

```yaml
requestBody:
  content:
    application/json:
      schema:
        type: object
        required: [name]
        properties:
          name:
            type: string
            description: Pet name
          tag:
            type: string
            description: Tag
```

becomes

```toml
[[endpoint.input.required]]
name = "name"
type = "string"
description = "Pet name"

[[endpoint.input.optional]]
name = "tag"
type = "string"
description = "Tag"
```

Special cases:

- **Bare arrays / scalars** at the top level wrap under a single `body` field with the full schema preserved.
- **`oneOf` / `anyOf` / `allOf`** at the top level emit a single passthrough field with the combinator's schema, plus a review-comment recommending the developer unfold or refine.
- **Nested objects / arrays** preserve the full JSON Schema under the field's `schema` field so the dispatcher's input validator (Phase 2) can enforce the inner shape.
- **`format`, `enum`** propagate from OpenAPI to the AGTP `format` / `enum` fields verbatim.

OpenAPI path / query parameters become required / optional inputs:

- Path parameters → always required.
- Query parameters → required if `required: true`, otherwise optional.
- Header / cookie parameters → not auto-translated (review-comment on the operation suggests adding them to `[endpoint.handler.headers]` if they're auth-related).

## Semantic block heuristics

Every generated endpoint gets a fully-populated semantic block (the registry validator refuses partial blocks). Defaults:

| Field | Default | Source / heuristic |
|---|---|---|
| `intent` | first sentence of `summary` or `description` | falls back to `Invoke the {VERB} endpoint.` |
| `actor` | `agent` | always |
| `outcome` | `responses["200"].description` | falls back to a generic post-condition |
| `capability` | derived from the AGTP verb's catalog category | `BOOK` → `transaction`, `QUERY` → `discovery`, `RECONCILE` → `transaction` |
| `confidence` | `0.85` (most ops) or `0.95` (irreversible) | review-comment encourages tuning |
| `impact` | `informational` (GET/HEAD), `irreversible` (PUT/DELETE/POST/PATCH) | review-comment encourages confirmation |
| `is_idempotent` | `true` (GET/HEAD/PUT/DELETE/OPTIONS), `false` (POST/PATCH) | review-comment encourages confirmation |

Review-comments fire on every defaulted field so the developer knows where to look.

## Error map

Non-2xx OpenAPI responses become entries in `[endpoint.handler.error_map]`:

```yaml
responses:
  '200':
    description: Pet
  '404':
    description: Pet not found
  '422':
    description: Validation failed
```

becomes

```toml
[endpoint.errors]
list = ["pet_not_found", "validation_failed", "upstream_timeout", ...]

[endpoint.handler.error_map]
"404" = "pet_not_found"
"422" = "validation_failed"
```

The AGTP error code is derived from the OpenAPI response description (slugified, snake_case). When no description is present, it falls back to `upstream_<status>`. The Phase-4 transport-layer codes (`upstream_timeout`, `upstream_connection_error`, `upstream_malformed_response`, `upstream_authentication_failed`, `upstream_error`) are always added to the endpoint's `errors` list so the handler can surface them.

## Handler binding

Every generated endpoint uses the `external_service` binding kind. The converter populates:

- **`url`**: `servers[0].url` + the operation path. Override with `--base-url` at convert time. (Pre-§9 the field was named `reference`; the loader still accepts the old name with a deprecation warning.)
- **`method`**: the OpenAPI HTTP method (uppercased).
- **`timeout_seconds`**: 30 (Phase-4 default; tune in the generated TOML).
- **`headers`**: derived from OpenAPI security schemes:
  - `http: bearer` → `Authorization: Bearer ${AGTP_UPSTREAM_<scheme>_TOKEN}`
  - `apiKey: in: header` → `<scheme.name>: ${AGTP_UPSTREAM_<scheme>}`
  - Other schemes (oauth2, openIdConnect, basic, cookie / query API keys) → review-comment, manual edit required.
- **`error_map`**: from response codes (see above).

## Review-comments: what they mean and how to act

Every `# REVIEW:` comment in generated TOML names a place where the converter's heuristic was not unambiguously correct. Common reasons:

| Comment | What to do |
|---|---|
| `Heuristic mapped HTTP POST on '/x' to CREATE.` | Confirm `CREATE` fits or pick another verb (`SUBMIT`, `REGISTER`, etc.). |
| `Heuristic mapped HTTP GET (collection) to LIST.` | Confirm `LIST` fits; switch to `QUERY` for search-style operations or `DISCOVER` for capability listings. |
| `is_idempotent defaulted to false for POST.` | Confirm; some POST endpoints are idempotent in practice. |
| `impact defaulted to 'irreversible' for POST/PUT/DELETE.` | Confirm; many writes are reversible (e.g., a PATCH that flips a flag). |
| `Path segment 'cancel' contains a recognized AGTP verb.` | Rewrite the path (e.g., `/orders/{id}/cancel` → `/orders/{id}` with method `CANCEL`) or accept that the registry will refuse it. |
| `Multiple 2xx responses declared.` | Confirm the converter chose the canonical success shape. |
| `Schema uses top-level oneOf.` | Unfold into separate operations or refine the schema manually. |
| `Security scheme 'X' (type 'oauth2') is not auto-translated.` | Add the necessary upstream auth header(s) to `[endpoint.handler.headers]` manually. |
| `servers[0].url is not HTTPS.` | Override with `--base-url=https://...` at convert time. |
| `OpenAPI spec has no servers[].url.` | Set `handler.url` manually before registering the endpoint. |

To find every review-comment in a generated directory:

```bash
grep -r "# REVIEW:" endpoints/
```

To count them per file:

```bash
grep -c "# REVIEW:" endpoints/*.toml
```

## Limitations

OpenAPI features that don't translate cleanly:

- **Callbacks / webhooks** — AGTP doesn't have a callback abstraction; the converter ignores `callbacks` blocks. Use Phase 3's composition handlers if you need server-initiated work.
- **Top-level `oneOf` / `anyOf`** in request or response schemas — emitted as a single passthrough field with a review-comment.
- **OAuth2 / OpenID Connect / HTTP Basic / API key in cookie or query** — emitted with a review-comment; manual header configuration required.
- **`$ref`s pointing into external files** — the converter does not chase external references. Bundle the spec first via `swagger-cli bundle` or similar.
- **Operations with `deprecated: true`** — converted as usual; the developer decides whether to register them.
- **Server variables in `servers[].url`** (`https://{environment}.api.example.com`) — not auto-substituted; override with `--base-url`.
- **OpenAPI 2.0 / Swagger** — refused at load time with a pointer to `swagger2openapi`. Convert the spec to OpenAPI 3.x first.

## Tutorial: the Petstore conversion

Take the canonical Petstore spec:

```yaml
openapi: 3.0.3
info:
  title: Petstore
  version: '1.0.0'
servers:
  - url: https://petstore.example.com/v1
paths:
  /pets:
    get:
      summary: List all pets
      responses:
        '200':
          description: A list of pets
    post:
      summary: Create a pet
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [name]
              properties:
                name: { type: string }
      responses:
        '201':
          description: Pet created
        '422':
          description: Validation failed
  /pets/{petId}:
    get:
      summary: Show a pet by id
      parameters:
        - name: petId
          in: path
          required: true
          schema: { type: string }
      responses:
        '200':
          description: A pet
        '404':
          description: Pet not found
```

Run:

```bash
agtp-import-openapi petstore.yaml --output endpoints/
```

You'll get three files:

- `list_pets.toml` — `LIST /pets`, `external_service` GET to `https://petstore.example.com/v1/pets`. Review-comments suggest `QUERY` / `DISCOVER` alternatives.
- `create_pets.toml` — `CREATE /pets`, `external_service` POST. Required input `name`. Error map `422 → validation_failed`. Review-comment suggests confirming `is_idempotent` and `impact`.
- `fetch_pets_petId.toml` — `FETCH /pets/{petId}`, `external_service` GET. Required input `petId` (path parameter). Error map `404 → pet_not_found`.

Now point the server at the directory:

```bash
python -m server --endpoints-dir endpoints/
```

The startup log shows three endpoints loaded, and the manifest's `endpoints` array surfaces them with `handler_type: "external_service"`. Invoke `LIST /pets` with the AGTP CLI; the request proxies to the upstream Petstore.

After review, the developer decides:

- Switch `LIST` to `QUERY` if the agent UX expects search-style filtering.
- Confirm `CREATE`'s `is_idempotent = false`.
- Accept the heuristic semantic block or refine it.

## Library API

The converter is also importable as a Python module:

```python
from tools.openapi_import import (
    load_openapi_spec,
    convert_spec,
    validate_converted,
    write_conversion,
)

spec = load_openapi_spec("openapi.yaml")
conversion = convert_spec(spec, base_url="https://api.example.com")
validate_converted(conversion)

for op in conversion.operations:
    print(f"{op.http_method:6} {op.path:30} -> {op.agtp_verb:10}")
    for c in op.review_comments:
        print(f"  REVIEW: {c}")

write_conversion(conversion, "endpoints/")
```

Use this when you want to embed the converter into a larger pipeline or run automated transformations on the output.

## Reverse direction (future work)

`agtp-export-openapi` — taking an AGTP server's manifest and producing an OpenAPI spec — is on the roadmap. The reverse mapping is mostly mechanical (AGTP verb → HTTP method via the legacy table, AGTP endpoint → OpenAPI operation). Useful for AGTP servers that want to expose an HTTP-compatible face for non-AGTP consumers. Not implemented in Phase 5.
