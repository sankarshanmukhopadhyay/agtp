<?php

declare(strict_types=1);

namespace Agtp;

/**
 * One entry in a HandlerRegistry: a (method, path) routing key paired
 * with the handler callable and its self-declared contract.
 *
 * Mirrors agtp.registry.RegisteredHandler in the Python library.
 */
final class RegisteredHandler
{
    /**
     * @param callable(EndpointContext): (EndpointResponse|EndpointError) $handler
     * @param list<string> $errors
     * @param list<string> $requiredScopes
     */
    public function __construct(
        public readonly string $method,
        public readonly string $path,
        /** @var callable */
        public readonly mixed $handler,
        public readonly array $errors = [],
        public readonly array $requiredScopes = [],
        public readonly string $description = '',
    ) {
    }
}
