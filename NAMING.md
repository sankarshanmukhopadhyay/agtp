# Naming conventions

This repo is a monorepo of products across five languages plus a
growing set of framework integrations. Several directory names look
inconsistent at first glance; almost all of those inconsistencies are
**forced** by the conventions of the language or framework involved,
not by sloppy authorship. This document is the reference for what's
spelled which way and *why*.

When in doubt: follow the table. When you can't follow the table
because a new language or framework forces a different shape, add a
row and an explanation here.

## The shape

```
/<role>-<language>      Cross-language packages with no language-imposed
                        identifier constraint. Hyphenated.

/<role>_<language>      Packages whose identifier IS the directory name
                        in their language (Python imports, Drupal
                        module machine names, etc.). Underscored.

/<no-suffix>            Packages where the language-canonical name is
                        just "agtp" (Python's `agtp` package).
```

## Layout

Top-level grouping makes role visible at a glance:

```
/sdk/           Handler libraries — what authors `import` from
/runtimes/      Gateway-protocol clients — one per language
/operational/   Daemon-side plugins — load via `--load-module`
/connectors/    Framework + cross-protocol bridges
/agtp/          Python SDK — at root because `import agtp` requires it
/core, /server, /client, /registry
                Daemon and shared protocol primitives
/docs, /ietf, /tests, /samples, /tools, /endpoints, /scripts
                Documentation, specs, dev support
```

## Concrete table

| Path                              | Role                          | Naming rule used                                  |
|-----------------------------------|-------------------------------|---------------------------------------------------|
| `agtp/`                           | Python handler library        | Forced: must be a valid Python identifier         |
| `sdk/agtp-go/`                    | Go handler library            | Convention: Go directories can be hyphenated      |
| `sdk/agtp-node/`                  | Node/TS handler library       | Convention: npm packages are hyphenated           |
| `sdk/agtp-rust/`                  | Rust handler library          | Convention: Cargo crates are hyphenated           |
| `runtimes/mod_python/`            | Python runtime module         | Forced: `import mod_python` requires underscore   |
| `runtimes/mod_go/`                | Go runtime module             | Underscore for symmetry with `mod_python`         |
| `runtimes/mod_node/`              | Node runtime module           | Underscore for symmetry with `mod_python`         |
| `runtimes/mod_rust/`              | Rust runtime module           | Underscore for symmetry with `mod_python`         |
| `operational/mod_cache/`          | Operational module: caching   | Forced: `import mod_cache` requires underscore    |
| `operational/mod_audit/`          | Operational module: audit log | Forced: `import mod_audit` requires underscore    |
| `operational/mod_proxy/`          | Operational module: AGTP proxy| Forced: `import mod_proxy` requires underscore    |
| `connectors/agtp-a2a/`            | A2A-on-AGTP connector         | Same template as agtp-mcp; lets A2A traffic ride on AGTP transport |

## External repos

Framework integrations and cross-protocol connectors live in their
own Git repositories. They're versioned independently of this spec
repo because their release cadence is tied to their package
registry (Packagist for PHP, npm for Node, etc.), not to the daemon.

A site that only wants WordPress doesn't pull the Drupal module, the
Symfony bundle, or the MCP connector. Each install footprint is what
the operator actually asked for.

| External repo | Contents | Published package |
|---------------|----------|--------------------|
| [`agtp-php`](https://github.com/nomoticai/agtp-php) | `agtp-php/` (handler SDK) and `mod_php/` (runtime module) | `agtp/agtp-php`, `agtp/mod-php` (Packagist) |
| [`agtp-drupal`](https://github.com/nomoticai/agtp-drupal) | Drupal module wrapping the PHP stack | `agtp/agtp-drupal` (Packagist) |
| [`agtp-symfony`](https://github.com/nomoticai/agtp-symfony) | Symfony bundle wrapping the PHP stack | `agtp/agtp-symfony` (Packagist) |
| [`agtp-laravel`](https://github.com/nomoticai/agtp-laravel) | Laravel package wrapping the PHP stack | `agtp/agtp-laravel` (Packagist) |
| [`agtp-wordpress`](https://github.com/nomoticai/agtp-wordpress) | WordPress plugin wrapping the PHP stack | `agtp/agtp-wordpress` (Packagist) |
| [`agtp-mcp`](https://github.com/nomoticai/agtp-mcp) | MCP-on-AGTP cross-protocol bridge | n/a (Python service, ships from source) |

`tests/test_gateway_e2e_php.py` in this spec repo exercises the
extracted PHP runtime over the gateway protocol; it discovers
`mod_php` either from `$AGTP_MOD_PHP_DIR` or from `../agtp-php/mod_php/`.

## Python import-path note

The runtime and operational modules live under `runtimes/` and
`operational/` on disk but appear as top-level Python packages
(`import mod_python`, `import mod_audit`). `pyproject.toml`'s
`[tool.setuptools.packages.find].where` lists all three roots so
setuptools registers each Python package at its canonical import
name regardless of where its source files actually live. This
preserves existing `import` statements and `agtpd --load-module
mod_audit` invocations across the layout change.

## The rules in plain English

### Handler libraries (`agtp-*`)

The handler-author-facing library for each language. Stable public API
across all five — three value classes (`EndpointContext`,
`EndpointResponse`, `EndpointError`), a registry, and testing helpers.

- **Python** is `agtp/` (no suffix) because `import agtp` is what
  handler authors type. Renaming the directory would break that.
- `agtp-go`, `agtp-node`, `agtp-rust` use hyphens — Cargo / npm /
  Go all accept hyphenated names.
- `agtp-php` lives in its own repo (see "PHP stack — external repos"
  above) alongside `mod_php`; both ship as Composer packages.

### Runtime modules (`mod_*` — language bridges)

The process that connects to `agtpd` over the gateway socket and
dispatches AGTP requests to handlers in its language. One per language.

- `mod_python/` is forced — `import mod_python` requires the
  underscore.
- `mod_go`, `mod_node`, `mod_rust` take the underscore for visual
  symmetry with `mod_python`, even though their languages would
  accept either.
- `mod_php/` lives in the external `agtp-php` repo and follows the
  same underscore convention.

If you write a `mod_<newlang>`, use the underscore unless the
language's own convention strongly forbids it.

### Operational modules (`mod_*` — daemon plugins)

Python packages loaded **into** the `agtpd` process via
`--load-module`. Unlike runtime modules, they do not speak the
gateway protocol — they extend the daemon's own behavior.

- `mod_cache/` — response caching for idempotent methods
- `mod_audit/` — append-only JSONL audit log
- `mod_proxy/` — forward AGTP requests to upstream `agtpd` instances

All are Python packages, all use the underscore (forced by Python
import rules). They share the `mod_` prefix with runtime modules
intentionally — both are "modules that extend agtpd"; the
in-process vs out-of-process split is a deployment detail, not a
naming concern. Disambiguate by referring to "runtime modules"
(language bridges) vs "operational modules" (daemon plugins) when
context matters.

### Framework integrations (`agtp_<framework>` or `agtp-<framework>`)

Sit on top of a handler library. They all live in external repos
(see "External repos" above) so each framework's user base only
pulls what they need:

- `agtp-wordpress`, `agtp-laravel`, `agtp-symfony` — Composer-published
  packages with hyphenated names.
- `agtp_drupal` — Drupal module; underscore is forced by Drupal's
  module-machine-name rule `^[a-z][a-z0-9_]*$`.

### Cross-protocol connectors (`agtp-<protocol>`)

Bridge AGTP to another protocol. Hyphenated by convention.

- `agtp-mcp` — bridges MCP into AGTP-hosted servers. Lives in its
  own repo (see "External repos").
- `connectors/agtp-a2a/` — bridges Google's A2A protocol. Still
  in-tree while the protocol surface stabilises; will likely move
  to its own repo once the API is stable.

## Composer / npm / Cargo / Go module names

The directory name and the published-package name can differ. Today's
mapping:

| Directory        | Published name (when published) |
|------------------|----------------------------------|
| `agtp/`          | `agtp` (PyPI)                    |
| `sdk/agtp-go/`   | `agtp.io/agtp-go` (Go module)    |
| `sdk/agtp-node/` | `@agtp/agtp-node` (npm)          |
| `sdk/agtp-rust/` | `agtp` (crates.io)               |
| `runtimes/mod_python/` | (ships inside the `agtp` PyPI distribution today) |
| `runtimes/mod_go/`   | `agtp.io/mod-go` (Go module)     |
| `runtimes/mod_node/` | `@agtp/mod-node` (npm)           |
| `runtimes/mod_rust/` | `mod_rust` (crates.io)           |

PHP packages (`agtp/agtp-php`, `agtp/mod-php`, `agtp/agtp-drupal`,
`agtp/agtp-symfony`) live in external repos — see "PHP stack —
external repos" above.

## When to break a rule

You can break a rule when the language or framework forces it. In
that case, add a row to the concrete table and a sentence explaining
the constraint. Don't break a rule for taste — if a new contributor
finds an exception that has no explanation here, they should treat
it as a bug.

## History: the flat-layout migration

Pre-Phase-D, the repo had every package at the top level (e.g.,
`/mod_php/`, `/agtp-php/`, `/mod_audit/`). After M9 + Phases A/B/C
the root had 33 entries, and the role of each (SDK vs runtime vs
operational vs connector) was only inferable from the README.

The migration moved every non-canonical package into one of four
role directories — `/sdk/`, `/runtimes/`, `/operational/`,
`/connectors/` — without renaming any package. Composer paths,
Cargo `path =` deps, Go `replace` directives, npm `file:` references,
and Python `pyproject.toml`'s `packages.find.where` all picked up
the new layout in one atomic commit. Published package names
(`agtp/agtp-php`, `@agtp/mod-node`, etc.) stayed identical because
those live in package manifests, not in directory names.
