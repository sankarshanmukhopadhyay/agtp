<?php

declare(strict_types=1);

namespace Agtp\ModPhp;

use RuntimeException;

/**
 * Raised when a gateway frame cannot be decoded — truncation,
 * non-JSON body, non-object body, or missing required fields.
 */
class FrameDecodeException extends RuntimeException
{
}
