# Synthesis runtime

The synthesis runtime is what makes PROPOSE genuinely instantiate
working methods. When an agent proposes a method that doesn't exist
in the server's registry, the runtime attempts to compose a handler
from existing primitives via a configurable composition policy. When
the proposal succeeds, the agent gets a `synthesis_id`; subsequent
invocations carrying `Synthesis-Id: <id>` execute the composed plan
through the same dispatcher every external invocation goes through.

## Composition policies

Policies plug in via the [`CompositionPolicy`](policies.py) protocol.
Two policies ship in the box:

| Policy | Role |
|---|---|
| [`RecipeBasedPolicy`](recipes.py) | Hand-authored synthesis recipes loaded from TOML. The default for production deployments. |
| [`PassthroughPolicy`](policies.py) | Single-step identity plan when the proposed name matches an existing method exactly. The runtime appends this automatically as the final fallback so v1 PROPOSE-on-exact-match keeps working. |

Configure the order in `agtp-server.toml`:

```toml
[synthesis]
policies     = ["recipes"]
recipes_file = "agtp-recipes.toml"
```

The runtime tries policies in declaration order; the first one to
return a [`SynthesisPlan`](plan.py) wins. Future deployments can add
capability-graph or LLM-driven policies by implementing the same
protocol.

## Recipes

Recipes are hand-authored in TOML. Each `[[recipe]]` block declares a
matching pattern and a sequence of steps the runtime executes when
the pattern matches an incoming proposal. Three sources fill each
step's parameters:

| Kind | Resolved at execution time as |
|---|---|
| `proposal` | `value` is the proposal-side parameter name; the value is read from the caller's request body |
| `constant` | `value` is a literal supplied in the recipe |
| `previous_step` | `value` is the captured-name string of an earlier step's output |

Steps may capture their output via `capture_as = "..."` so later steps
can reference it. The plan's `[recipe.aggregation].mode` controls
how outputs combine into the final response: `last` (default,
return only the final step's output), `merge` (shallow-merge all step
outputs into one object), or `list` (ordered list of all step outputs).

Starter recipes ship in [`server/agtp-recipes.toml`](../agtp-recipes.toml):

  * `EVALUATE` — `QUERY` then `SUMMARIZE` with output threading
    (canonical composition example).
  * `AUDIT` — `QUERY` + `SUMMARIZE` with merge aggregation.
  * `INSPECT` — `DISCOVER` + `DESCRIBE` with list aggregation.

## Plan execution

When a Synthesis-Id arrives, the dispatcher routes to
[`SynthesisRuntime.execute`](runtime.py). The runtime walks the plan's
steps in order:

1. Resolve each step's parameters from the three sources.
2. Build a fresh `AGTPRequest` for the step (the original auth-relevant
   headers carry over; `Synthesis-Id` is stripped so inner steps do
   not recurse).
3. Dispatch the step through the same `dispatch()` function the main
   request loop uses. **This is where authority preservation lives:**
   `check_capability`, scope checks, and the rest of dispatch all fire
   per step.
4. Capture the step's output if the step asked for it.
5. Aggregate per the plan's mode and return.

## Authority preservation

Every step is dispatched as if it were a direct external invocation.
Scope checks, capability checks, and the wildcards/policy chain all
fire against the same agent identity that made the original call. A
synthesis cannot launder authority — if the agent lacks scope for an
inner method, the synthesis fails with the underlying status code
and a structured body identifying the failed step.

When a step fails, the runtime returns the step's underlying status
code (so callers can branch on auth/scope/invocation) and surfaces a
body with `outcome: "error"`, `error.failed_step`, `error.method`,
and `error.captured_outputs` — the audit trail from prior steps that
already succeeded.

## Lifecycle

Syntheses are session-scoped and held in process memory. They expire
when:

  * The agent calls `SUSPEND` with the `synthesis_id` parameter.
  * The server restarts.

Persistent syntheses (surviving restart, possibly promoted into the
catalog as registered methods) are future work.

## Module map

| Module | Role |
|---|---|
| [`__init__.py`](__init__.py) | Public API surface (re-exports the types below) |
| [`errors.py`](errors.py) | `SynthesisError` (raised when a step fails at execution) |
| [`plan.py`](plan.py) | `ParameterSource`, `CompositionStep`, `SynthesisPlan` |
| [`policies.py`](policies.py) | `CompositionPolicy` protocol + `PassthroughPolicy` |
| [`recipes.py`](recipes.py) | `Recipe`, `RecipePattern`, `RecipeBasedPolicy`, TOML loader |
| [`runtime.py`](runtime.py) | `SynthesisRuntime` (the main class) + legacy `Synthesis` / `SynthesisRegistry` shims |

The legacy `server/synthesis_runtime.py` module is preserved as a
thin re-export shim so existing imports (`from server.synthesis_runtime
import SYNTHESES`) keep working.

## Future work

  * Capability-graph composition policy (auto-find compositions over
    the semantic block).
  * LLM-driven composition policy.
  * Persistent syntheses surviving restart.
  * Synthesis promotion to the catalog (a useful synthesized method
    gets added permanently).
  * Synthesis hot-reload (recipes file watched for changes).
  * Multi-server synthesis (one synthesis composed of methods on
    different servers).
