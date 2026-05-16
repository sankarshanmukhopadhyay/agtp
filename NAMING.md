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

## Concrete table

| Path                | Role                          | Naming rule used                                  |
|---------------------|-------------------------------|---------------------------------------------------|
| `agtp/`             | Python handler library        | Forced: must be a valid Python identifier         |
| `agtp-go/`          | Go handler library            | Convention: Go directories can be hyphenated      |
| `agtp-node/`        | Node/TS handler library       | Convention: npm packages are hyphenated           |
| `agtp-php/`         | PHP handler library           | Convention: Composer packages are hyphenated      |
| `agtp-rust/`        | Rust handler library          | Convention: Cargo crates are hyphenated           |
| `mod_python/`       | Python runtime module         | Forced: `import mod_python` requires underscore   |
| `mod_go/`           | Go runtime module             | Underscore for symmetry with `mod_python`         |
| `mod_node/`         | Node runtime module           | Underscore for symmetry with `mod_python`         |
| `mod_php/`          | PHP runtime module            | Underscore for symmetry with `mod_python`         |
| `mod_rust/`         | Rust runtime module           | Underscore for symmetry with `mod_python`         |
| `agtp_drupal/`      | Drupal framework integration  | Forced: Drupal module machine names `^[a-z_]+$`   |
| `agtp-wordpress/`   | WordPress plugin              | Convention: WP plugin slug directories are hyphenated |
| `agtp-symfony/`     | Symfony bundle                | Convention: Composer-published bundles are hyphenated |
| `agtp-laravel/`     | Laravel package               | Convention: Composer-published Laravel packages are hyphenated |
| `agtp-mcp/`         | MCP-on-AGTP connector         | Convention: hyphenated like the rest of the agtp-X family |

## The rules in plain English

### Handler libraries (`agtp-*`)

The handler-author-facing library for each language. Stable public API
across all five — three value classes (`EndpointContext`,
`EndpointResponse`, `EndpointError`), a registry, and testing helpers.

- **Python** is `agtp/` (no suffix) because `import agtp` is what
  handler authors type. Renaming the directory would break that.
- The other four are `agtp-go`, `agtp-node`, `agtp-php`, `agtp-rust`
  because Cargo / npm / Composer / Go all accept hyphenated names
  and hyphens read more naturally in published-package contexts.

### Runtime modules (`mod_*`)

The process that connects to `agtpd` over the gateway socket and
dispatches AGTP requests to handlers in its language. One per language.

- `mod_python/` is forced — `import mod_python` requires the
  underscore.
- The other four take the underscore for visual symmetry with
  `mod_python`, even though their languages would accept either.

If you write a `mod_<newlang>`, use the underscore unless the
language's own convention strongly forbids it.

### Framework integrations (`agtp_<framework>` or `agtp-<framework>`)

Sit on top of a handler library. The naming follows the framework's
convention:

- `agtp_drupal/` — Drupal modules MUST use `[a-z][a-z0-9_]*` for the
  directory name (matches the module machine name). The underscore is
  not a style choice.
- `agtp-wordpress/` — WordPress plugin slug directories accept hyphens.
- `agtp-symfony/`, `agtp-laravel/` — Symfony bundles and Laravel
  packages publish as Composer packages with hyphenated names.

### Cross-protocol connectors (`agtp-<protocol>`)

Bridge AGTP to another protocol (MCP, A2A, …). Hyphenated.

- `agtp-mcp/` — bridges MCP into AGTP-hosted servers.
- Future `agtp-a2a/` — would bridge Google's A2A protocol.

## Composer / npm / Cargo / Go module names

The directory name and the published-package name can differ. Today's
mapping:

| Directory        | Published name (when published) |
|------------------|----------------------------------|
| `agtp/`          | `agtp` (PyPI)                    |
| `agtp-go/`       | `agtp.io/agtp-go` (Go module)    |
| `agtp-node/`     | `@agtp/agtp-node` (npm)          |
| `agtp-php/`      | `agtp/agtp-php` (Packagist)      |
| `agtp-rust/`     | `agtp` (crates.io)               |
| `mod_python/`    | (ships inside the `agtp` PyPI distribution today) |
| `mod_go/`        | `agtp.io/mod-go` (Go module)     |
| `mod_node/`      | `@agtp/mod-node` (npm)           |
| `mod_php/`       | `agtp/mod-php` (Packagist)       |
| `mod_rust/`      | `mod_rust` (crates.io)           |
| `agtp_drupal/`   | `agtp/agtp-drupal` (Packagist; Composer package name can hyphenate even when the Drupal module name doesn't) |

## When to break a rule

You can break a rule when the language or framework forces it. In
that case, add a row to the concrete table and a sentence explaining
the constraint. Don't break a rule for taste — if a new contributor
finds an exception that has no explanation here, they should treat
it as a bug.

## Subdirectory grouping (deferred)

A future "Layout v1.0" cleanup will likely group these into
subdirectories — e.g., `/sdk/`, `/modules/`, `/connectors/` — but
that change is destabilizing for every cross-package reference
(Composer path repositories, Cargo `path =` deps, Go `replace`
directives, npm `file:` references, Python `pyproject.toml`
patterns). It's queued to land alongside other planned breaking
changes (e.g., removing the legacy in-process handler resolution
path documented in
[`docs/architecture/server-modules.md`](docs/architecture/server-modules.md)).

Until then: the top-level layout is flat, the conventions in this
document apply, and adding a new language or framework means a new
top-level directory.
