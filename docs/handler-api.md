# Handler API (`agtp.handlers`)

The minimal, stable surface a developer imports from when binding
a Python function to an AGTP endpoint. Phase 2 introduces the API;
Phases 3 and 4 extend the *bindings* (composition recipes,
external services) but keep this surface unchanged.

```python
from agtp.handlers import EndpointContext, EndpointResponse, EndpointError

def book_room(ctx: EndpointContext):
    if ctx.input["room_type"] not in AVAILABLE:
        return EndpointError(
            code="room_unavailable",
            message="The requested room type is not available.",
            details={"room_type": ctx.input["room_type"]},
        )
    reservation_id = create_reservation(ctx.input)
    return EndpointResponse(body={"reservation_id": reservation_id})
```

## The signature

A handler is any callable matching:

```python
def handler(ctx: EndpointContext) -> EndpointResponse | EndpointError
```

The dispatcher does the heavy lifting around the call:

1. Parses and JSON-decodes the request body.
2. Validates it against the endpoint's input schema.
3. Checks the agent's declared scopes against
   `required_scopes`.
4. Builds the `EndpointContext` and calls the handler.
5. Validates the returned `EndpointResponse.body` against the
   endpoint's output schema (Phase 2 runs this unconditionally so
   handler bugs surface immediately).
6. Translates the result into an AGTP wire response.

Anything the handler raises (other than the structured
`EndpointError` return) becomes a 500 with the exception logged.
Use `EndpointError` for the failure modes the endpoint contract
declared; use `raise` for surprises.

## `EndpointContext`

| Field | Type | Notes |
|---|---|---|
| `input` | `dict` | Validated request body. Required fields are guaranteed present; optional fields may be absent. Types match the endpoint's input schema. |
| `agent_id` | `str` | The invoking agent's identity (from `Agent-ID` — legacy `Target-Agent` — or the URI). May be empty for server-level probes. |
| `authority_scope` | `List[str]` | The §10 `Authority-Scope` header value: scopes the agent claims for this specific request. Pre-validated by the dispatcher against the agent's declared scope set. Empty list when absent. |
| `session_id` | `Optional[str]` | The §10 `Session-ID` header — opaque operational grouping. Passed through without server-side interpretation. |
| `task_id` | `Optional[str]` | The §10 `Task-ID` header — task tracing across requests. The dispatcher echoes this in the response automatically; handlers can read it for log correlation. |
| `agent_scopes` | `list[str]` | Scopes the calling agent declared. Surfaced for finer-grained checks; the dispatcher already enforced `required_scopes`. |
| `server_state` | `Any` | Opaque reference to the server's runtime state (registry, runtime). Reserved for advanced handlers; most don't need it. |
| `request_id` | `str` | Correlation id matching the response's `Request-Id` header. Use it for log correlation. |
| `method` | `str` | The AGTP verb (uppercase). |
| `path` | `str` | The URI path the request targeted. |
| `headers` | `dict[str, str]` | Request headers, lowercased keys. Reserved for handlers that need access to e.g. `Idempotency-Key` or `Trace-Parent`. |

## `EndpointResponse`

| Field | Type | Default | Notes |
|---|---|---|---|
| `body` | `dict` | required | Response payload. The dispatcher validates against the output schema. |
| `status` | `int` | `200` | HTTP / AGTP status code for the response line. Most endpoints use the default; `201`, `202`, `204` are common alternatives. |
| `headers` | `dict[str, str]` or `None` | `None` | Optional response headers added alongside `Content-Type` and `Content-Length`. |

## `EndpointError`

| Field | Type | Default | Notes |
|---|---|---|---|
| `code` | `str` | required | Must be one of the names in the endpoint's `errors` list. The dispatcher refuses undeclared codes (handler bug → 500). |
| `message` | `str` | required | Operator / agent facing prose. |
| `details` | `dict` or `None` | `None` | Optional structured detail. Must be JSON-serializable. |

When a handler returns `EndpointError`, the dispatcher emits a 422
response with body shape:

```json
{
  "error": {
    "code": "room_unavailable",
    "method": "BOOK",
    "path": "/room",
    "message": "The requested room type is not available.",
    "details": {"requested": "suite", "available": ["single", "double"]}
  }
}
```

## Handler binding kinds

The TOML declaration's `[endpoint.handler]` table picks the
binding kind. Phase 2 implements one; the others are stubbed with
clear errors that point at the right phase.

| Kind | Status | Reference |
|---|---|---|
| `registered_function` | **Phase 2** | Python dotted path. The server `import`s the module and calls the named callable. |
| `composition` | Phase 3 | Recipe name in the synthesis runtime. Composition handlers participate in PROPOSE — see [`docs/propose.md`](propose.md) for the §7 negotiation surface. |
| `external_service` | Phase 4 | HTTPS URL the server proxies to. Resolution stub raises `NotImplementedError`. |

See [`docs/endpoint-toml.md`](endpoint-toml.md) for the full TOML
schema and [`samples/handlers.py`](../samples/handlers.py) for
worked handler examples bound to the
[`endpoints/`](../endpoints/) samples.

## Worked example: end-to-end

`endpoints/book_room.toml`:

```toml
[endpoint]
method = "BOOK"
path = "/room"
description = "Books a room for the named guest at the named property."

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
description = "Guest profile identifier."
format = "uuid"

[[endpoint.input.required]]
name = "room_type"
type = "string"
description = "Room category."
enum = ["single", "double", "suite"]

[[endpoint.output]]
name = "reservation_id"
type = "string"
description = "Server-assigned handle."

[endpoint.errors]
list = ["room_unavailable"]

[endpoint.handler]
type = "registered_function"
function = "samples.handlers.book_room"
```

`samples/handlers.py`:

```python
from agtp.handlers import EndpointContext, EndpointError, EndpointResponse

AVAILABLE = {"single", "double"}

def book_room(ctx: EndpointContext):
    if ctx.input["room_type"] not in AVAILABLE:
        return EndpointError(
            code="room_unavailable",
            message=f"{ctx.input['room_type']!r} not available",
            details={"requested": ctx.input["room_type"]},
        )
    return EndpointResponse(body={"reservation_id": "res-001"})
```

Client invocation:

```bash
agtp agtp://server BOOK /room \
    -d '{"guest_id":"3f1...","room_type":"double"}'
```

Response (success):

```json
{
  "reservation_id": "res-001"
}
```

Response (typed error):

```json
{
  "error": {
    "code": "room_unavailable",
    "method": "BOOK",
    "path": "/room",
    "message": "'suite' not available",
    "details": {"requested": "suite"}
  }
}
```
