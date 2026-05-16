<?php

declare(strict_types=1);

namespace Agtp\Laravel;

use Agtp\Laravel\Console\AgtpServeCommand;
use Agtp\Laravel\Registry\AgtpHandlerCollector;
use Illuminate\Support\ServiceProvider;

/**
 * Laravel service provider for AGTP.
 *
 * Auto-discovered on `composer require agtp/agtp-laravel` via the
 * `extra.laravel.providers` declaration in composer.json. Sites that
 * disable package auto-discovery add the provider manually to
 * `config/app.php` (legacy) or `bootstrap/providers.php` (Laravel 11+).
 *
 * The provider binds the AgtpHandlerCollector as a container
 * singleton and registers the agtp:serve artisan command.
 */
final class AgtpServiceProvider extends ServiceProvider
{
    /**
     * Register services. Runs during the bind phase.
     */
    public function register(): void
    {
        $this->app->singleton(AgtpHandlerCollector::class, function ($app) {
            // Tagged services come from app()->tag(); the collector
            // walks them on demand when collect() is called.
            return new AgtpHandlerCollector(
                $app->tagged('agtp.endpoint'),
            );
        });
    }

    /**
     * Boot services. Runs after all providers register.
     */
    public function boot(): void
    {
        if ($this->app->runningInConsole()) {
            $this->commands([
                AgtpServeCommand::class,
            ]);
        }
    }
}
