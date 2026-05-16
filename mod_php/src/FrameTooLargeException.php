<?php

declare(strict_types=1);

namespace Agtp\ModPhp;

use RuntimeException;

/**
 * Raised when a frame's announced length exceeds FrameCodec::MAX_FRAME_SIZE.
 */
class FrameTooLargeException extends RuntimeException
{
}
