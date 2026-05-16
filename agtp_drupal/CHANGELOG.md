# agtp-drupal changelog

The Drupal module that wires AGTP handlers into the Drupal service
container. Sits on top of [`agtp-php`](../agtp-php/) and
[`mod_php`](../mod_php/); same versioning rule.

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major version
(see
[`../docs/architecture/gateway-protocol-v1.md` §12.1](../docs/architecture/gateway-protocol-v1.md#121-when-v2-cuts)).
Minor bumps add features. Patch bumps fix bugs or improve docs.
Drupal core compatibility is declared in `agtp_drupal.info.yml`.

## [Unreleased]

### Added — M5 initial release

Initial Drupal module. Wires AGTP handler discovery into Drupal's
service container via a tagged-service collector.

- `Drupal\agtp_drupal\Registry\AgtpHandlerCollector` — iterates every
  service tagged `agtp.endpoint` and adopts it into the agtp-php
  `HandlerRegistry`.
- `drush agtp:serve --gateway-socket=...` — bootstraps Drupal,
  collects handlers, runs the gateway client. Includes SIGTERM/SIGINT
  graceful-shutdown handling for process supervisors.
- Module metadata: `agtp_drupal.info.yml`, `agtp_drupal.module`,
  `agtp_drupal.services.yml`, `composer.json`.
- Operator docs: [`README.md`](README.md),
  [`INSTALL.md`](INSTALL.md) (smallest-possible-installation
  walkthrough).

Public surface for site builders:

- Service tag: `agtp.endpoint` — tag your handler service with this
  and `AgtpHandlerCollector` picks it up at boot.
- Handler class shape: methods decorated with `#[AgtpEndpoint]`
  (from agtp-php). No base class required, no dependency on Drupal
  classes — handlers stay testable as plain functions via
  `\Agtp\Testing`.
- Drush command: `agtp:serve` with `--gateway-socket`,
  `--module-id`, `--module-version` options.

### Known limits

- **No HTTP-pipeline integration.** AGTP traffic does not flow
  through Drupal's request handler; `agtpd` answers AGTP on TCP/4480
  and the gateway worker is a separate process.
- **No admin UI for registered endpoints.** A future ConfigEntity
  could surface them in `/admin/config/services/agtp`; not in v1.
- **No automatic worker supervision.** Operators wrap
  `drush agtp:serve` in systemd / Supervisor / Kubernetes. The
  drush command is designed to be supervisor-friendly (clean
  signal handling, exits on socket close).
- **No Drush-less invocation.** The runtime entry point is the
  drush command. Sites that don't ship Drush can fall back to
  `php mod_php/bin/run.php` with a custom bootstrap that bootstraps
  Drupal manually, but agtp_drupal doesn't ship that bootstrap
  out of the box.
