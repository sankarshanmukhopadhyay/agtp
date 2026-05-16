<?php

declare(strict_types=1);

namespace Agtp\Tests;

use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointResponse;

/**
 * Fixture: a global function with an #[AgtpEndpoint] attribute,
 * for HandlerRegistryTest::testRegisterFunctionWithAttribute.
 */
#[AgtpEndpoint(method: 'QUERY', path: '/echo')]
function _decoratedEcho(EndpointContext $ctx): EndpointResponse
{
    return new EndpointResponse(body: ['echo' => (string) ($ctx->input['value'] ?? '')]);
}
