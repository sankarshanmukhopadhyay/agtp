<?php

declare(strict_types=1);

namespace Agtp;

/**
 * Per-request envelope handed to an AGTP handler.
 *
 * Mirrors agtp.handlers.EndpointContext in the Python reference
 * implementation and is validated against the canonical
 * core/schemas/endpoint-context.schema.json. Every field has already
 * been validated by agtpd before this envelope crosses the gateway:
 * input is schema-checked, agent_id is authenticated, authority_scope
 * is claim-validated.
 *
 * Properties are readonly (PHP 8.1+) so handlers cannot mutate the
 * incoming context. Defaults match the Python dataclass — empty
 * string for missing identifiers, empty array for missing list
 * fields, null for missing optional scalars.
 */
final class EndpointContext
{
    /**
     * @param array<string, mixed>  $input
     * @param list<string>          $agentScopes
     * @param list<string>          $authorityScope
     * @param array<string, string> $headers
     */
    public function __construct(
        public readonly array $input = [],
        public readonly string $agentId = '',
        public readonly string $principalId = '',
        public readonly array $agentScopes = [],
        public readonly array $authorityScope = [],
        public readonly ?string $sessionId = null,
        public readonly ?string $taskId = null,
        public readonly string $requestId = '',
        public readonly string $method = '',
        public readonly string $path = '/',
        public readonly array $headers = [],
    ) {
    }

    /**
     * Construct from the wire envelope produced by agtpd.
     *
     * Used by mod_php's gateway client; not part of the public
     * handler-author API.
     *
     * @param array<string, mixed> $envelope
     */
    public static function fromEnvelope(array $envelope): self
    {
        return new self(
            input: (array) ($envelope['input'] ?? []),
            agentId: (string) ($envelope['agent_id'] ?? ''),
            principalId: (string) ($envelope['principal_id'] ?? ''),
            agentScopes: array_map('strval', (array) ($envelope['agent_scopes'] ?? [])),
            authorityScope: array_map('strval', (array) ($envelope['authority_scope'] ?? [])),
            sessionId: isset($envelope['session_id']) ? (string) $envelope['session_id'] : null,
            taskId: isset($envelope['task_id']) ? (string) $envelope['task_id'] : null,
            requestId: (string) ($envelope['request_id'] ?? ''),
            method: strtoupper((string) ($envelope['method'] ?? '')),
            path: (string) ($envelope['path'] ?? '/'),
            headers: array_map('strval', (array) ($envelope['headers'] ?? [])),
        );
    }
}
