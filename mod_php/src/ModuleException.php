<?php

declare(strict_types=1);

namespace Agtp\ModPhp;

use RuntimeException;

/**
 * Raised when the module cannot operate (handshake failed,
 * registration refused, etc).
 */
class ModuleException extends RuntimeException
{
}
