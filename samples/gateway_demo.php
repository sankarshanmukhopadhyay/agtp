<?php

declare(strict_types=1);

/**
 * Sample PHP handlers — mirror of samples/gateway_demo.py.
 *
 * Used by mod_php's CLI via `--bootstrap`; also imported by the
 * tests/test_gateway_e2e_php.py end-to-end test that spawns
 * mod_php as a subprocess and verifies the round-trip.
 *
 * Run end-to-end:
 *
 *     # Terminal 1
 *     python -m server --gateway-socket /tmp/agtpd.sock \
 *         --agents-dir server/agents --endpoints-dir endpoints
 *
 *     # Terminal 2
 *     php mod_php/bin/run.php \
 *         --gateway-socket /tmp/agtpd.sock \
 *         --bootstrap samples/gateway_demo.php
 *
 * The composer.json at agtp-php/ uses path-style autoloading; the
 * host project autoloader (or a separate `composer install` inside
 * mod_php/) provides PSR-4 resolution for the Agtp\ namespace.
 */

namespace Samples\GatewayDemo;

use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;
use Agtp\HandlerRegistry;

final class GatewayDemoHandlers
{
    #[AgtpEndpoint(method: 'QUERY', path: '/echo')]
    public function echo(EndpointContext $ctx): EndpointResponse
    {
        return new EndpointResponse(body: [
            'echo' => (string) ($ctx->input['value'] ?? ''),
        ]);
    }

    #[AgtpEndpoint(
        method: 'BOOK',
        path: '/room',
        errors: ['room_unavailable'],
    )]
    public function bookRoom(EndpointContext $ctx): EndpointResponse|EndpointError
    {
        $roomType = (string) ($ctx->input['room_type'] ?? 'double');
        if ($roomType === 'presidential_suite') {
            return new EndpointError(
                code: 'room_unavailable',
                message: 'The presidential suite is not available.',
                details: ['room_type' => $roomType],
            );
        }
        return new EndpointResponse(body: [
            'reservation_id' => 'res-' . (string) ($ctx->input['guest'] ?? 'anon') . '-' . $roomType,
            'agent' => $ctx->agentId,
        ]);
    }
}

HandlerRegistry::default()->registerClass(GatewayDemoHandlers::class);
