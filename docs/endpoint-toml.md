# Endpoint TOML schema

Non-normative reference for the `*.toml` files
[`server.endpoint_loader`](../server/endpoint_loader.py) reads at
startup. Each file declares one endpoint — a `(method, path)` pair
and the full contract the server promises to honor at that pair.

The loader returns parsed specs and structured `LoadError`s; the
caller registers the valid specs with
[`EndpointRegistry`](../server/endpoint_registry.py). The registry
enforces the catalog and path-grammar rules described in the root
[README](../README.md#method-validation-catalog--path-grammar).

## Skeleton

```toml
[endpoint]
method = "BOOK"                     # AGTP verb (uppercase)
path = "/room"                      # URI path served at
description = "Books a room for the named guest."
namespace = "reservations"          # optional grouping

# ---- semantic block (all seven fields required) ----
[endpoint.semantic]
intent = "Reserve a room for the named guest at the named property."
actor = "agent"                     # agent | user | system
outcome = "A confirmed reservation_id is returned for the guest."
capability = "transaction"          # discovery | transaction |
                                    # modification | retrieval |
                                    # analysis | notification
confidence = 0.85          # 0.0 to 1.0
impact = "irreversible"        # informational | reversible | irreversible
is_idempotent = false

# ---- input parameters ----
[[endpoint.input.required]]
name = "guest_id"
type = "string"                     # string | integer | number |
                                    # boolean | object | array
description = "The booking guest's profile identifier."
format = "uuid"                     # optional named format
                                    # (date, date-time, email, uuid, ...)

[[endpoint.input.required]]
name = "room_type"
type = "string"
description = "Room category."
enum = ["single", "double", "suite"]   # optional value constraint

[[endpoint.input.optional]]
name = "special_requests"
type = "string"
description = "Free-form notes the front desk should see at check-in."

# ---- output fields ----
[[endpoint.output]]
name = "reservation_id"
type = "string"
description = "Server-assigned reservation handle."

[[endpoint.output]]
name = "confirmation_email"
type = "string"
description = "Email address the confirmation went to."
format = "email"

# ---- declared error conditions ----
# Named string identifiers, distinct from HTTP / AGTP status codes.
# Empty list is fine for endpoints that declare no failure modes.
[endpoint.errors]
list = ["room_unavailable", "invalid_dates", "guest_not_found"]

# ---- handler binding ----
[endpoint.handler]
type = "registered_function"        # registered_function | composition |
                                    # external_service
function = "staybeta.handlers.book_room"
```

## Field-by-field

### `[endpoint]`

| Key | Type | Required | Notes |
|---|---|---|---|
| `method` | string | **yes** | The AGTP method. Must be in [`core/methods.json`](../core/methods.json). Loader uppercases it. |
| `path` | string | **yes** | Must begin with `/`, must not have a trailing slash (except the root), must not embed a verb token in any segment. See [`core/path_grammar.py`](../core/path_grammar.py). |
| `description` | string | optional | Operator-facing prose. |
| `namespace` | string | optional | Free-form grouping label. The registry doesn't enforce uniqueness across namespaces. |

### `[endpoint.semantic]`

All seven fields are required by the registry validator.

| Key | Type | Allowed values |
|---|---|---|
| `intent` | string | non-empty single sentence, agent-goal voice |
| `actor` | string | `agent`, `user`, `system` |
| `outcome` | string | non-empty single sentence, post-condition voice |
| `capability` | string | `discovery`, `transaction`, `modification`, `retrieval`, `analysis`, `notification` |
| `confidence` | number | `0.0` to `1.0`. The protocol recommends `≥ 0.85` for `irreversible` endpoints. |
| `impact` | string | `informational`, `reversible`, `irreversible` |
| `is_idempotent` | boolean | author's declaration |

### `[[endpoint.input.required]]` / `[[endpoint.input.optional]]`

Repeated tables, one per input field.

| Key | Type | Required | Notes |
|---|---|---|---|
| `name` | string | **yes** | lowercase snake_case |
| `type` | string | **yes** | `string`, `integer`, `number`, `boolean`, `object`, `array` |
| `description` | string | **yes** | non-empty |
| `format` | string | optional | named hint (`date`, `date-time`, `email`, `uuid`, etc.). The registry treats it as documentation; downstream consumers may enforce it. |
| `enum` | array | optional | constrained-value list — the field value must match one entry |
| `schema` | inline table | optional | full JSON Schema for complex `object` / `array` types |

### `[[endpoint.output]]`

Repeated tables, one per output field. Same per-entry shape as inputs.

### `[endpoint.errors]`

A list of named error condition strings the endpoint declares. The
loader accepts two forms:

```toml
# preferred form (matches the prompt schema)
[endpoint.errors]
list = ["room_unavailable", "invalid_dates"]

# bare-array form
errors = ["room_unavailable", "invalid_dates"]
```

The strings are identifiers — operators choose them. They are
distinct from the HTTP / AGTP status codes the endpoint may
return; status codes belong on the response, error names belong on
the contract.

### `[endpoint.handler]`

Per ``agtp-api §9`` each binding type uses its own type-specific
reference field rather than a generic ``reference``:

| Key | Type | Required | Notes |
|---|---|---|---|
| `type` | string | **yes** | `registered_function`, `composition`, `external_service` |
| `function` | string | yes (registered_function) | Python dotted path |
| `recipe` | string | yes (composition) | recipe name in the synthesis runtime |
| `url` | string | yes (external_service) | HTTPS URL the server proxies to |

Handler types:

* **`registered_function`** — `function` is a Python dotted path
  (e.g. `staybeta.handlers.book_room`). The server imports the
  module and calls the named callable with the standard
  `(EndpointContext)` signature documented in
  [`docs/handler-api.md`](handler-api.md).
* **`composition`** — `recipe` names a recipe in the synthesis
  runtime. The dispatcher routes the call to the runtime, which
  threads parameters through the recipe's plan. See
  [`docs/composition-handlers.md`](composition-handlers.md).
* **`external_service`** — `url` is an HTTPS URL the server proxies
  to. Plaintext (`http://`) is refused at registration. See
  [`docs/external-service-handlers.md`](external-service-handlers.md)
  for the full field list (`method`, `headers`, `input_transform`,
  `output_transform`, `error_map`, `timeout_seconds`).

> **Pre-§9 back-compat:** the loader still accepts the generic
> ``reference`` field on any binding type, plus ``input_map`` /
> ``output_map`` on `external_service`. Reading those names emits
> a deprecation warning naming the §9 replacement. The fallbacks
> will be removed in a future release; new TOMLs should use the
> §9 names.

## Loader behavior

* The loader scans the directory non-recursively, processing
  `*.toml` files in sorted order.
* One file → one endpoint. Multiple endpoints per file is not
  supported (it complicates LoadError attribution).
* Malformed files don't crash the loader. They produce a single
  [`LoadError`](../server/endpoint_loader.py) with `error_type =
  "parse"` (TOML couldn't parse), `"validation"` (parsed but
  failed a registry rule), or `"io"` (couldn't open / read).
* Validation `LoadError`s carry the same `detail` tag the registry
  raises (`semantic-missing-field:capability`,
  `path-grammar:verb-in-path`, etc.). Callers can branch on
  `detail` rather than parsing `message`.

## Worked examples

See the [`endpoints/` directory](../endpoints/) for three
documentation-grade samples:

* [`query_catalog.toml`](../endpoints/query_catalog.toml) — a
  simple endpoint using an embedded verb (`QUERY`) on `/catalog`.
* [`book_room.toml`](../endpoints/book_room.toml) — a custom-verb
  endpoint with the full contract.
* [`audit_summary.toml`](../endpoints/audit_summary.toml) — an
  endpoint whose handler binds to a synthesis recipe at
  `/reviews/{subject_id}`.
