# AGTP method validation

The protocol's method vocabulary is the curated method list at
[`core/methods.json`](../core/methods.json). Validation reduces to two
list lookups:

1. **Method-name validation** — is this verb in the catalog (or
   admitted as a legacy HTTP method via the server's
   `[policies.methods]` config block)?
2. **Path validation** — does the request path begin with `/`,
   avoid trailing slashes, and not embed a verb token?

Failures return one of two new AGTP-specific status codes:

* **459 Method Grammar Violation** — verb is not in the catalog.
* **460 Endpoint Grammar Violation** — path violates path grammar.

This document describes the catalog and path-grammar surface.

## The catalog

`core/methods.json` carries:

* **`embedded`** — the 12 protocol primitives every AGTP server must
  answer to (`QUERY`, `DISCOVER`, `DESCRIBE`, `SUMMARIZE`, `PLAN`,
  `EXECUTE`, `DELEGATE`, `ESCALATE`, `CONFIRM`, `SUSPEND`, `PROPOSE`,
  `NOTIFY`).
* **`legacy`** — the 5 HTTP methods (`GET`, `POST`, `PUT`, `DELETE`,
  `PATCH`) plus their preferred AGTP-canonical replacements
  (`FETCH`, `CREATE`, `REPLACE`, `REMOVE`, `MODIFY` respectively).
  Servers admit legacy methods only by opt-in via the
  `[policies.methods]` block of `agtp-server.toml`.
* **`methods`** — the curated set of approved AGTP methods (~425
  entries) organized by category.
* **`categories`** — top-level category metadata (`discovery`,
  `retrieval`, `analysis`, `transaction`, `modification`, `creation`,
  `notification`, `mechanics`, `domain_spanning`).
* **`version`** — semver version of the catalog (e.g. `"1.0.0"`).
  See [Catalog evolution](catalog-evolution.md) for what each
  version bump means and how deprecation / removal flow through
  the runtime.
* Per-verb optional **`deprecated_in` / `removed_in` / `successor`**
  — a deprecated verb is still admitted by the dispatcher but
  rides an `AGTP-Catalog-Warning` advisory header so callers
  migrate. See [Catalog evolution](catalog-evolution.md#per-verb-deprecation).

The list is regenerated from a canonical Python source whenever you
edit it:

```bash
python scripts/build_methods.py
```

The build script merges duplicates (verbs that appear under multiple
categories), excludes the 5 legacy HTTP methods from the curated
set so they're legacy-only, and emits the JSON in canonical order:
embedded first, then alphabetical within each category.

## Lookup surface

Use [`core.methods`](../core/methods.py) from any layer:

```python
from core.methods import (
    is_approved_verb, is_legacy_verb, is_embedded_verb,
    categorize, describe, get_legacy_preferred,
    find_close_matches,
    APPROVED_VERBS, EMBEDDED_VERBS, LEGACY_VERBS,
)

is_approved_verb("RECONCILE")    # True — in the catalog
is_approved_verb("GET")          # False — legacy, not approved
is_approved_verb("FROBNICATE")   # False — not recognized

is_legacy_verb("GET")            # True
is_embedded_verb("QUERY")        # True

categorize("AUDIT")              # ['analysis', 'domain_spanning']
describe("EVALUATE")             # 'Compute the value of an expression...'
get_legacy_preferred("GET")      # 'FETCH'

find_close_matches("PROPOSEX")   # ['PROPOSE']  Levenshtein-2
find_close_matches("FETHC")      # ['FETCH']
```

`find_close_matches` is what the dispatcher feeds into the 459
response body so callers see actionable typo hints.

## Path grammar

[`core.path_grammar.validate_path`](../core/path_grammar.py) raises
`PathGrammarError` on:

* paths that don't begin with `/`,
* paths that end with `/` (except the root itself),
* paths whose segments contain an AGTP verb token after stripping
  `-` and `_` (e.g., `/fetch`, `/F-E-T-C-H`, `/orders/query`).

Parameterized segments (`{order_id}`, etc.) are exempt from the
verb-in-path check; their content is variable by definition.

The grammar deliberately doesn't enforce casing, kebab-vs-snake
conventions, segment depth, or parameter naming — operators are
trusted with those choices. The two checks above are the
load-bearing ones.

## Dispatcher gate order

The server's `dispatch()` runs gates in this order:

1. **Synthesis-Id** — route to the synthesis runtime if the header
   names an active synthesis.
2. **459 Method Grammar Violation** — verb not in the catalog and
   not legacy-opted-in.
3. **460 Endpoint Grammar Violation** — path violates path grammar.
4. **405 Method Not Allowed** — the server's `policies.methods`
   block refuses this verb.
5. **Redirect** — `policies.methods.redirects` rewrites
   `(method, path)` before dispatch.
6. **Registry lookup** — handler resolves and runs.

Embedded methods bypass the policy gate (4) so a mis-authored
`disallow` entry can't take a server off-protocol.

## Status codes

### 459 Method Grammar Violation

Returned when the dispatcher refuses an unrecognized verb. Body:

```json
{
  "error": {
    "code": "method-grammar-violation",
    "message": "'FROBNICATE' is not a recognized AGTP verb.",
    "method": "FROBNICATE",
    "suggestions": ["FETCH", "CREATE"]
  }
}
```

`suggestions` lists the top-3 close matches by Levenshtein
distance against the approved set. For legacy HTTP methods, the
preferred replacement leads the list (`GET → FETCH`).

### 460 Endpoint Grammar Violation

Returned when the dispatcher refuses a malformed path. Body:

```json
{
  "error": {
    "code": "endpoint-grammar-violation",
    "message": "Path segment 'fetch' contains a recognized AGTP verb. Verbs belong in the method, not the path.",
    "path": "/fetch/orders",
    "segment": "fetch"
  }
}
```

The `segment` field names the offending path segment when one
exists; structural failures (missing leading slash, trailing
slash) leave it absent.

