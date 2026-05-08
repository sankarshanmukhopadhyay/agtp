# Server-side AMG

The server's implementation of the AMG (Agent Method Grammar)
specification. Intentionally parallel to [`client/amg/`](../../client/amg/) —
both are independent implementations of the same spec, following
the SMTP MTA client/server pattern.

## Sync invariant

The two trees are kept in sync by [`tests/test_amg_drift.py`](../../tests/test_amg_drift.py).
If a change to `client/amg/` is not also applied to `server/amg/`,
the drift tests fail in CI with the file or symbol named.

When intentional divergence is required (e.g., server-only
catalog-aware validation), add the divergent module or symbol to
the exception list in `test_amg_drift.py` with a comment explaining
why. The exception list starts empty by design.

## When this side of AMG is invoked

The server-side AMG package is wired into the runtime at three
points, all in `server/methods.py`:

- **`register_custom()`** — gate for custom-method registration.
  Every spec passed to `register_custom` runs through `validate()`
  before the method enters the dispatch registry.
- **`handle_propose()`** — gate for incoming PROPOSE bodies.
  Malformed proposals (lexical / reserved / stoplist / semantic
  class failures) are turned into 460 Negotiation Refused with
  `reason="ambiguous"` before the negotiation policy ever sees them.
- **Counter-proposal composition (461 responses)** — when the
  policy chooses to suggest an alternative, the server uses
  `compose_method` (or the `MethodBuilder`) to construct the
  alternative spec it returns to the client.

## When the client side is invoked

For reference, see [`client/amg/`](../../client/amg/). It's wired
into:

- Outbound PROPOSE construction in `client/cli/main.py`.
- Manifest validation in elemen's bridge.
- Developer authoring via the `agtp-amg` CLI.

## Module map

Same eight files as `client/amg/`. Byte-identical content modulo
the import-path prefix (`from server.amg.X` instead of
`from client.amg.X`). The drift suite enforces this.

| Module | Role |
|---|---|
| `grammar.py` | `AMGMethodSpec`, `ParamSpec`, `SemanticBlock`, `SubstitutionHint` |
| `reserved.py` | `HTTP_METHODS`, `EMBEDDED_METHODS`, `STOPLIST` + suggestion helpers |
| `validator.py` | The nine-pass validator + `ValidationResult` / `ValidationError` |
| `substitution.py` | `EquivalenceClass`, `find_substitutes`, `DEFAULT_SUBSTITUTIONS` |
| `synthesis.py` | `SynthesisContract` + `validate_synthesis` |
| `composer.py` | `compose_method`, `MethodBuilder`, `compose_from_*`, `CompositionError`, `suggest_fix` |
| `cli.py` | Server-side AMG driver with `validate` and `compose` subcommands |
| `__init__.py` | Public API (mirrors `client/amg/__init__.py` exactly) |

## CLI access

The packaged `agtp-amg` console script points at `client.amg.cli`
because the client side is what end-users (method authors,
developers) interact with. The server-side CLI is reachable only as
a module, intended for in-process use by a developer working in a
server environment::

    python -m server.amg.cli compose --from path/to/method.yaml
    python -m server.amg.cli validate path/to/method.json

There is no `agtp-server-amg` console script; use the module form.

## Future divergence path

After this prompt lands, the two AMG trees are maintained as
parallel implementations. If a future change requires server-only or
client-only logic:

1. Make the change in one tree.
2. Run `tests/test_amg_drift.py` — it fails with the divergence
   identified by file name (and / or symbol name).
3. Decide: replicate to the other tree, or add to the exception
   list?
4. If exception list: document the reason in a comment alongside
   the exception entry.

This keeps drift visible and intentional rather than accidental.
