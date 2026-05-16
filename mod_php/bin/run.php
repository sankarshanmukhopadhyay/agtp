#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * mod_php CLI entry point.
 *
 *     php mod_php/bin/run.php \
 *         --gateway-socket /var/run/agtpd/gateway.sock \
 *         --bootstrap /path/to/handlers-bootstrap.php
 *
 * The bootstrap file is a PHP script that:
 *   1. Requires the Composer autoloader
 *      (`require __DIR__ . '/vendor/autoload.php';`)
 *   2. Registers your handlers against \Agtp\HandlerRegistry::default()
 *      (via registerClass(), registerFunction(), or register()).
 *
 * After the bootstrap runs, this script connects to agtpd and serves
 * gateway frames until the connection closes or the daemon sends
 * goodbye.
 */

// Find the Composer autoloader. The CLI may be invoked from one of:
//   - the mod_php package directly (composer install in mod_php/)
//   - a host project that pulled mod_php in as a dependency
$autoloadCandidates = [
    __DIR__ . '/../vendor/autoload.php',         // mod_php/vendor/...
    __DIR__ . '/../../vendor/autoload.php',      // host-project/vendor/...
    __DIR__ . '/../../../autoload.php',          // host-project/vendor/agtp/mod-php/bin → host-project/vendor/autoload.php
];
$autoloader = null;
foreach ($autoloadCandidates as $path) {
    if (file_exists($path)) {
        $autoloader = require $path;
        break;
    }
}
if ($autoloader === null) {
    fwrite(STDERR, "[mod_php] could not locate Composer autoloader. Run `composer install` first.\n");
    exit(2);
}

use Agtp\HandlerRegistry;
use Agtp\ModPhp\GatewayClient;
use Agtp\ModPhp\ModuleException;

// ---- Argument parsing (deliberately tiny; no Symfony Console dep) ----

$args = $argv;
array_shift($args); // pop script name

$socketPath = null;
$bootstrapPath = null;
$moduleId = 'mod_php';
$moduleVersion = '0.1.0';

while (!empty($args)) {
    $arg = array_shift($args);
    switch ($arg) {
        case '--gateway-socket':
            $socketPath = array_shift($args);
            break;
        case '--bootstrap':
            $bootstrapPath = array_shift($args);
            break;
        case '--module-id':
            $moduleId = (string) array_shift($args);
            break;
        case '--module-version':
            $moduleVersion = (string) array_shift($args);
            break;
        case '-h':
        case '--help':
            echo <<<TEXT
                Usage: php mod_php/bin/run.php --gateway-socket PATH [--bootstrap FILE]

                Required:
                  --gateway-socket PATH    Unix socket path or 'host:port' for TCP loopback.

                Optional:
                  --bootstrap FILE         PHP script that registers your handlers
                                           against \Agtp\HandlerRegistry::default().
                  --module-id ID           Identifier reported in the hello frame.
                                           Defaults to 'mod_php'.
                  --module-version VER     Version reported in the hello frame.
                                           Defaults to '0.1.0'.

                See agtp-php/README.md for the handler-author guide.
                TEXT . "\n";
            exit(0);
        default:
            fwrite(STDERR, "[mod_php] unknown argument: {$arg}\n");
            exit(2);
    }
}

if ($socketPath === null) {
    fwrite(STDERR, "[mod_php] --gateway-socket is required\n");
    exit(2);
}

// ---- Bootstrap: load user handlers ----

if ($bootstrapPath !== null) {
    if (!file_exists($bootstrapPath)) {
        fwrite(STDERR, "[mod_php] bootstrap file not found: {$bootstrapPath}\n");
        exit(2);
    }
    require $bootstrapPath;
    fwrite(STDERR, "[mod_php] bootstrap loaded: {$bootstrapPath}\n");
    fwrite(STDERR, '[mod_php] handlers registered: ' . HandlerRegistry::default()->count() . "\n");
}

// ---- Run ----

$client = new GatewayClient(
    socketPath: $socketPath,
    registry: HandlerRegistry::default(),
    moduleId: $moduleId,
    moduleVersion: $moduleVersion,
);

// Graceful shutdown on SIGTERM / SIGINT.
if (function_exists('pcntl_signal')) {
    pcntl_async_signals(true);
    $shutdown = function () use ($client) {
        fwrite(STDERR, "[mod_php] shutting down\n");
        $client->stop();
    };
    pcntl_signal(SIGTERM, $shutdown);
    pcntl_signal(SIGINT, $shutdown);
}

try {
    $client->run();
} catch (ModuleException $exc) {
    fwrite(STDERR, '[mod_php] ' . $exc->getMessage() . "\n");
    exit(1);
}

exit(0);
