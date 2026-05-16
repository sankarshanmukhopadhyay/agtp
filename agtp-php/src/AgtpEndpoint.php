<?php

declare(strict_types=1);

namespace Agtp;

use Attribute;

/**
 * Mark a class method as an AGTP endpoint handler.
 *
 * The Drupal / Symfony / Laravel idiom for AGTP handlers in PHP. A
 * handler class declares one or more methods, each tagged with
 * #[AgtpEndpoint(...)] specifying the AGTP verb and path:
 *
 *     use Agtp\AgtpEndpoint;
 *     use Agtp\EndpointContext;
 *     use Agtp\EndpointResponse;
 *
 *     class RoomHandlers {
 *         #[AgtpEndpoint(method: 'BOOK', path: '/room', errors: ['room_unavailable'])]
 *         public function book(EndpointContext $ctx): EndpointResponse {
 *             return new EndpointResponse(body: ['reservation_id' => '...']);
 *         }
 *     }
 *
 * The runtime module (mod_php) scans loaded classes with
 * HandlerRegistry::registerClass() and binds each method to the
 * declared (method, path) pair.
 *
 * For the functional case where a class is overkill, call
 * HandlerRegistry::register() directly with a callable.
 */
#[Attribute(Attribute::TARGET_METHOD | Attribute::TARGET_FUNCTION)]
final class AgtpEndpoint
{
    /**
     * @param list<string> $errors          declared error codes the handler may return
     * @param list<string> $requiredScopes  scopes required to invoke this endpoint
     */
    public function __construct(
        public readonly string $method,
        public readonly string $path,
        public readonly array $errors = [],
        public readonly array $requiredScopes = [],
        public readonly string $description = '',
    ) {
    }
}
