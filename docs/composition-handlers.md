# Composition handlers

Phase 3 wires the second of the three handler-binding kinds: **composition**. A composition-bound endpoint declares a recipe by name; the dispatcher routes incoming calls to the synthesis runtime, which walks the recipe's steps through the same dispatcher every external invocation goes through. The runtime already exists (it powers the runtime PROPOSE / Synthesis-Id flow); composition handlers are a thin adapter that bridges "an endpoint declares `handler.type = composition`" to "the runtime executes the named recipe at invocation time."

Composition-bound endpoints differ from the runtime PROPOSE-and-synthesize flow in three ways:

- They're **permanent** — registered at server startup, not negotiated per-session.
- They're **manifest-visible** — agents can see them via DISCOVER and address them by `(method, path)`.
- They're **execution is data, not code** — the recipe is a TOML declaration; promoting a useful runtime synthesis to a permanent endpoint is a TOML edit, not a Python rewrite.

## Authoring a recipe

Recipes live in [`server/agtp-recipes.toml`](../server/agtp-recipes.toml) (or wherever the synthesis runtime is configured to load from via `[synthesis]` in the server config). Each `[[recipe]]` block declares:

- a unique `name` (the string an endpoint binding references)
- a `description`
- a `pattern` block (used by the runtime PROPOSE flow; ignored when the recipe is bound directly to an endpoint)
- one or more `steps`, each naming a method and the parameters it should be called with
- an `aggregation` mode (`last` / `merge` / `list`) controlling how the final response is built from the captured step outputs

Example:

```toml
[[recipe]]
name = "audit-via-query-and-summarize"
description = "AUDIT = QUERY then SUMMARIZE; returns both step outputs merged."

[recipe.pattern]
name_exact = "AUDIT"

[[recipe.steps]]
method = "QUERY"
capture_as = "facts"

  [recipe.steps.parameters.intent]
  kind = "proposal"
  value = "subject"

[[recipe.steps]]
method = "SUMMARIZE"

  [recipe.steps.parameters.source]
  kind = "previous_step"
  value = "facts"

  [recipe.steps.parameters.length]
  kind = "proposal"
  value = "length"

[recipe.aggregation]
mode = "merge"
```

`ParameterSource` kinds:

- `proposal` — the value comes from the caller's request body, under the named key.
- `constant` — the value is the literal supplied here.
- `previous_step` — the value is the captured output of an earlier step (referenced by `capture_as`).

See [`server/agtp-recipes.toml`](../server/agtp-recipes.toml) for the shipped starter set and [`server/synthesis/recipes.py`](../server/synthesis/recipes.py) for the canonical loader.

## Referencing a recipe from an endpoint TOML

Set `handler.type = "composition"` and `handler.recipe` to the recipe name. The endpoint's `errors` list **must** include `"composition_failed"` — the resolver refuses to register a composition endpoint without it, since the handler returns that code whenever a recipe step fails.

> Pre-§9 deployments used the generic `handler.reference` field; the loader still accepts it with a deprecation warning naming `handler.recipe` as the replacement.

```toml
# endpoints/audit_summary.toml
[endpoint]
method = "AUDIT"
path = "/reviews/{subject_id}"
description = "Audits the named subject by querying then summarizing."

[endpoint.semantic]
intent = "Audit the named subject and return a structured assessment."
actor = "agent"
outcome = "An audit summary covering the subject's current state and any flags is returned."
capability = "analysis"
confidence = 0.80
impact = "informational"
is_idempotent = true

[[endpoint.input.required]]
name = "subject"
type = "string"
description = "The entity to audit."

[[endpoint.input.optional]]
name = "length"
type = "string"
description = "Desired summary length."
enum = ["short", "medium", "long"]

[[endpoint.output]]
name = "summary"
type = "string"
description = "Synthesized audit summary."

[endpoint.errors]
list = ["subject_not_found", "composition_failed"]

[endpoint.handler]
type = "composition"
recipe = "audit-via-query-and-summarize"
```

## What happens at invocation

1. The dispatcher resolves `(AUDIT, /reviews/{subject_id})` against the endpoint registry; the composition handler runs.
2. The handler validates the request body against `endpoint.input`. (Already done by the dispatcher.)
3. The handler checks `required_scopes`. (Already done by the dispatcher.)
4. The handler hands the captured `SynthesisPlan` (built once at registration), the synthetic AGTPRequest, the agent_doc, and the server_state to `runtime.execute_plan(...)`.
5. The runtime walks each step:
   - Resolves the step's parameter sources (proposal / constant / previous_step) into a step body.
   - Builds a fresh AGTPRequest carrying the **original agent's identity** in headers.
   - Dispatches the step through the same dispatcher external invocations go through. Authority gates fire — if the agent doesn't have permission for QUERY, that step returns 403.
   - Captures the step's output if `capture_as` is set, so later steps can reference it.
6. After every step succeeds, the runtime aggregates the captured outputs per the recipe's mode and returns a 200 with `{method, synthesis_id, outcome: "ok", output, steps}`.
7. The composition handler turns the runtime's response into either an `EndpointResponse(body=output)` (success) or `EndpointError(code="composition_failed", details=...)` (any step failed).
8. The dispatcher validates the success body against `endpoint.output`. Phase 2's validator runs unconditionally — handler bugs in your recipe surface immediately.

## Authority preservation

Every step is dispatched with the same agent identity as the original request. Three concrete consequences:

- The agent's `requires.methods` is checked against each step. A composition that calls `QUERY` requires the agent to declare `QUERY` (or be a wildcards agent on a server that admits wildcards).
- The agent's `scopes` flow through. If a step requires a scope the agent didn't declare, the step returns 455 / 403, and the composition handler returns `composition_failed` rather than silent success.
- The legacy `Method-Grammar` carve-outs do **not** apply: a composition step is a plain dispatcher call.

> **Note.** The composition handler's `required_scopes` (declared on the endpoint) are checked **before** the recipe runs. They cover access to the composition itself. The per-step authority enforcement covers access to the underlying primitives. Both gates fire — an agent must satisfy both.

## Error contract

| Failure | Wire response | Body shape |
|---|---|---|
| Recipe step returns 4xx/5xx | 422 | `EndpointError` translated by the dispatcher: `{error: {code: "composition_failed", method, path, message, details: {recipe, failed_step, step_method, underlying_status, underlying, captured_outputs}}}` |
| Recipe step references an undefined `previous_step` capture | 500 | `synthesis-bad-reference` from the runtime, surfaced as `composition_failed` with `failed_step` |
| Resolution-time failure (recipe not found, step method missing, `composition_failed` not declared on the endpoint) | (no startup) | `InvalidHandlerError` logged at boot; the endpoint is skipped |

## Promoting a runtime synthesis

The promotion workflow:

1. The runtime PROPOSE flow accepts a synthesis (`audit-via-query-and-summarize` matches `AUDIT`-shaped proposals).
2. The synthesis_id agent is using is ephemeral — it disappears on `SUSPEND` or process restart.
3. To make the composition permanent, an operator copies the recipe into the server's `agtp-recipes.toml` (if it isn't already there), then writes an endpoint TOML that names it. After server restart, the composition is in the manifest, addressable by `(method, path)`, and runs without PROPOSE round-trips.

## Resolution-time checks

`resolve_composition` (in [`server/handler_resolution.py`](../server/handler_resolution.py)) catches misconfigurations at server startup so they don't surface as confusing 500s on first traffic. Stable detail tags:

| Detail | Cause |
|---|---|
| `recipe-not-found` | `handler.recipe` doesn't match any loaded recipe. |
| `recipe-step-method-missing` | A step's method isn't registered on this server. |
| `runtime-not-configured` | `server_state.synthesis_runtime` is `None`. |
| `composition-needs-spec` | `resolve_handler` was called for a composition binding without a spec (programmer error). |
| `composition-missing-error-code` | The endpoint's `errors` list doesn't include `"composition_failed"`. |
| `empty-reference` | `handler.recipe` is empty. |

The boot sequence ([`server.main.AgentRegistry.configure_endpoints`](../server/main.py)) catches all `InvalidHandlerError` and `NotImplementedError` raises, logs them prominently, and skips the offending endpoint. The rest of the directory continues to load.

## Manifest exposure

Composition endpoints surface in the manifest's `endpoints` array with the `handler.type` set to `"composition"`. The recipe name (the `handler.recipe` field) is **not** exposed — that's implementation detail. Agents see the binding kind so they can reason about expected latency and behavior:

```json
{
  "method": "AUDIT",
  "path": "/reviews/{subject_id}",
  "description": "...",
  "input": {"required": [...], "optional": [...]},
  "output": [...],
  "errors": ["subject_not_found", "composition_failed"],
  "semantic": {...},
  "handler_type": "composition"
}
```
