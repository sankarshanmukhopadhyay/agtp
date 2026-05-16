<?php

declare(strict_types=1);

namespace Agtp\WordPress;

use Agtp\HandlerRegistry;
use Agtp\ModPhp\GatewayClient;
use Agtp\ModPhp\ModuleException;
use WP_CLI;
use WP_CLI_Command;

/**
 * WP-CLI command: `wp agtp serve`.
 *
 * WordPress is already bootstrapped by the time this command runs
 * (WP-CLI takes care of that). The `init` action that
 * agtp-wordpress.php hooked has already fired, so every handler
 * declared via the `agtp_register_handlers` filter or the
 * `agtp_init` action is in \Agtp\HandlerRegistry::default().
 *
 * The command runs the gateway client against the supplied socket
 * until the daemon disconnects or the operator interrupts.
 */
final class AgtpCliCommand extends WP_CLI_Command
{
    /**
     * Serve AGTP traffic via the local gateway socket.
     *
     * ## OPTIONS
     *
     * [--gateway-socket=<path>]
     * : Required. Path to the agtpd gateway socket. Pass "host:port"
     * for TCP loopback.
     *
     * [--module-id=<id>]
     * : Identifier reported in the hello frame. Defaults to
     * "agtp_wordpress".
     *
     * [--module-version=<ver>]
     * : Version reported in the hello frame. Defaults to "0.1.0".
     *
     * ## EXAMPLES
     *
     *     wp agtp serve --gateway-socket=/var/run/agtpd/gateway.sock
     *     wp agtp serve --gateway-socket=127.0.0.1:4481
     *
     * @param array<int, string> $args
     * @param array<string, string> $assoc_args
     */
    public function serve(array $args, array $assoc_args): void
    {
        $socket = $assoc_args['gateway-socket'] ?? '';
        if ($socket === '') {
            WP_CLI::error('--gateway-socket is required');
            return;
        }

        $registry = HandlerRegistry::default();
        WP_CLI::log(
            sprintf('Handlers registered: %d', $registry->count())
        );
        if ($registry->count() === 0) {
            WP_CLI::warning(
                'No handlers registered. Did your plugin add an ' .
                '`agtp_register_handlers` filter or an `agtp_init` action?'
            );
        }

        $client = new GatewayClient(
            socketPath: $socket,
            registry: $registry,
            moduleId: $assoc_args['module-id'] ?? 'agtp_wordpress',
            moduleVersion: $assoc_args['module-version'] ?? '0.1.0',
        );

        if (function_exists('pcntl_signal')) {
            pcntl_async_signals(true);
            $shutdown = function () use ($client) {
                WP_CLI::log('Shutting down.');
                $client->stop();
            };
            pcntl_signal(SIGTERM, $shutdown);
            pcntl_signal(SIGINT, $shutdown);
        }

        try {
            $client->run();
        } catch (ModuleException $exc) {
            WP_CLI::error($exc->getMessage());
        }
    }
}
