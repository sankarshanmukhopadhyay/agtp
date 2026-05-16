<?php

declare(strict_types=1);

namespace Agtp\ModPhp;

use Agtp\EndpointContext;
use Agtp\EndpointError;
use Agtp\EndpointResponse;
use Agtp\HandlerRegistry;
use Throwable;

/**
 * Module-side gateway client.
 *
 * Port of mod_python/client.py. Connects to agtpd over a Unix socket
 * or TCP loopback, performs the handshake, receives the daemon's
 * endpoint registration, resolves each handler_reference against the
 * local HandlerRegistry, then serves request frames in a synchronous
 * loop.
 *
 * One connection, one in-flight request at a time. For higher
 * concurrency, the operator runs N mod_php processes pointing at
 * the same gateway socket (matches the FPM worker-pool model).
 */
final class GatewayClient
{
    private $sock = null;
    private $reader = null;
    private $writer = null;
    private bool $stop = false;
    /** @var array<string, callable> keyed by "METHOD path" */
    private array $bindings = [];
    /** @var array<string, callable> cached bindings for register_resume */
    private array $cachedBindings = [];
    public string $cachedManifestHash = '';

    public function __construct(
        private readonly string $socketPath,
        private readonly HandlerRegistry $registry,
        private readonly string $moduleId = 'mod_php',
        private readonly string $moduleVersion = '0.1.0',
        string $cachedManifestHash = '',
    ) {
        $this->cachedManifestHash = $cachedManifestHash;
    }

    /**
     * Connect, handshake, register, serve until disconnect/goodbye.
     *
     * Returns when the daemon sends goodbye, when the socket closes,
     * or when stop() is called from a signal handler.
     */
    public function run(): void
    {
        $this->connect();
        try {
            $this->handshake();
            $this->serveLoop();
        } finally {
            $this->close();
        }
    }

    public function stop(): void
    {
        $this->stop = true;
    }

    // ----- Internals -----

    private function connect(): void
    {
        // Decide transport: "host:port" → TCP, anything else → Unix socket.
        if (preg_match('/^[\d.]+(?::|:\[)?\d+$/', $this->socketPath) || str_starts_with($this->socketPath, '127.0.0.1:')) {
            [$host, $port] = explode(':', $this->socketPath, 2);
            $this->sock = @stream_socket_client(
                "tcp://{$host}:{$port}",
                $errno,
                $errstr,
                5.0,
            );
        } else {
            $this->sock = @stream_socket_client(
                "unix://" . $this->socketPath,
                $errno,
                $errstr,
                5.0,
            );
        }
        if ($this->sock === false || $this->sock === null) {
            throw new ModuleException(
                "could not connect to gateway socket {$this->socketPath}: " . ($errstr ?: 'unknown error')
            );
        }
        stream_set_blocking($this->sock, true);
        // We use the same handle for read and write — stream_socket_client
        // returns one resource. The codec's read/write helpers work on it.
        $this->reader = $this->sock;
        $this->writer = $this->sock;
    }

    private function close(): void
    {
        if ($this->sock !== null) {
            @fclose($this->sock);
            $this->sock = null;
            $this->reader = null;
            $this->writer = null;
        }
    }

    private function handshake(): void
    {
        $hello = [
            'type' => 'hello',
            'gateway_versions' => [FrameCodec::GATEWAY_VERSION],
            'module' => [
                'id' => $this->moduleId,
                'version' => $this->moduleVersion,
                'runtime' => 'PHP ' . PHP_VERSION,
                'pid' => getmypid() ?: 0,
            ],
            'capabilities' => ['registered_function'],
        ];
        if ($this->cachedManifestHash !== '') {
            $hello['cached_manifest_hash'] = $this->cachedManifestHash;
        }
        FrameCodec::writeFrame($this->writer, $hello);

        $welcome = FrameCodec::readFrame($this->reader);
        if (($welcome['type'] ?? '') === 'error') {
            throw new ModuleException(
                'daemon refused handshake: ' . ($welcome['code'] ?? '') . ': ' . ($welcome['message'] ?? '')
            );
        }
        if (($welcome['type'] ?? '') !== 'welcome') {
            throw new ModuleException(
                "expected welcome, got type=" . json_encode($welcome['type'] ?? null)
            );
        }
        $chosen = $welcome['gateway_version'] ?? '';
        if ($chosen !== FrameCodec::GATEWAY_VERSION) {
            throw new ModuleException(
                "daemon chose gateway version " . json_encode($chosen) .
                "; this module speaks " . FrameCodec::GATEWAY_VERSION
            );
        }

        $register = FrameCodec::readFrame($this->reader);
        $type = $register['type'] ?? '';
        if ($type === 'register_resume') {
            $this->handleRegisterResume($register);
        } elseif ($type === 'register') {
            $this->handleRegister($register);
        } else {
            throw new ModuleException(
                "expected register or register_resume, got type=" . json_encode($type)
            );
        }
    }

    /**
     * @param array<string, mixed> $register
     */
    private function handleRegister(array $register): void
    {
        $manifestHash = (string) ($register['manifest_hash'] ?? '');
        $endpoints = (array) ($register['endpoints'] ?? []);
        $resolved = [];
        $errors = [];
        $newBindings = [];
        foreach ($endpoints as $ep) {
            $method = strtoupper((string) ($ep['method'] ?? ''));
            $path = (string) ($ep['path'] ?? '/');
            $ref = (string) ($ep['handler_reference'] ?? '');
            $entry = $this->registry->lookup($method, $path);
            if ($entry === null) {
                $errors[] = [
                    'endpoint' => "{$method} {$path}",
                    'reason' => 'handler_not_found',
                    'detail' => "no #[AgtpEndpoint] registration matches ({$method}, {$path}); reference was " . json_encode($ref),
                ];
                continue;
            }
            $newBindings["{$method} {$path}"] = $entry->handler;
            $resolved[] = "{$method} {$path}";
        }

        if (!empty($errors)) {
            FrameCodec::writeFrame($this->writer, [
                'type' => 'register_ack',
                'ok' => false,
                'errors' => $errors,
            ]);
            throw new ModuleException(
                'could not resolve ' . count($errors) . ' endpoint reference(s): ' . json_encode($errors)
            );
        }

        $this->bindings = $newBindings;
        $this->cachedBindings = $newBindings;
        $this->cachedManifestHash = $manifestHash;
        FrameCodec::writeFrame($this->writer, [
            'type' => 'register_ack',
            'ok' => true,
            'resolved' => $resolved,
        ]);
    }

    /**
     * @param array<string, mixed> $register
     */
    private function handleRegisterResume(array $register): void
    {
        $manifestHash = (string) ($register['manifest_hash'] ?? '');
        if (empty($this->cachedBindings) || $manifestHash !== $this->cachedManifestHash) {
            FrameCodec::writeFrame($this->writer, [
                'type' => 'register_ack',
                'ok' => false,
                'errors' => [[
                    'endpoint' => '*',
                    'reason' => 'cache_miss',
                    'detail' => "module has no cached bindings matching manifest_hash=" . json_encode($manifestHash),
                ]],
            ]);
            throw new ModuleException(
                "register_resume could not reuse cached bindings (hash={$manifestHash})"
            );
        }
        $this->bindings = $this->cachedBindings;
        $resolved = array_keys($this->bindings);
        FrameCodec::writeFrame($this->writer, [
            'type' => 'register_ack',
            'ok' => true,
            'resolved' => $resolved,
        ]);
    }

    private function serveLoop(): void
    {
        while (!$this->stop) {
            try {
                $frame = FrameCodec::readFrame($this->reader);
            } catch (FrameDecodeException | FrameTooLargeException) {
                return;
            }

            $type = $frame['type'] ?? '';
            if ($type === 'goodbye') {
                return;
            }
            if ($type === 'ping') {
                FrameCodec::writeFrame($this->writer, [
                    'type' => 'pong',
                    'nonce' => (string) ($frame['nonce'] ?? ''),
                ]);
                continue;
            }
            if ($type !== 'request') {
                FrameCodec::writeFrame($this->writer, [
                    'type' => 'error',
                    'code' => 'phase_violation',
                    'message' => "unexpected frame type " . json_encode($type),
                ]);
                continue;
            }

            $this->handleRequest($frame);
        }
    }

    /**
     * @param array<string, mixed> $frame
     */
    private function handleRequest(array $frame): void
    {
        $requestId = (string) ($frame['request_id'] ?? '');
        $envelope = (array) ($frame['envelope'] ?? []);
        $method = strtoupper((string) ($envelope['method'] ?? ''));
        $path = (string) ($envelope['path'] ?? '/');
        $key = "{$method} {$path}";
        $handler = $this->bindings[$key] ?? null;
        if ($handler === null) {
            FrameCodec::writeFrame($this->writer, [
                'type' => 'error',
                'request_id' => $requestId,
                'code' => 'handler_exception',
                'message' => "no handler bound for ({$method}, {$path})",
            ]);
            return;
        }

        $ctx = EndpointContext::fromEnvelope($envelope);

        try {
            /** @var EndpointResponse|EndpointError $result */
            $result = $handler($ctx);
        } catch (Throwable $exc) {
            fwrite(STDERR, "[mod_php] handler raised " . $exc::class . ": " . $exc->getMessage() . "\n");
            FrameCodec::writeFrame($this->writer, [
                'type' => 'error',
                'request_id' => $requestId,
                'code' => 'handler_exception',
                'message' => $exc::class . ': ' . $exc->getMessage(),
                'details' => ['exception_type' => $exc::class],
            ]);
            return;
        }

        if ($result instanceof EndpointResponse) {
            FrameCodec::writeFrame($this->writer, [
                'type' => 'response',
                'request_id' => $requestId,
                'envelope' => $result->toEnvelope(),
            ]);
            return;
        }
        if ($result instanceof EndpointError) {
            FrameCodec::writeFrame($this->writer, [
                'type' => 'response',
                'request_id' => $requestId,
                'envelope' => $result->toEnvelope(),
            ]);
            return;
        }
        $type = is_object($result) ? $result::class : gettype($result);
        FrameCodec::writeFrame($this->writer, [
            'type' => 'error',
            'request_id' => $requestId,
            'code' => 'handler_exception',
            'message' => "handler returned {$type}; expected EndpointResponse or EndpointError",
        ]);
    }
}
