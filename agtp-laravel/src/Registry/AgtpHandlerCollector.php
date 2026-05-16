<?php

declare(strict_types=1);

namespace Agtp\Laravel\Registry;

use Agtp\HandlerRegistry;
use Agtp\RegisteredHandler;

/**
 * Adopts every container-tagged handler into the agtp-php registry.
 *
 * Laravel's tagged-service pattern matches Symfony's and Drupal's:
 * the application binds services and tags them with
 * `agtp.endpoint`; we receive the resulting iterable and register
 * each instance.
 */
final class AgtpHandlerCollector
{
    /**
     * @param iterable<object> $taggedHandlers
     */
    public function __construct(
        private readonly iterable $taggedHandlers,
    ) {
    }

    /**
     * @return list<RegisteredHandler>
     */
    public function collect(HandlerRegistry $registry): array
    {
        $all = [];
        foreach ($this->taggedHandlers as $handler) {
            foreach ($registry->registerInstance($handler) as $entry) {
                $all[] = $entry;
            }
        }
        return $all;
    }
}
