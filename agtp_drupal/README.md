# AGTP for Drupal

A Drupal module that exposes your site to the Agent Transfer Protocol
(AGTP). Site builders write handler classes the same way they'd write
Drupal services; AGTP traffic routes through them via the gateway
protocol.

This module pairs with two other packages:

- **[agtp-php](../agtp-php/)** — the language library that defines
  `EndpointContext`, `EndpointResponse`, `EndpointError`, and the
  `#[AgtpEndpoint]` attribute. Handler classes use these directly.
- **[mod_php](../mod_php/)** — the runtime that connects to `agtpd`
  over a gateway socket. The drush command in this module wraps it.

You do not run a separate `agtpd` daemon as part of Drupal — `agtpd`
is the AGTP server, you install it once on the host, and it listens
on TCP/4480 like Apache would on 80. This module is the Drupal-side
worker that connects to it.

## Requirements

- Drupal 10.2+ or Drupal 11
- PHP 8.1+
- `agtpd` running locally or on the same host (see the top-level
  [`README.md`](../README.md))
- Drush 12+

## Install

```bash
composer require agtp/agtp-drupal
drush en agtp_drupal
```

If you're working from the AGTP monorepo (not yet on Packagist),
configure path repositories in your site's `composer.json`:

```json
{
  "repositories": [
    { "type": "path", "url": "/absolute/path/to/agtp/agtp-php" },
    { "type": "path", "url": "/absolute/path/to/agtp/mod_php" },
    { "type": "path", "url": "/absolute/path/to/agtp/agtp_drupal" }
  ]
}
```

Then `composer require agtp/agtp-drupal:@dev` and enable normally.

## Writing a handler

Three files: a service registration, a handler class, and (optionally)
a custom module to hold them.

### 1. The handler class

A plain PHP class with one or more `#[AgtpEndpoint]`-decorated methods.
Use any Drupal services you need — they're injected through the
constructor.

```php
// web/modules/custom/example_agtp/src/Agtp/RoomHandlers.php
namespace Drupal\example_agtp\Agtp;

use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;
use Drupal\Core\Entity\EntityTypeManagerInterface;

final class RoomHandlers
{
    public function __construct(
        private readonly EntityTypeManagerInterface $entityTypeManager,
    ) {}

    #[AgtpEndpoint(
        method: 'BOOK',
        path: '/room',
        errors: ['room_unavailable'],
        requiredScopes: ['booking:write'],
    )]
    public function book(EndpointContext $ctx): EndpointResponse|EndpointError
    {
        $nodes = $this->entityTypeManager
            ->getStorage('node')
            ->loadByProperties([
                'type' => 'room',
                'field_room_type' => $ctx->input['room_type'] ?? 'double',
            ]);

        if ($nodes === []) {
            return new EndpointError(
                code: 'room_unavailable',
                message: 'No rooms of that type are bookable.',
                details: ['room_type' => $ctx->input['room_type'] ?? null],
            );
        }

        $node = reset($nodes);
        return new EndpointResponse(body: [
            'reservation_id' => 'res-' . $node->id() . '-' . $ctx->agentId,
            'room_id' => $node->id(),
        ]);
    }
}
```

### 2. The service registration

Tag the handler service with `agtp.endpoint`. The collector will pick
it up at boot.

```yaml
# web/modules/custom/example_agtp/example_agtp.services.yml
services:
  example_agtp.room_handlers:
    class: Drupal\example_agtp\Agtp\RoomHandlers
    arguments:
      - '@entity_type.manager'
    tags:
      - { name: agtp.endpoint }
```

### 3. The module info file

```yaml
# web/modules/custom/example_agtp/example_agtp.info.yml
name: Example AGTP handlers
type: module
package: AGTP
core_version_requirement: ^10.2 || ^11
dependencies:
  - agtp:agtp_drupal
```

Enable: `drush en example_agtp`.

## Running the worker

```bash
drush agtp:serve --gateway-socket=/var/run/agtpd/gateway.sock
```

What happens:

1. Drush bootstraps Drupal so the service container is built and your
   handler service is available.
2. `AgtpHandlerCollector` walks every service tagged `agtp.endpoint`
   and calls `HandlerRegistry::registerInstance()` on each, picking
   up every method decorated with `#[AgtpEndpoint]`.
3. A `GatewayClient` connects to the daemon, performs the handshake,
   receives the daemon's endpoint registration, and dispatches
   requests by looking up the registered handler.
4. The process serves until the daemon sends `goodbye` or the
   socket closes.

### Production deployment

Run the worker under a process supervisor:

```ini
# /etc/systemd/system/agtp-drupal.service
[Unit]
Description=AGTP for Drupal worker
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/var/www/example.com
ExecStart=/usr/bin/drush --root=/var/www/example.com/web agtp:serve --gateway-socket=/var/run/agtpd/gateway.sock
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

For higher request concurrency, run multiple worker units — `agtpd`
accepts multiple module connections and routes among them.

## Testing handlers

Use [`Agtp\Testing`](../agtp-php/README.md#testing-handlers) to
exercise handler methods directly. Build a synthetic
`EndpointContext`, call the method, assert on the result. No daemon,
no gateway socket, no AGTP traffic.

```php
public function testBookSuccess(): void
{
    $entityTypeManager = $this->createMock(EntityTypeManagerInterface::class);
    // ... stub entityTypeManager as needed ...
    $handler = new RoomHandlers($entityTypeManager);

    $ctx = Testing::makeContext(input: ['room_type' => 'double']);
    $response = Testing::assertOk($handler->book($ctx));
    $this->assertArrayHasKey('reservation_id', $response->body);
}
```

## What this module does not do

- **Does not serve AGTP traffic over Drupal's HTTP request pipeline.**
  AGTP runs on its own port (4480) via `agtpd`. Drupal answers HTTP
  on its usual port. The two protocols co-exist on the same host
  without interfering.
- **Does not expose handler endpoints to anonymous traffic.**
  Authentication happens at the `agtpd` layer (Agent-ID resolution
  and, when Agent-Cert lands, mTLS). Inside the handler,
  `$ctx->agentId` is the verified agent identity; trust it.
- **Does not provide a UI to author handlers.** Handlers are PHP
  code in your modules. A future configuration entity could surface
  registered endpoints in the admin UI, but the source of truth
  stays in code (where it belongs).

## Related

- [`docs/architecture/server-modules.md`](../docs/architecture/server-modules.md)
  — overall architecture
- [`docs/architecture/gateway-protocol-v1.md`](../docs/architecture/gateway-protocol-v1.md)
  — protocol between `agtpd` and `mod_php`
- [`agtp-php/`](../agtp-php/) — the underlying PHP library
- [`mod_php/`](../mod_php/) — the runtime module this wraps
