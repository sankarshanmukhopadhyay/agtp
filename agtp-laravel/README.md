# AGTP for Laravel

A Laravel package that wires AGTP handlers into your Laravel app's
service container. Bind your handler classes via a service provider,
tag them with `agtp.endpoint`, and `php artisan agtp:serve` runs
the gateway worker.

Pairs with:
- [`agtp-php`](../agtp-php/) — the language library
- [`mod_php`](../mod_php/) — the runtime client (wrapped by the
  `agtp:serve` artisan command)

## Requirements

- Laravel 10 or 11
- PHP 8.1+
- `agtpd` running locally or on the same host

## Install

```bash
composer require agtp/agtp-laravel
```

Laravel auto-discovers the service provider via the
`extra.laravel.providers` declaration in this package's
`composer.json`. If you've disabled package auto-discovery, register
the provider manually in `bootstrap/providers.php` (Laravel 11) or
`config/app.php` (Laravel 10):

```php
Agtp\Laravel\AgtpServiceProvider::class,
```

## Writing a handler

### 1. The handler class

```php
namespace App\Agtp;

use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;
use App\Models\Room;

final class RoomHandlers
{
    #[AgtpEndpoint(
        method: 'BOOK',
        path: '/room',
        errors: ['room_unavailable'],
        requiredScopes: ['booking:write'],
    )]
    public function book(EndpointContext $ctx): EndpointResponse|EndpointError
    {
        $room = Room::where('type', $ctx->input['room_type'] ?? 'double')->first();
        if ($room === null) {
            return new EndpointError(
                code: 'room_unavailable',
                message: 'No rooms available.',
                details: ['room_type' => $ctx->input['room_type'] ?? null],
            );
        }
        return new EndpointResponse(body: [
            'reservation_id' => sprintf('res-%d-%s', $room->id, $ctx->agentId),
        ]);
    }
}
```

### 2. Tag the binding

In your application's `AppServiceProvider::register()`:

```php
use App\Agtp\RoomHandlers;

public function register(): void
{
    $this->app->singleton(RoomHandlers::class);
    $this->app->tag([RoomHandlers::class], 'agtp.endpoint');
}
```

Multiple handlers? Pass them all to `tag()`:

```php
$this->app->tag(
    [RoomHandlers::class, ReservationHandlers::class, GuestHandlers::class],
    'agtp.endpoint',
);
```

## Running the worker

```bash
php artisan agtp:serve --gateway-socket=/var/run/agtpd/gateway.sock
```

Production via systemd:

```ini
[Service]
Type=simple
User=www-data
WorkingDirectory=/var/www/example.com
ExecStart=/usr/bin/php artisan agtp:serve --gateway-socket=/var/run/agtpd/gateway.sock
Environment=APP_ENV=production
Restart=on-failure
RestartSec=5s
```

## Testing handlers

```php
use Agtp\Testing;

public function test_book_room_success(): void
{
    $handler = new RoomHandlers();

    $ctx = Testing::makeContext(input: ['room_type' => 'double']);
    $response = Testing::assertOk($handler->book($ctx));
    $this->assertArrayHasKey('reservation_id', $response->body);
}
```

The handler class isn't tied to Laravel at runtime — it works as a
pure function over `EndpointContext`. For tests that need Eloquent,
use Laravel's normal `RefreshDatabase` trait and resolve the handler
via the container.

## What this package does not do

- Does not route AGTP through Laravel's HTTP middleware.
- Does not expose handlers to anonymous traffic; authentication is
  `agtpd`'s responsibility.
- Does not provide a Nova / Filament / admin UI panel.

## Related

- [`docs/architecture/server-modules.md`](../docs/architecture/server-modules.md)
- [`agtp-php/`](../agtp-php/) — the underlying PHP library
- [`agtp-symfony/`](../agtp-symfony/) — equivalent for Symfony
- [`agtp_drupal/`](../agtp_drupal/) — equivalent for Drupal
