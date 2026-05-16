# agtp-php

The PHP handler-author library for AGTP. Pairs with `mod_php` (the
runtime module that connects to `agtpd` over the gateway socket).

The public API mirrors the Python library [`agtp`](../agtp/) so
authoring is identical across languages: three value classes, one
attribute, one registry.

## Install

```bash
composer require agtp/agtp-php
```

Minimum PHP version: **8.1** (matches Drupal 10's floor).

## Writing a handler

Three idioms; pick the one that fits your codebase.

### 1. Class with attributed methods (Drupal / Symfony / Laravel)

```php
use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;

class RoomHandlers
{
    #[AgtpEndpoint(method: 'BOOK', path: '/room', errors: ['room_unavailable'])]
    public function book(EndpointContext $ctx): EndpointResponse|EndpointError
    {
        $type = $ctx->input['room_type'] ?? 'double';
        if ($type === 'presidential_suite') {
            return new EndpointError(
                code: 'room_unavailable',
                message: 'The presidential suite is not available.',
                details: ['room_type' => $type],
            );
        }
        return new EndpointResponse(body: [
            'reservation_id' => 'res-' . $ctx->input['guest'] . '-' . $type,
        ]);
    }
}

// Register at boot:
\Agtp\HandlerRegistry::default()->registerClass(RoomHandlers::class);
```

### 2. Global function with the same attribute

```php
use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointResponse;

#[AgtpEndpoint(method: 'QUERY', path: '/echo')]
function echoHandler(EndpointContext $ctx): EndpointResponse
{
    return new EndpointResponse(body: ['echo' => $ctx->input['value'] ?? '']);
}

\Agtp\HandlerRegistry::default()->registerFunction('echoHandler');
```

### 3. Functional registration with a closure

```php
\Agtp\HandlerRegistry::default()->register(
    method: 'QUERY',
    path: '/echo',
    handler: fn(\Agtp\EndpointContext $ctx) => new \Agtp\EndpointResponse(
        body: ['echo' => $ctx->input['value'] ?? '']
    ),
);
```

## Testing handlers

`Agtp\Testing` lets you exercise handlers as plain functions. No daemon,
no gateway socket.

```php
use Agtp\Testing;

public function testBookRoomSuccess(): void
{
    $ctx = Testing::makeContext(input: ['guest' => 'Chris', 'room_type' => 'double']);
    $response = Testing::assertOk((new RoomHandlers())->book($ctx));
    $this->assertSame('res-Chris-double', $response->body['reservation_id']);
}

public function testBookRoomDeclaredError(): void
{
    $ctx = Testing::makeContext(input: ['guest' => 'X', 'room_type' => 'presidential_suite']);
    $error = Testing::assertError(
        (new RoomHandlers())->book($ctx),
        code: 'room_unavailable',
    );
    $this->assertSame('presidential_suite', $error->details['room_type']);
}
```

## Running against `agtpd`

This library is only the authoring surface. To actually serve AGTP
traffic, run `agtpd` with a gateway socket and connect `mod_php`:

```bash
# Terminal 1: the daemon
python -m server 4480 \
    --agents-dir server/agents \
    --endpoints-dir endpoints \
    --gateway-socket /tmp/agtpd.sock

# Terminal 2: the PHP runtime module loading your handlers
php mod_php/bin/run.php \
    --gateway-socket /tmp/agtpd.sock \
    --bootstrap path/to/your/bootstrap.php
```

`bootstrap.php` is a small script that requires the autoloader and
registers your handler classes. See
[`samples/gateway_demo.php`](../samples/gateway_demo.php) for a
worked example.

## Public-API contract

The package versions independently of the AGTP wire format, the
method catalog, and the gateway protocol. The frozen public surface
is documented in [`CHANGELOG.md`](CHANGELOG.md) and validated against
the canonical JSON Schemas in [`../core/schemas/`](../core/schemas/).
Breaking changes wait for gateway protocol v2; additive minor bumps
land freely.

## Related

- [`docs/architecture/server-modules.md`](../docs/architecture/server-modules.md)
  — overall daemon/module/library architecture
- [`docs/architecture/gateway-protocol-v1.md`](../docs/architecture/gateway-protocol-v1.md)
  — wire-level contract between `agtpd` and `mod_php`
- [`agtp/`](../agtp/) — Python equivalent of this library
