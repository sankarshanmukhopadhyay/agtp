# agtp-symfony changelog

The Symfony bundle that wires AGTP handlers into the Symfony service
container. Sits on top of [`agtp-php`](../agtp-php/) and
[`mod_php`](../mod_php/).

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major version.
Minor bumps add features. Patch bumps fix bugs or improve docs.
Symfony version compatibility is declared in `composer.json`.

## [Unreleased]

### Added — M8 initial release

Initial Symfony bundle. Handler discovery via tagged services;
serving via Symfony Console.

- `Agtp\Symfony\AgtpBundle` — bundle entry point; registers the
  extension and the compiler pass.
- `Agtp\Symfony\DependencyInjection\AgtpExtension` — loads
  `config/services.yaml`.
- `Agtp\Symfony\DependencyInjection\AgtpHandlerPass` — compiler pass
  that collects every `agtp.endpoint`-tagged service into the
  AgtpHandlerCollector constructor as a tagged iterator.
- `Agtp\Symfony\Registry\AgtpHandlerCollector` — populates the
  agtp-php registry at boot from tagged services.
- `Agtp\Symfony\Command\AgtpServeCommand` — implements
  `bin/console agtp:serve --gateway-socket=...`. Tagged with
  `console.command`. SIGTERM / SIGINT handlers for clean shutdown.

Public surface for application developers:

- Service tag: `agtp.endpoint`. Tag a handler service with this and
  AgtpHandlerCollector picks it up at boot.
- Handler-class shape: methods decorated with `#[AgtpEndpoint]`
  (from agtp-php). Constructor DI works normally.
- Console command: `agtp:serve` with `--gateway-socket`,
  `--module-id`, `--module-version` options.

### Known limits

- Same set as `agtp_drupal`: no HTTP-kernel integration, no admin UI,
  no automatic worker supervision (use systemd).
- The compiler pass uses the simplest tagged-iterator pattern — no
  priority ordering or alias support. A future revision could allow
  priority-based ordering if a real use case shows up.
