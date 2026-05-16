# agtp-wordpress changelog

The WordPress plugin that wires AGTP handlers into WordPress. Sits on
top of [`agtp-php`](../agtp-php/) and [`mod_php`](../mod_php/).

## Versioning

Major bumps coordinate with the AGTP gateway protocol's major version.
Minor bumps add features. Patch bumps fix bugs or improve docs.
WordPress core compatibility is declared in the plugin header.

## [Unreleased]

### Added — M8 initial release

Initial WordPress plugin. Handler discovery via WordPress action /
filter hooks; serving via WP-CLI.

- `agtp-wordpress.php` — plugin entry. Loads Composer autoloader,
  fires `init` at priority 5 to give plugins a moment to register,
  then registers the WP-CLI command.
- `Agtp\WordPress\AgtpCliCommand` — implements `wp agtp serve
  --gateway-socket=...`. Supports SIGTERM / SIGINT for clean
  shutdown under systemd.

Public surface for plugin developers:

- Filter `agtp_register_handlers`: return a list of handler class
  names; the plugin will `registerClass()` each.
- Action `agtp_init`: fires after the filter runs, before WP-CLI
  commands. Use this when handlers need constructor arguments.
- The handler-class shape is whatever `agtp-php` supports
  (`#[AgtpEndpoint]` on public methods).

### Known limits

- No HTTP-pipeline integration. AGTP traffic does not flow through
  WordPress's request handler; `agtpd` answers AGTP on TCP/4480 and
  the gateway worker is a separate process.
- No admin UI for registered endpoints. Handlers are PHP code in
  your plugins.
- No WordPress-multisite-specific handling. The plugin works on
  single-site and multisite installs equivalently; multisite-
  specific concerns (per-blog handler registries) wait for a real
  use case.
