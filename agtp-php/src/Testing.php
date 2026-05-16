<?php

declare(strict_types=1);

namespace Agtp;

use RuntimeException;

/**
 * Test helpers for AGTP handler authors.
 *
 * Mirrors agtp/testing.py — same surface, PHP idioms. Build a
 * synthetic EndpointContext, call the handler as a plain function,
 * assert on the EndpointResponse or EndpointError. No daemon, no
 * gateway socket, no Composer dependencies beyond the library
 * itself.
 *
 *     use Agtp\Testing;
 *     use Agtp\EndpointResponse;
 *
 *     public function testBookRoom(): void {
 *         $ctx = Testing::makeContext(input: ['room_type' => 'double']);
 *         $result = (new RoomHandlers())->book($ctx);
 *         $response = Testing::assertOk($result);
 *         $this->assertArrayHasKey('reservation_id', $response->body);
 *     }
 */
final class Testing
{
    /**
     * @param array<string, mixed>  $input
     * @param list<string>          $agentScopes
     * @param list<string>          $authorityScope
     * @param array<string, string> $headers
     */
    public static function makeContext(
        array $input = [],
        string $method = 'QUERY',
        string $path = '/',
        string $agentId = 'test-agent',
        string $principalId = '',
        array $agentScopes = [],
        array $authorityScope = [],
        ?string $sessionId = null,
        ?string $taskId = null,
        string $requestId = 'test-req-1',
        array $headers = [],
    ): EndpointContext {
        return new EndpointContext(
            input: $input,
            agentId: $agentId,
            principalId: $principalId,
            agentScopes: $agentScopes,
            authorityScope: $authorityScope,
            sessionId: $sessionId,
            taskId: $taskId,
            requestId: $requestId,
            method: strtoupper($method),
            path: $path,
            headers: $headers,
        );
    }

    /**
     * Assert $result is a success; return the EndpointResponse.
     */
    public static function assertOk(mixed $result): EndpointResponse
    {
        if ($result instanceof EndpointError) {
            throw new RuntimeException(
                "expected EndpointResponse, got EndpointError code={$result->code} message={$result->message}"
            );
        }
        if (!($result instanceof EndpointResponse)) {
            $type = is_object($result) ? $result::class : gettype($result);
            throw new RuntimeException("expected EndpointResponse, got {$type}");
        }
        return $result;
    }

    /**
     * Assert $result is a declared error; optionally match $code.
     */
    public static function assertError(mixed $result, ?string $code = null): EndpointError
    {
        if ($result instanceof EndpointResponse) {
            throw new RuntimeException(
                "expected EndpointError, got EndpointResponse status={$result->status}"
            );
        }
        if (!($result instanceof EndpointError)) {
            $type = is_object($result) ? $result::class : gettype($result);
            throw new RuntimeException("expected EndpointError, got {$type}");
        }
        if ($code !== null && $result->code !== $code) {
            throw new RuntimeException(
                "expected EndpointError code={$code}, got code={$result->code} message={$result->message}"
            );
        }
        return $result;
    }
}
