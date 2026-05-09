# AMG (Agent Method Grammar) — client side

The validation + composition layer for AGTP method declarations.
This is the **client-side** implementation; the server has its own
parallel copy at [`server/amg/`](../../server/amg/), kept in sync
by [`tests/test_amg_drift.py`](../../tests/test_amg_drift.py).
The two trees are intentionally parallel implementations of the
same spec, following the SMTP MTA client/server pattern.

## Two halves

- **Validator** (`validate`) — runs nine passes against any
  `AMGMethodSpec` and returns a structured `ValidationResult`. Wired
  into `server.methods.register_custom` and `server.methods.handle_propose`,
  so every custom-method registration and every PROPOSE proposal is
  gated.
- **Composer** (`compose_method`, `MethodBuilder`,
  `compose_from_dict` / `_yaml` / `_json`) — helps method authors
  build well-formed specs in the first place. Always calls the
  validator before returning; anything you receive from the composer
  has passed the gate.

The validator is the gatekeeper. The composer is the assembly tool
that puts a spec together and runs the gate before handing the spec
back. They share the same `AMGMethodSpec` data shape and the same
nine-pass contract.

## Three composition modes

All three converge on the same output: a validated `AMGMethodSpec`.

### Function-style

The fastest path for programmatic callers.

```python
from client.amg import compose_method

spec = compose_method(
    "EVALUATE",
    intent="Evaluates the input against a declared ruleset",
    actor="agent",
    outcome="A structured assessment with pass/fail per rule is returned",
    capability="analysis",
    confidence_guidance=0.75,
    impact_tier="informational",
    is_idempotent=True,
    namespace="acme-quality",
    required_params=[
        {"name": "input",   "type": "object", "description": "data to evaluate",
         "schema": {"type": "object"}},
        {"name": "ruleset", "type": "string", "description": "ruleset id"},
    ],
)
```

### Builder pattern

A fluent surface for incremental construction (Try-It forms, agent
authoring tools).

```python
from client.amg import MethodBuilder

spec = (MethodBuilder("RECONCILE")
        .with_intent("Reconciles transactions for the named account and period")
        .with_actor("agent")
        .with_outcome("A reconciliation summary listing matched and unmatched entries is returned")
        .with_capability("transaction")
        .with_idempotent(False)
        .with_impact_tier("reversible")
        .with_namespace("acme-finance")
        .with_required_param("account_id", "string", "the ledger account")
        .with_required_param("period",     "string", "time window like 2026-Q1")
        .with_optional_param("tolerance", "number", "rounding tolerance")
        .with_error_code(400).with_error_code(422).with_error_code(455)
        .build())
```

`MethodBuilder.preview()` returns the in-progress spec without running
validation, useful for incremental UIs that want to render a draft as
the user types. Don't publish the preview output without calling
`build()`.

### Document-form

For catalog files and CI pipelines.

```python
from client.amg import compose_from_yaml

spec = compose_from_yaml("methods/evaluate.method.yaml",
                        known_methods={"VALIDATE"})
```

`compose_from_dict`, `compose_from_json`, and `compose_from_yaml` all
take the same document shape (dataclass-style at the top level, AGIS
semantic block under `semantic`):

```yaml
name: EVALUATE
semantic:
  intent: Evaluates the input against a declared ruleset
  actor: agent
  outcome: A structured assessment with pass/fail per rule is returned
  capability: analysis
  confidence_guidance: 0.75
  impact_tier: informational
  is_idempotent: true
description: >-
  Run a ruleset against the supplied input and return an
  assessment listing which rules passed and which failed.
category: transact
required_params:
  - { name: input,   type: object, description: The data to evaluate,
      schema: { type: object } }
  - { name: ruleset, type: string, description: Identifier of the ruleset to apply }
optional_params:
  - { name: tolerance, type: number, description: Acceptable variance threshold }
error_codes: [400, 405, 422, 455]
source: amg/1.0
namespace: acme-quality
substitutes_for:
  - { target: VALIDATE, conditions: when ruleset is JSON Schema }
```

YAML support requires the optional `pyyaml` extra:

```bash
pip install -e ".[yaml]"
```

JSON works with no extras.

## Error model

`CompositionError` extends `ValueError` and carries the full
`ValidationResult`:

```python
from client.amg import compose_method, CompositionError

try:
    spec = compose_method("get", intent="...", actor="agent", outcome="...")
except CompositionError as e:
    print(e)                       # one-line summary
    print(e.validation_result)     # structured pass-by-pass detail
    for s in e.suggestions:
        print(" -", s)             # actionable hints from the suggestion engine
```

The composer raises `CompositionError` for two distinct families of
failures:

1. **Validator refusals** (lexical / reserved / stoplist / required-fields
   / description / parameters / schemas / substitution). The
   `validation_result` field is populated; `suggestions` includes hints
   produced by `suggest_fix()`.

2. **Composer-side coherence failures** that the validator does not
   own (missing semantic block on a custom method, contradictory
   idempotency, invalid actor/capability/impact_tier, confidence-guidance
   out of range). `validation_result` is `None` for these because
   validation never ran.

Soft warnings (irreversible methods with low confidence-guidance,
descriptions that match intent verbatim) flow through the
`suggestions` list on a successful spec — accessible via
`spec.__dict__.get("_composer_warnings", [])`.

## Suggestion engine

`suggest_fix(validation_result, attempted_name)` returns a list of
human-readable hints for fixing a failed composition. Common cases:

| Failure | Suggestion |
|---|---|
| `malformed-name` (lowercase) | "Try `RECONCILE` instead of `reconcile`." |
| `reserved-http-method` (GET) | "Consider an action verb like FETCH, RETRIEVE, QUERY." |
| `non-action-intent` (STATUS) | "Consider an action verb like CHECK or REPORT." |
| `description-too-short` | "Expand to at least 20 characters; describe what the method does and what it produces." |
| `missing-namespace` | "Add a namespace such as 'acme-finance'." |
| `error-codes-missing-422` | "Add 422 to error_codes." |

The engine reaches into `client.amg.substitution.DEFAULT_SUBSTITUTIONS`
to surface catalog candidates that share intent with the offending name.

## CLI: `agtp-amg compose`

After `pip install -e .`:

```bash
# Compose from a fixture file (YAML or JSON)
agtp-amg compose --from path/to/evaluate.method.yaml

# Compose with inline arguments
agtp-amg compose \
    --name EVALUATE \
    --intent "Evaluates the input against a declared ruleset" \
    --actor agent \
    --outcome "A structured assessment is returned" \
    --capability analysis \
    --no-idempotent \
    --required-param "input:object:The data to evaluate" \
    --required-param "ruleset:string:Identifier of the ruleset"

# YAML output (requires pyyaml)
agtp-amg compose --from path/to/method.yaml --output yaml

# Extend the known-methods set for substitution checks
agtp-amg compose --from method.yaml --known-methods extra.json
```

Exit codes: **0** on successful composition (spec printed to stdout),
**1** on composition failure (validation result + suggestions printed
to stderr), **2** on argument or I/O error.

The existing `agtp-amg validate` subcommand stays as-is. Bare-path
invocations (`agtp-amg path/to/file.json`) fall through to validate
for backward compatibility.

## A worked example: deliberate mistake → correction

Start with a wrong-shape declaration to see the composer's feedback
loop end to end.

```python
from client.amg import compose_method, CompositionError

# Attempt 1: lowercase name. The composer surfaces an uppercase
# suggestion before any wire traffic happens.
try:
    spec = compose_method(
        "evaluate",
        intent="Evaluates the input against a declared ruleset",
        actor="agent",
        outcome="A structured assessment is returned",
        capability="analysis",
        is_idempotent=True,
        namespace="acme-quality",
        required_params=[
            {"name": "input",   "type": "object",
             "description": "data to evaluate",
             "schema": {"type": "object"}},
            {"name": "ruleset", "type": "string",
             "description": "ruleset id"},
        ],
    )
except CompositionError as e:
    print(e.validation_result.error.code)     # malformed-name
    for s in e.suggestions:
        print(s)
    # Output:
    #   Method names must be uppercase ASCII. Try 'EVALUATE' instead of 'evaluate'.

# Attempt 2: applied the suggestion. Composition succeeds.
spec = compose_method(
    "EVALUATE",
    intent="Evaluates the input against a declared ruleset",
    actor="agent",
    outcome="A structured assessment is returned",
    capability="analysis",
    is_idempotent=True,
    namespace="acme-quality",
    required_params=[
        {"name": "input",   "type": "object",
         "description": "data to evaluate",
         "schema": {"type": "object"}},
        {"name": "ruleset", "type": "string",
         "description": "ruleset id"},
    ],
)
print(spec.name, spec.semantic.is_idempotent)
# EVALUATE True
```

In an interactive UI, the suggestion list drives a fix-it button: the
user clicks "Fix" and the form repopulates with the corrected name.

## Field reference

### `SemanticBlock` (AGIS semantic declaration)

| Field | Required | Type | Notes |
|---|---|---|---|
| `intent` | yes | str | Single sentence, agent-goal voice |
| `actor` | yes | str | One of `agent` / `user` / `system` |
| `outcome` | yes | str | Single sentence, post-condition voice |
| `capability` | no | str | `discovery` / `transaction` / `modification` / `retrieval` / `analysis` / `notification` |
| `confidence_guidance` | no | float | 0.0–1.0; `>=0.85` recommended for `irreversible` impact tier |
| `impact_tier` | no | str | `informational` / `reversible` / `irreversible` |
| `is_idempotent` | no | bool | Cross-checked against `AMGMethodSpec.idempotent` |
| `state_transition` | no | dict[str, str] | `field -> "[old] -> [new]"` |

### `AMGMethodSpec` (validator's view)

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | yes | str | Pass 1: `/^[A-Z]{3,32}$/` |
| `semantic_class` | yes | str | `action-intent` / `query-intent` / `protocol-mechanic` |
| `category` | yes | str | Free-form bucket (`transact`, `cognitive`, …) |
| `description` | yes | str | ≥ 20 chars, non-stub |
| `idempotent` | yes | bool | Protocol-level dispatcher hint |
| `state_modifying` | yes | bool | Protocol-level dispatcher hint |
| `required_params` | yes | List[ParamSpec] | Each ParamSpec lowercase snake_case + recognized type + non-empty description |
| `optional_params` | yes | List[ParamSpec] | Same shape |
| `error_codes` | yes | List[int] | Must include 422 |
| `source` | yes | str | `agtp/1.0` (embedded) or `amg/1.0` (custom) |
| `namespace` | conditional | str | Required when `source=amg/1.0`; forbidden when `source=agtp/1.0` |
| `substitutes_for` | no | List[SubstitutionHint] | Each target must be a known method |
| `semantic` | conditional | SemanticBlock | Required by composer when `source=amg/1.0`; optional for embedded |

## Future work

- **Server-side composer.** This module lives in `client/amg/`; the
  same code belongs in `server/amg/` per the SMTP MTA analogy. The
  follow-on prompt copies it across.
- **`--interactive` CLI.** A guided flow that prompts for each AGIS
  field with inline validation and suggestion replay. The CLI scaffold
  is in place; the prompt loop lands separately.
- **Elemen "Compose Method" panel.** A GUI surface that wires
  `MethodBuilder` to a form. The Try-It pane is the natural home.
- **Catalog publishing.** Once the composer produces a stable
  `*.method.json`, a follow-on pipeline submits it to the AMG
  catalog (the canonical extension point for `EMBEDDED_METHODS` and
  `STOPLIST` updates).
