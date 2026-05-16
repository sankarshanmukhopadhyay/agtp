<?php

declare(strict_types=1);

namespace Agtp\Symfony;

use Agtp\Symfony\DependencyInjection\AgtpExtension;
use Agtp\Symfony\DependencyInjection\AgtpHandlerPass;
use Symfony\Component\DependencyInjection\Compiler\PassConfig;
use Symfony\Component\DependencyInjection\ContainerBuilder;
use Symfony\Component\DependencyInjection\Extension\ExtensionInterface;
use Symfony\Component\HttpKernel\Bundle\Bundle;

/**
 * Symfony bundle entry point for AGTP.
 *
 * Registers two pieces of container wiring:
 *
 *   - AgtpExtension: loads `services.yaml` (the collector + the
 *     console command).
 *   - AgtpHandlerPass: a compiler pass that finds every service
 *     tagged `agtp.endpoint` and feeds it into the collector
 *     constructor as a tagged iterator.
 *
 * Sites enable the bundle in `config/bundles.php`:
 *
 *     return [
 *         Agtp\Symfony\AgtpBundle::class => ['all' => true],
 *     ];
 */
final class AgtpBundle extends Bundle
{
    public function build(ContainerBuilder $container): void
    {
        parent::build($container);
        $container->addCompilerPass(
            new AgtpHandlerPass(),
            PassConfig::TYPE_BEFORE_OPTIMIZATION,
        );
    }

    public function getContainerExtension(): ?ExtensionInterface
    {
        if ($this->extension === null) {
            $this->extension = new AgtpExtension();
        }
        return $this->extension === false ? null : $this->extension;
    }
}
