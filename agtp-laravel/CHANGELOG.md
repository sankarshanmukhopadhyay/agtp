# agtp-laravel changelog

The Laravel package that wires AGTP handlers into the Laravel
container. Sits on top of [`agtp-php`](../agtp-php/) and
[`mod_php`](../mod_php/).

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major version.
Minor bumps add features. Patch bumps fix bugs or improve docs.
Laravel version compatibility is declared in `composer.json`.

## [Unreleased]

### Added — M8 initial release

Initial Laravel package. Auto-discovered service provider; handler
discovery via container tags; serving via artisan.

- `Agtp\Laravel\AgtpServiceProvider` — auto-discovered service
  provider. Binds AgtpHandlerCollector as a singleton consuming
  the `agtp.endpoint` tag, registers the artisan command.
- `Agtp\Laravel\Registry\AgtpHandlerCollector` — populates the
  agtp-php registry at boot from tagged container bindings.
- `Agtp\Laravel\Console\AgtpServeCommand` — implements
  `php artisan agtp:serve --gateway-socket=...`. SIGTERM / SIGINT
  handlers for clean shutdown under systemd.

Public surface for application developers:

- Service tag: `agtp.endpoint`. Bind handler classes in your
  AppServiceProvider and call `$this->app->tag([Handler::class],
  'agtp.endpoint')`.
- Handler-class shape: methods decorated with `#[AgtpEndpoint]`
  (from agtp-php). Standard Laravel constructor DI works.
- Artisan command: `agtp:serve` with `--gateway-socket`,
  `--module-id`, `--module-version` options.

### Known limits

- Same set as `agtp_drupal` / `agtp-symfony`: no HTTP-middleware
  integration, no admin UI, no automatic worker supervision.
- No queue integration. Handlers run synchronously inside the
  gateway worker. Long-running operations should dispatch a queued
  job from inside the handler and return immediately.
