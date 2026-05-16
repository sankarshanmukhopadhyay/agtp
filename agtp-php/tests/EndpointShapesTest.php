<?php

declare(strict_types=1);

namespace Agtp\Tests;

use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;
use Agtp\Testing;
use PHPUnit\Framework\TestCase;

/**
 * Exercises the three value classes: EndpointContext, EndpointResponse,
 * EndpointError. These shapes must round-trip through the wire
 * envelope they carry over the gateway socket.
 */
final class EndpointShapesTest extends TestCase
{
    public function testEndpointContextFromEnvelope(): void
    {
        $envelope = [
            'method' => 'book',
            'path' => '/room',
            'agent_id' => 'abc123',
            'principal_id' => 'chris@example.com',
            'authority_scope' => ['booking:write'],
            'session_id' => 'sess-1',
            'task_id' => 'task-1',
            'request_id' => 'req-1',
            'headers' => ['agent-id' => 'abc123'],
            'input' => ['guest' => 'Chris'],
        ];
        $ctx = EndpointContext::fromEnvelope($envelope);

        $this->assertSame('BOOK', $ctx->method);  // uppercased
        $this->assertSame('/room', $ctx->path);
        $this->assertSame('abc123', $ctx->agentId);
        $this->assertSame('chris@example.com', $ctx->principalId);
        $this->assertSame(['booking:write'], $ctx->authorityScope);
        $this->assertSame('sess-1', $ctx->sessionId);
        $this->assertSame('task-1', $ctx->taskId);
        $this->assertSame('req-1', $ctx->requestId);
        $this->assertSame(['agent-id' => 'abc123'], $ctx->headers);
        $this->assertSame(['guest' => 'Chris'], $ctx->input);
    }

    public function testEndpointContextDefaults(): void
    {
        $ctx = new EndpointContext();
        $this->assertSame([], $ctx->input);
        $this->assertSame('', $ctx->agentId);
        $this->assertSame('', $ctx->principalId);
        $this->assertSame([], $ctx->agentScopes);
        $this->assertSame([], $ctx->authorityScope);
        $this->assertNull($ctx->sessionId);
        $this->assertNull($ctx->taskId);
        $this->assertSame('', $ctx->requestId);
        $this->assertSame('', $ctx->method);
        $this->assertSame('/', $ctx->path);
        $this->assertSame([], $ctx->headers);
    }

    public function testEndpointResponseEnvelope(): void
    {
        $response = new EndpointResponse(
            body: ['reservation_id' => 'res-1'],
            status: 201,
            headers: ['Idempotency-Key' => 'ik-1'],
        );
        $envelope = $response->toEnvelope();
        $this->assertSame(['reservation_id' => 'res-1'], $envelope['body']);
        $this->assertSame(201, $envelope['status']);
        $this->assertSame(['Idempotency-Key' => 'ik-1'], $envelope['headers']);
    }

    public function testEndpointResponseDefaultsExcludeHeaders(): void
    {
        $response = new EndpointResponse(body: ['ok' => true]);
        $envelope = $response->toEnvelope();
        $this->assertArrayNotHasKey('headers', $envelope);
        $this->assertSame(200, $envelope['status']);
    }

    public function testEndpointErrorEnvelope(): void
    {
        $error = new EndpointError(
            code: 'room_unavailable',
            message: 'no rooms',
            details: ['hotel' => 'Grand'],
        );
        $envelope = $error->toEnvelope();
        $this->assertArrayHasKey('endpoint_error', $envelope);
        $this->assertSame('room_unavailable', $envelope['endpoint_error']['code']);
        $this->assertSame('no rooms', $envelope['endpoint_error']['message']);
        $this->assertSame(['hotel' => 'Grand'], $envelope['endpoint_error']['details']);
    }

    public function testEndpointErrorWithoutDetails(): void
    {
        $error = new EndpointError(code: 'x', message: 'y');
        $envelope = $error->toEnvelope();
        $this->assertArrayNotHasKey('details', $envelope['endpoint_error']);
    }

    public function testTestingMakeContextDefaults(): void
    {
        $ctx = Testing::makeContext();
        $this->assertSame('QUERY', $ctx->method);
        $this->assertSame('/', $ctx->path);
        $this->assertSame('test-agent', $ctx->agentId);
        $this->assertSame([], $ctx->input);
    }

    public function testTestingAssertOk(): void
    {
        $response = new EndpointResponse(body: ['ok' => true]);
        $this->assertSame($response, Testing::assertOk($response));
    }

    public function testTestingAssertOkRaisesOnError(): void
    {
        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/EndpointError/');
        Testing::assertOk(new EndpointError(code: 'x', message: 'y'));
    }

    public function testTestingAssertErrorChecksCode(): void
    {
        $err = new EndpointError(code: 'room_unavailable', message: '');
        Testing::assertError($err, code: 'room_unavailable');

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/expected EndpointError code/');
        Testing::assertError($err, code: 'wrong');
    }
}
