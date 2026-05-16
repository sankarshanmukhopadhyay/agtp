<?php

declare(strict_types=1);

namespace Agtp\Laravel\Console;

use Agtp\HandlerRegistry;
use Agtp\Laravel\Registry\AgtpHandlerCollector;
use Agtp\ModPhp\GatewayClient;
use Agtp\ModPhp\ModuleException;
use Illuminate\Console\Command;

/**
 * Artisan command: `php artisan agtp:serve`.
 *
 * Laravel has already bootstrapped the application by the time the
 * command runs. The collector grabs every `agtp.endpoint`-tagged
 * binding from the container, registers each into the agtp-php
 * registry, then runs the gateway client.
 */
final class AgtpServeCommand extends Command
{
    /** @var string */
    protected $signature = 'agtp:serve
        {--gateway-socket= : Path to the agtpd gateway socket (or host:port for TCP loopback).}
        {--module-id=agtp_laravel : Identifier reported in the hello frame.}
        {--module-version=0.1.0 : Version reported in the hello frame.}';

    /** @var string */
    protected $description = 'Serve AGTP traffic via the local gateway socket.';

    public function handle(AgtpHandlerCollector $collector): int
    {
        $socket = (string) $this->option('gateway-socket');
        if ($socket === '') {
            $this->error('--gateway-socket is required');
            return self::FAILURE;
        }

        $registry = HandlerRegistry::default();
        $count = 0;
        foreach ($collector->collect($registry) as $_) {
            $count++;
        }
        $this->info(sprintf('Collected %d AGTP endpoint binding(s).', $count));
        if ($count === 0) {
            $this->warn(
                'No tagged "agtp.endpoint" bindings were found. Did you ' .
                'forget to call $this->app->tag(MyHandler::class, "agtp.endpoint") ' .
                'in your service provider?'
            );
        }

        $client = new GatewayClient(
            socketPath: $socket,
            registry: $registry,
            moduleId: (string) $this->option('module-id'),
            moduleVersion: (string) $this->option('module-version'),
        );

        if (function_exists('pcntl_signal')) {
            pcntl_async_signals(true);
            $shutdown = function () use ($client) {
                $this->info('Shutting down on signal.');
                $client->stop();
            };
            pcntl_signal(SIGTERM, $shutdown);
            pcntl_signal(SIGINT, $shutdown);
        }

        try {
            $client->run();
        } catch (ModuleException $exc) {
            $this->error($exc->getMessage());
            return self::FAILURE;
        }

        return self::SUCCESS;
    }
}
