<?php

declare(strict_types=1);

namespace Agtp\Tests;

use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;
use Agtp\HandlerRegistry;
use Agtp\Testing;
use PHPUnit\Framework\TestCase;

/**
 * Tests the agtp-php registration surface. Mirrors
 * tests/test_agtp_registry.py from the Python library.
 */
final class HandlerRegistryTest extends TestCase
{
    protected function setUp(): void
    {
        HandlerRegistry::resetDefault();
    }

    public function testRegisterFunctionalApi(): void
    {
        $registry = HandlerRegistry::default();
        $entry = $registry->register(
            method: 'BOOK',
            path: '/room',
            handler: fn(EndpointContext $ctx) => new EndpointResponse(body: ['ok' => true]),
        );

        $this->assertSame('BOOK', $entry->method);
        $this->assertSame('/room', $entry->path);
        $this->assertSame(1, $registry->count());
        $this->assertNotNull($registry->lookup('BOOK', '/room'));
    }

    public function testRegisterClassWithAttributedMethods(): void
    {
        $registry = HandlerRegistry::default();
        $registry->registerClass(_TestRoomHandlers::class);

        $this->assertSame(2, $registry->count());
        $this->assertNotNull($registry->lookup('BOOK', '/room'));
        $this->assertNotNull($registry->lookup('QUERY', '/room'));
    }

    public function testRegisterFunctionWithAttribute(): void
    {
        require_once __DIR__ . '/_fixtures/decorated_function.php';
        $registry = HandlerRegistry::default();
        $entry = $registry->registerFunction(__NAMESPACE__ . '\\_decoratedEcho');
        $this->assertNotNull($entry);
        $this->assertSame('QUERY', $entry->method);
        $this->assertSame('/echo', $entry->path);
    }

    public function testDuplicateRegistrationThrows(): void
    {
        $registry = HandlerRegistry::default();
        $registry->register(
            method: 'BOOK',
            path: '/room',
            handler: fn() => new EndpointResponse(body: []),
        );
        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/already registered/');
        $registry->register(
            method: 'BOOK',
            path: '/room',
            handler: fn() => new EndpointResponse(body: []),
        );
    }

    public function testMethodNormalizedToUppercase(): void
    {
        $registry = HandlerRegistry::default();
        $registry->register(
            method: 'book',
            path: '/room',
            handler: fn() => new EndpointResponse(body: []),
        );
        $this->assertNotNull($registry->lookup('BOOK', '/room'));
        $this->assertNotNull($registry->lookup('book', '/room'));
    }

    public function testRegisteredHandlerCarriesContract(): void
    {
        $registry = HandlerRegistry::default();
        $entry = $registry->register(
            method: 'BOOK',
            path: '/room',
            handler: fn() => new EndpointResponse(body: []),
            errors: ['room_unavailable'],
            requiredScopes: ['booking:write'],
            description: 'Books a room.',
        );
        $this->assertSame(['room_unavailable'], $entry->errors);
        $this->assertSame(['booking:write'], $entry->requiredScopes);
        $this->assertSame('Books a room.', $entry->description);
    }

    public function testClearResetsRegistry(): void
    {
        $registry = HandlerRegistry::default();
        $registry->register(
            method: 'X',
            path: '/y',
            handler: fn() => new EndpointResponse(body: []),
        );
        $this->assertSame(1, $registry->count());
        $registry->clear();
        $this->assertSame(0, $registry->count());
    }

    public function testHandlerRoundTrip(): void
    {
        $registry = HandlerRegistry::default();
        $registry->registerClass(_TestRoomHandlers::class);

        $entry = $registry->lookup('BOOK', '/room');
        $this->assertNotNull($entry);

        // Success path.
        $okCtx = Testing::makeContext(input: ['room_type' => 'double', 'guest' => 'Chris']);
        $response = Testing::assertOk(($entry->handler)($okCtx));
        $this->assertSame('res-Chris-double', $response->body['reservation_id']);

        // Error path.
        $errCtx = Testing::makeContext(input: ['room_type' => 'presidential_suite']);
        $err = Testing::assertError(($entry->handler)($errCtx), code: 'room_unavailable');
        $this->assertSame(['room_type' => 'presidential_suite'], $err->details);
    }
}

/**
 * Fixture class used by registerClass tests. Lives alongside the test
 * class so the attribute reflection has something to find. Not part
 * of the agtp-php public API.
 */
final class _TestRoomHandlers
{
    #[AgtpEndpoint(method: 'BOOK', path: '/room', errors: ['room_unavailable'])]
    public function book(EndpointContext $ctx): EndpointResponse|EndpointError
    {
        $type = (string) ($ctx->input['room_type'] ?? 'double');
        if ($type === 'presidential_suite') {
            return new EndpointError(
                code: 'room_unavailable',
                message: 'unavailable',
                details: ['room_type' => $type],
            );
        }
        return new EndpointResponse(body: [
            'reservation_id' => 'res-' . ($ctx->input['guest'] ?? 'anon') . '-' . $type,
        ]);
    }

    #[AgtpEndpoint(method: 'QUERY', path: '/room')]
    public function listRooms(EndpointContext $ctx): EndpointResponse
    {
        return new EndpointResponse(body: ['rooms' => ['101', '102']]);
    }
}
