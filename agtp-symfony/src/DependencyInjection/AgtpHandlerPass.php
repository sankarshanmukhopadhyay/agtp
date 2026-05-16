<?php

declare(strict_types=1);

namespace Agtp\Symfony\DependencyInjection;

use Symfony\Component\DependencyInjection\Compiler\CompilerPassInterface;
use Symfony\Component\DependencyInjection\ContainerBuilder;
use Symfony\Component\DependencyInjection\Reference;

/**
 * Compiler pass: collects every service tagged `agtp.endpoint` into
 * the AgtpHandlerCollector constructor as a tagged iterator.
 *
 * The pattern mirrors agtp_drupal's tagged-service collection: site
 * builders register their handler service with a tag, and we
 * inject the resulting iterator at compile time so the collector
 * iterates real services (not stub references) at runtime.
 */
final class AgtpHandlerPass implements CompilerPassInterface
{
    public function process(ContainerBuilder $container): void
    {
        if (!$container->hasDefinition('agtp.handler_collector')) {
            return;
        }
        $taggedServices = $container->findTaggedServiceIds('agtp.endpoint');
        $references = [];
        foreach (array_keys($taggedServices) as $serviceId) {
            $references[] = new Reference($serviceId);
        }
        $definition = $container->getDefinition('agtp.handler_collector');
        $definition->setArgument('$taggedHandlers', $references);
    }
}
