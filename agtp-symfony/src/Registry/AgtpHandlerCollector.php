<?php

declare(strict_types=1);

namespace Agtp\Symfony\Registry;

use Agtp\HandlerRegistry;
use Agtp\RegisteredHandler;

/**
 * Adopts every service tagged `agtp.endpoint` into the agtp-php
 * HandlerRegistry.
 *
 * Direct port of Drupal\agtp_drupal\Registry\AgtpHandlerCollector —
 * Symfony's DI is the parent of Drupal's, so the pattern is
 * identical. The compiler pass wires the tagged-service iterator at
 * compile time.
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
     * Populate the supplied registry from every tagged service.
     *
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
