# `agtp` — the AGTP CLI

The packaged ``agtp`` console script is a thin layer over
[`client.core_client`](../core_client.py): URI parsing, body
assembly, output formatting, exit codes, and the higher-level
flows (``--match-check``, ``--negotiate``, ``--propose``) live
here; the actual protocol work is delegated.

```text
agtp <uri>                        # DESCRIBE (default)
agtp <uri> QUERY --param a=1      # invoke a method
agtp <uri> --html                 # DESCRIBE -> browser
agtp <uri> --match-check          # run the matching handshake
agtp <uri> --propose --interactive  # interactive PROPOSE walkthrough
agtp <uri> --propose -d '{...}'   # PROPOSE with an inline body
agtp <uri> --propose --params-file path/to/method.yaml
```

## `--propose`: declaring an endpoint

The ``--propose`` flow walks the author through a (verb, path,
semantic block, parameters) tuple and submits it to the server.
Validators are cheap list lookups against the AGTP method catalog
([`core/methods.json`](../../core/methods.json)) and the path grammar
([`core/path_grammar.py`](../../core/path_grammar.py)).

Three entry shapes converge on a single submitted PROPOSE:

| Shape                                  | Use when                                |
|---|---|
| ``--propose --interactive`` (or ``-i``) | composing an endpoint from scratch in a terminal |
| ``--propose -d '<json>'``              | scripting a PROPOSE from a CI job |
| ``--propose --params-file FILE``       | submitting a hand-edited ``*.endpoint.yaml`` / ``*.endpoint.json`` |

The mode is enforced post-parse: ``--propose`` is mutually exclusive
with a positional method argument, and at least one of
``--interactive`` / ``-d`` / ``--params-file`` must be supplied.

### Interactive walkthrough

```text
$ agtp agtp://acme.example --propose -i

Compose an endpoint to propose to agtp://acme.example.
Press Ctrl-C at any prompt to abort.

Verb (one of the AGTP catalog):
> FROBNICATE
  ✗ 'FROBNICATE' is not in the AGTP verb catalog.

Verb (one of the AGTP catalog):
> RECONCILE
  ✓ Verb 'RECONCILE' is in the approved AGTP catalog.

Path (optional, e.g. /orders/{order_id}):
> /orders/{order_id}
  ✓ Path '/orders/{order_id}' accepted.

Intent (one sentence, agent-goal voice):
> Reconciles transactions for the named account
  ✓ Intent looks good.

[...]

──── Proposed Endpoint ──────────────────────────────────────
Verb:        RECONCILE
Path:        /orders/{order_id}
Intent:      Reconciles transactions for the named account
Actor:       agent
Outcome:     A reconciliation summary listing matched and unmatched entries is returned
Capability:  analysis
Namespace:   acme-finance
Parameters:
  Required:
    account_id:string ─ the ledger account
    period:string ─ time window like 2026-Q1
  Optional:
    tolerance:number ─ rounding tolerance
─────────────────────────────────────────────────────────────

Submit this PROPOSE to agtp://acme.example? (y/N/e to edit/s to save):
> y
```

Per-field validation runs as soon as a value is entered:

  * **Verb** — checked against
    [`core.methods.is_approved_verb`](../../core/methods.py); typos
    and legacy HTTP names get close-match suggestions from the
    catalog.
  * **Path** — checked against
    [`core.path_grammar.validate_path`](../../core/path_grammar.py);
    paths must begin with ``/``, must not have a trailing slash
    (except the root), must not embed a verb token in any segment.
  * **Intent / Outcome** — minimum 20 characters, single sentence.
  * **Actor / Capability** — must be from the recognized vocabulary
    (``agent / user / system`` and the seven capability buckets).
  * **Parameter triples** — ``name:type:description``; names are
    lowercase snake_case, types from the recognized primitive set.

Edit-mode re-enters the walkthrough with every prior value
preserved as the default — pressing Enter keeps the existing value.

The four post-preview options:

  * **y / yes** — submit the PROPOSE and render the response.
  * **n / no** (or empty) — discard and exit.
  * **e / edit** — re-enter the walkthrough with the current draft
    as the default for every prompt.
  * **s / save** — write the validated spec to a file (YAML by
    default, JSON if the path ends in ``.json``).

### Response handling

The CLI renders the three PROPOSE outcomes individually:

  * **200** Synthesis accepted — prints the synthesis ID,
    underlying target method, and a one-shot retry command.
  * **422** ``negotiation-refused`` — prints the reason and
    explanation, plus a pointer to ``--negotiate`` for automated
    alternatives.
  * **422** with ``counter_proposal`` body — prints the suggestion
    and prompts "Accept counter-proposal and re-invoke as
    ``<NAME>``? (y/N)".
    On ``y`` the CLI re-invokes against the suggested method with
    the original parameter shape.

### Body file shape

``--params-file`` accepts both JSON and YAML (extension-driven).
The body is the wire-shaped PROPOSE proposal:

```yaml
name: RECONCILE
path: /orders/{order_id}                # optional
parameters:
  account_id: string
  period: string
outcome: A reconciliation summary is returned
description: Reconcile transactions for the named account and period.
semantic:
  intent: Reconciles transactions for the named account
  actor: agent
  outcome: A reconciliation summary is returned
  capability: analysis
required_params:
  - { name: account_id, type: string, description: the ledger account }
  - { name: period,     type: string, description: time window like 2026-Q1 }
optional_params:
  - { name: tolerance,  type: number, description: rounding tolerance }
namespace: acme-finance
```

The verb name is checked against the catalog (``core/methods.json``)
and any path is checked against the path grammar before any wire
traffic. Local refusals exit 1 with an actionable message.

When ``--params-file *.yaml`` is supplied, ``pyyaml`` is required
(install via the ``[yaml]`` extra: ``pip install -e .[yaml]``).
``.json`` files work without extras.

### Exit codes

  * **0** — successful flow (200, save, or accepted counter).
  * **1** — server refusal (422 ``negotiation-refused``), declined counter, local validator
    refusal, or invocation error.
  * **2** — argparse / mutex error, malformed body, missing
    ``--params-file``, missing dependency.

## `--grammar-check`: probe a verb without committing

```bash
agtp <uri> RECONCILE --grammar-check
```

Probes whether a verb is in the AGTP catalog and whether the
server admits it. The Method-Grammar header pathway the protocol
previously shipped was retired; the catalog-based dispatcher gives
the same answer at the top of every request, so this flag is just
sugar — it lets the operator ask the question without committing
to a real invocation.

Two-stage check:

  1. **Local catalog** (`core/methods.json`). If the verb isn't
     locally recognized, refuse immediately with close-match
     suggestions — no network call.
  2. **Live probe** with an empty body. The status code carries the
     answer:

      * **200 / 400 / 422** — the verb passed every dispatcher gate
        and reached the handler. ``400 missing-required-params`` is
        the common case for a bodyless probe and is treated as
        proof of admission.
      * **459** Method Violation — the server's catalog (or
        ``policies.methods``) refused the name. Suggestions are
        printed when the body carries them.
      * **405 method-not-implemented** — the verb is in the AGTP
        catalog but no handler is registered on this server. On an
        interactive TTY, the CLI offers to chain into
        ``--propose --interactive`` so the full pathway runs from
        one command.
      * **405 method-not-allowed-by-policy** — the server's
        ``policies.methods`` actively disallows the verb. No PROPOSE
        chain is offered.
      * **403** Forbidden — the agent's capability or the server's
        policy declined.

The flag is mutually exclusive with ``--match-check``,
``--negotiate``, and ``--propose``.

## `--negotiate` (legacy fallback)

The pre-existing ``--negotiate`` flag is the *recovery* path: when an
ordinary method invocation returns 403 with a soft-deny error code
(``method-not-permitted-for-agent`` or ``wildcards-refused``), the CLI auto-issues a
PROPOSE and continues with the synthesis ID. ``--propose`` is the
*proactive* path: the user already knows they need a new method.
The two flags are not exclusive, but ``--negotiate`` is invisible to
the proactive flow because ``--propose`` short-circuits before the
fallback runs.

## Source map

| File | Role |
|---|---|
| [`main.py`](main.py) | argparse, ``run()``, ``--match-check``, ``--negotiate`` |
| [`propose.py`](propose.py) | ``--propose`` dispatch + interactive walkthrough |
| [`curl.py`](curl.py) | ``agtp-curl`` HTTPS-side helper |
| [`migrate.py`](migrate.py) | catalog migration helper |

The ``agtp`` console script is registered in
[`pyproject.toml`](../../pyproject.toml) and points at
``client.cli.main:main``.
