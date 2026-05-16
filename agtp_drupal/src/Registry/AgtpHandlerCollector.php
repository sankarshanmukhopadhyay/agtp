<?php

declare(strict_types=1);

namespace Drupal\agtp_drupal\Registry;

use Agtp\HandlerRegistry;
use Agtp\RegisteredHandler;

/**
 * Adopts every service tagged ``agtp.endpoint`` into the agtp-php
 * HandlerRegistry.
 *
 * The collector is the Drupal-native discovery surface for AGTP
 * handlers. Site builders register their handler classes as services
 * with the tag ``agtp.endpoint``; the collector iterates them and
 * calls ``HandlerRegistry::registerInstance()`` for each. The method
 * reflection inside ``registerInstance()`` then picks up every public
 * method decorated with ``#[AgtpEndpoint]``.
 *
 * The collected service is the responsibility of the module that
 * registers it. The collector does not enforce a base class, does
 * not auto-configure dependencies, and does not validate the
 * handler's contract beyond what agtp-php already checks. This
 * keeps agtp_drupal out of the way of Drupal's normal DI patterns.
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
     * Called once at boot. Idempotent — the registry's
     * duplicate-registration check guards against multiple calls,
     * but the operator should not need to invoke this more than once
     * per process.
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

    /**
     * Convenience for callers that don't need the registered list.
     */
    public function collectIntoDefaultRegistry(): int
    {
        $registered = $this->collect(HandlerRegistry::default());
        return count($registered);
    }
}
