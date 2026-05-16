<?php
/**
 * Plugin Name:       AGTP for WordPress
 * Plugin URI:        https://agtp.io
 * Description:       Serve AGTP traffic from your WordPress site. Plugin developers register handler classes; the `wp agtp serve` WP-CLI command runs the gateway worker against a local agtpd instance.
 * Version:           0.1.0
 * Requires at least: 6.4
 * Requires PHP:      8.1
 * Author:            Chris Hood
 * Author URI:        https://nomotic.ai
 * License:           GPL-2.0-or-later
 * License URI:       https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain:       agtp-wordpress
 *
 * AGTP for WordPress sits on top of agtp-php (the language library)
 * and mod_php (the runtime client). It does NOT serve AGTP traffic
 * through WordPress's HTTP request pipeline — AGTP runs on its own
 * port (4480) via agtpd, and this plugin is the WordPress-side
 * worker that connects to it.
 */

declare(strict_types=1);

if (!defined('ABSPATH')) {
    exit;
}

// Composer autoloader. Operators can install this plugin standalone
// (using a release ZIP that bundles vendor/) or via Composer
// (recommended), in which case the host project's autoloader
// resolves @agtp/agtp-php and @agtp/mod-php.
$candidates = [
    __DIR__ . '/vendor/autoload.php',          // bundled
    ABSPATH . 'vendor/autoload.php',           // site-level Composer (Bedrock etc.)
];
foreach ($candidates as $path) {
    if (file_exists($path)) {
        require_once $path;
        break;
    }
}

if (!class_exists(\Agtp\HandlerRegistry::class)) {
    add_action('admin_notices', function () {
        echo '<div class="notice notice-error"><p>';
        echo '<strong>AGTP for WordPress</strong>: agtp-php is not installed. ';
        echo 'Run <code>composer require agtp/agtp-php agtp/mod-php</code> ';
        echo 'in your site root, or install a release ZIP that bundles them.';
        echo '</p></div>';
    });
    return;
}

/**
 * Action hook: plugins register their AGTP handlers in response to
 * this. Either call \Agtp\HandlerRegistry::default()->registerClass(
 * MyHandlers::class) directly, or use the `agtp_register_handlers`
 * filter to return a list of class names that this plugin will
 * register for you.
 *
 * Fired during init at priority 5, well before WP-CLI commands run.
 */
add_action('init', function () {
    /**
     * Filter: agtp_register_handlers.
     *
     * Plugins may return a list of fully-qualified handler class
     * names. agtp-wordpress will call registerClass() on each.
     *
     * Plugins that need finer-grained control (e.g., instantiating
     * handlers with constructor arguments) should instead listen for
     * the `agtp_init` action and call HandlerRegistry methods directly.
     *
     * @param array<int, class-string> $classes
     * @return array<int, class-string>
     */
    $classes = apply_filters('agtp_register_handlers', []);
    foreach ($classes as $class) {
        if (is_string($class) && class_exists($class)) {
            \Agtp\HandlerRegistry::default()->registerClass($class);
        }
    }

    /**
     * Action: agtp_init.
     *
     * Fires after agtp-wordpress has processed the filter, just
     * before WP-CLI commands run. Plugins use this to register
     * handlers that need explicit construction.
     */
    do_action('agtp_init');
}, 5);

// WP-CLI command registration.
if (defined('WP_CLI') && WP_CLI) {
    \WP_CLI::add_command('agtp', \Agtp\WordPress\AgtpCliCommand::class);
}
