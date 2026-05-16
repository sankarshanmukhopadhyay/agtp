<?php

declare(strict_types=1);

namespace Drupal\agtp_drupal\Drush\Commands;

use Agtp\HandlerRegistry;
use Agtp\ModPhp\GatewayClient;
use Agtp\ModPhp\ModuleException;
use Drupal\agtp_drupal\Registry\AgtpHandlerCollector;
use Drush\Attributes as CLI;
use Drush\Commands\DrushCommands;

/**
 * Drush command: ``drush agtp:serve``.
 *
 * Bootstraps Drupal (Drush does this for us before the command runs),
 * collects every ``agtp.endpoint``-tagged service into the agtp-php
 * HandlerRegistry, then runs the gateway client against the supplied
 * agtpd gateway socket.
 *
 * Operators typically run this under a process supervisor (systemd,
 * Supervisor, Kubernetes Deployment) so the worker restarts on crash.
 * For higher request concurrency, run N copies of the command
 * pointing at the same socket — agtpd accepts multiple module
 * connections.
 */
final class AgtpServeCommands extends DrushCommands
{
    public function __construct(
        private readonly AgtpHandlerCollector $collector,
    ) {
        parent::__construct();
    }

    #[CLI\Command(name: 'agtp:serve', aliases: ['agtp-serve'])]
    #[CLI\Option(
        name: 'gateway-socket',
        description: 'Path to the agtpd gateway socket. Required. Pass "host:port" for TCP loopback.',
    )]
    #[CLI\Option(
        name: 'module-id',
        description: 'Identifier reported in the hello frame.',
    )]
    #[CLI\Option(
        name: 'module-version',
        description: 'Version reported in the hello frame.',
    )]
    #[CLI\Usage(
        name: 'drush agtp:serve --gateway-socket=/var/run/agtpd/gateway.sock',
        description: 'Serve AGTP traffic via the local Unix gateway socket.',
    )]
    #[CLI\Usage(
        name: 'drush agtp:serve --gateway-socket=127.0.0.1:4481',
        description: 'Serve AGTP traffic via TCP loopback (for sibling-container deployments).',
    )]
    public function serve(array $options = [
        'gateway-socket' => self::REQ,
        'module-id' => 'agtp_drupal',
        'module-version' => '0.1.0',
    ]): int
    {
        $socket = (string) ($options['gateway-socket'] ?? '');
        if ($socket === '') {
            $this->logger()->error('--gateway-socket is required');
            return self::EXIT_FAILURE;
        }

        $registry = HandlerRegistry::default();
        $count = 0;
        foreach ($this->collector->collect($registry) as $_) {
            $count++;
        }
        $this->logger()->notice(
            sprintf('Collected %d AGTP endpoint binding(s) from tagged services.', $count)
        );
        if ($count === 0) {
            $this->logger()->warning(
                'No services tagged "agtp.endpoint" were found. Did you ' .
                'forget to register your handler service with the tag?'
            );
        }

        $client = new GatewayClient(
            socketPath: $socket,
            registry: $registry,
            moduleId: (string) ($options['module-id'] ?? 'agtp_drupal'),
            moduleVersion: (string) ($options['module-version'] ?? '0.1.0'),
        );

        // Graceful shutdown on SIGTERM / SIGINT.
        if (function_exists('pcntl_signal')) {
            pcntl_async_signals(true);
            $shutdown = function () use ($client) {
                $this->logger()->notice('Shutting down on signal.');
                $client->stop();
            };
            pcntl_signal(SIGTERM, $shutdown);
            pcntl_signal(SIGINT, $shutdown);
        }

        try {
            $client->run();
        } catch (ModuleException $exc) {
            $this->logger()->error($exc->getMessage());
            return self::EXIT_FAILURE;
        }

        return self::EXIT_SUCCESS;
    }
}
