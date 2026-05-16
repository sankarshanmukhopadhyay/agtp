<?php

declare(strict_types=1);

namespace Agtp;

use ReflectionClass;
use ReflectionFunction;
use RuntimeException;

/**
 * Process-wide registry of AGTP handlers keyed by (method, path).
 *
 * Mirrors agtp.registry.HandlerRegistry in the Python library. Most
 * applications use the singleton returned by ::default(); tests and
 * runtime modules building per-connection registries instantiate
 * their own.
 *
 * Three registration paths:
 *
 *   - HandlerRegistry::default()->register(...)
 *       Functional, explicit. Take a callable and pin it to
 *       (method, path). Closures work fine.
 *
 *   - HandlerRegistry::default()->registerClass(MyHandlers::class)
 *       Scan a class for methods tagged with #[AgtpEndpoint] and
 *       register each one. The matching Drupal-style idiom.
 *
 *   - HandlerRegistry::default()->registerFunction('book_room')
 *       Read #[AgtpEndpoint] off a global function and register it.
 *       For small scripts that don't want a class.
 */
final class HandlerRegistry
{
    private static ?self $default = null;

    /** @var array<string, RegisteredHandler> keyed by "METHOD path" */
    private array $handlers = [];

    public static function default(): self
    {
        if (self::$default === null) {
            self::$default = new self();
        }
        return self::$default;
    }

    /**
     * Reset the process-wide singleton. Tests only.
     */
    public static function resetDefault(): void
    {
        self::$default = null;
    }

    /**
     * @param callable(EndpointContext): (EndpointResponse|EndpointError) $handler
     * @param list<string> $errors
     * @param list<string> $requiredScopes
     */
    public function register(
        callable $handler,
        string $method,
        string $path,
        array $errors = [],
        array $requiredScopes = [],
        string $description = '',
    ): RegisteredHandler {
        $key = $this->key($method, $path);
        if (isset($this->handlers[$key])) {
            throw new RuntimeException(
                "handler already registered for ({$method}, {$path})"
            );
        }
        $entry = new RegisteredHandler(
            method: strtoupper($method),
            path: $path,
            handler: $handler,
            errors: $errors,
            requiredScopes: $requiredScopes,
            description: $description,
        );
        $this->handlers[$key] = $entry;
        return $entry;
    }

    /**
     * Register every public method of $className tagged with #[AgtpEndpoint].
     *
     * The class is instantiated once with no constructor arguments;
     * for handlers that need dependencies, pass a pre-built instance
     * to ::registerInstance() instead.
     *
     * @param class-string $className
     * @return list<RegisteredHandler>
     */
    public function registerClass(string $className): array
    {
        $reflection = new ReflectionClass($className);
        $instance = $reflection->newInstance();
        return $this->registerInstance($instance);
    }

    /**
     * Register every method of $instance tagged with #[AgtpEndpoint].
     *
     * @return list<RegisteredHandler>
     */
    public function registerInstance(object $instance): array
    {
        $reflection = new ReflectionClass($instance);
        $registered = [];
        foreach ($reflection->getMethods() as $method) {
            foreach ($method->getAttributes(AgtpEndpoint::class) as $attr) {
                /** @var AgtpEndpoint $endpoint */
                $endpoint = $attr->newInstance();
                $registered[] = $this->register(
                    handler: $method->getClosure($instance),
                    method: $endpoint->method,
                    path: $endpoint->path,
                    errors: $endpoint->errors,
                    requiredScopes: $endpoint->requiredScopes,
                    description: $endpoint->description,
                );
            }
        }
        return $registered;
    }

    /**
     * Register a global function tagged with #[AgtpEndpoint].
     *
     * @return RegisteredHandler|null  null when the function has no
     *     #[AgtpEndpoint] attribute.
     */
    public function registerFunction(string $functionName): ?RegisteredHandler
    {
        $reflection = new ReflectionFunction($functionName);
        foreach ($reflection->getAttributes(AgtpEndpoint::class) as $attr) {
            /** @var AgtpEndpoint $endpoint */
            $endpoint = $attr->newInstance();
            return $this->register(
                handler: $reflection->getClosure(),
                method: $endpoint->method,
                path: $endpoint->path,
                errors: $endpoint->errors,
                requiredScopes: $endpoint->requiredScopes,
                description: $endpoint->description,
            );
        }
        return null;
    }

    public function lookup(string $method, string $path): ?RegisteredHandler
    {
        return $this->handlers[$this->key($method, $path)] ?? null;
    }

    /** @return list<RegisteredHandler> */
    public function all(): array
    {
        return array_values($this->handlers);
    }

    public function count(): int
    {
        return count($this->handlers);
    }

    public function clear(): void
    {
        $this->handlers = [];
    }

    private function key(string $method, string $path): string
    {
        return strtoupper($method) . ' ' . $path;
    }
}
