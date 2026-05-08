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

## `--propose`: declaring a new method

The ``--propose`` flow wires the [`AMG composer`](../amg/) into the
CLI. Three entry shapes converge on a single submitted PROPOSE:

| Shape                                  | Use when                                |
|---|---|
| ``--propose --interactive`` (or ``-i``) | composing a method from scratch in a terminal |
| ``--propose -d '<json>'``              | scripting a PROPOSE from a CI job |
| ``--propose --params-file FILE``       | submitting a hand-edited ``*.method.yaml`` / ``*.method.json`` |

The mode is enforced post-parse: ``--propose`` is mutually exclusive
with a positional method argument, and at least one of
``--interactive`` / ``-d`` / ``--params-file`` must be supplied.

### Interactive walkthrough

```text
$ agtp agtp://acme.example --propose -i

Compose a new method to propose to agtp://acme.example.
Press Ctrl-C at any prompt to abort.

Method name (uppercase, single token):
> evaluate
  ✗ 'evaluate' is not a valid method name (must be 3-32 uppercase ASCII letters).
    Try 'EVALUATE'.

Method name (uppercase, single token):
> EVALUATE
  ✓ Name passes lexical and stoplist checks.

Intent (one sentence, agent-goal voice):
> Evaluates the input against a declared ruleset
  ✓ Intent looks good.

[...]

──── Proposed Method ────────────────────────────────────────
Name:        EVALUATE
Intent:      Evaluates the input against a declared ruleset
Actor:       agent
Outcome:     A structured assessment with pass/fail per rule is returned
Capability:  analysis
Impact:      informational  (confidence floor 0.85)
Idempotent:  yes
Parameters:
  Required:
    input:string  ─  The data to evaluate
    ruleset:string  ─  Identifier of the ruleset
  Optional: (none)
Namespace:   acme-quality
Source:      amg/1.0
─────────────────────────────────────────────────────────────

Submit this PROPOSE to agtp://acme.example? (y/N/e to edit/s to save):
> y
```

Per-field validation surfaces feedback at the moment the value is
typed; the suggestion engine pulls from
[`client.amg.composer.suggest_fix`](../amg/composer.py). Edit-mode
re-enters the walkthrough with every prior value preserved as the
default — pressing Enter keeps the existing value.

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
  * **460** Refused — prints the reason and explanation, plus a
    pointer to ``--negotiate`` for automated alternatives.
  * **461** Counter-proposal — prints the suggestion and prompts
    "Accept counter-proposal and re-invoke as ``<NAME>``? (y/N)".
    On ``y`` the CLI re-invokes against the suggested method with
    the original parameter shape.

### Body file shape

``--params-file`` accepts both JSON and YAML (extension-driven).
The same shape that
[`client.amg.compose_from_dict`](../amg/composer.py) accepts:

```yaml
name: EVALUATE
semantic:
  intent: Evaluates the input against a declared ruleset
  actor: agent
  outcome: A structured assessment with pass/fail per rule is returned
  capability: analysis
  confidence_guidance: 0.85
  impact_tier: informational
  is_idempotent: true
description: Run a ruleset against the supplied input and report.
category: transact
required_params:
  - { name: input,   type: string, description: The data to evaluate }
  - { name: ruleset, type: string, description: Identifier of the ruleset }
error_codes: [400, 422]
source: amg/1.0
namespace: acme-quality
```

When ``--params-file *.yaml`` is supplied, ``pyyaml`` is required
(install via the ``[yaml]`` extra: ``pip install -e .[yaml]``).
``.json`` files work without extras.

### Exit codes

  * **0** — successful flow (200, save, or accepted counter).
  * **1** — server refusal (460), declined counter, local validator
    refusal, or invocation error.
  * **2** — argparse / mutex error, malformed body, missing
    ``--params-file``, missing dependency.

## `--negotiate` (legacy fallback)

The pre-existing ``--negotiate`` flag is the *recovery* path: when an
ordinary method invocation returns 452 / 462, the CLI auto-issues a
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
