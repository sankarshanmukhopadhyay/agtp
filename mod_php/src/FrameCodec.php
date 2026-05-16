<?php

declare(strict_types=1);

namespace Agtp\ModPhp;

use RuntimeException;

/**
 * Length-prefixed JSON framing for the AGTP gateway protocol.
 *
 * Mirrors core/gateway/protocol.py from the Python implementation:
 * 4-byte big-endian unsigned length prefix, then UTF-8 JSON payload,
 * max 16 MiB. This is the on-the-wire form between agtpd and any
 * runtime module.
 *
 * See docs/architecture/gateway-protocol-v1.md §3 for the framing
 * contract.
 */
final class FrameCodec
{
    /** Gateway protocol version this implementation speaks. */
    public const GATEWAY_VERSION = '1.0';

    /** Hard cap on a single frame's JSON payload. */
    public const MAX_FRAME_SIZE = 16 * 1024 * 1024;

    /**
     * Read one frame from a stream and return the parsed payload.
     *
     * @param resource $stream
     * @return array<string, mixed>
     */
    public static function readFrame($stream): array
    {
        $header = self::readExact($stream, 4);
        $unpacked = unpack('N', $header);
        if ($unpacked === false || !isset($unpacked[1])) {
            throw new RuntimeException('frame length header could not be parsed');
        }
        $length = $unpacked[1];
        if ($length > self::MAX_FRAME_SIZE) {
            throw new FrameTooLargeException(
                "frame length {$length} exceeds MAX_FRAME_SIZE " . self::MAX_FRAME_SIZE
            );
        }
        if ($length === 0) {
            throw new FrameDecodeException('empty frame (length=0)');
        }
        $body = self::readExact($stream, $length);
        try {
            $payload = json_decode($body, true, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException $exc) {
            throw new FrameDecodeException(
                'frame body is not valid JSON: ' . $exc->getMessage(),
                previous: $exc,
            );
        }
        if (!is_array($payload) || array_is_list($payload)) {
            throw new FrameDecodeException(
                'frame body must be a JSON object'
            );
        }
        if (!isset($payload['type'])) {
            throw new FrameDecodeException(
                "frame payload missing required 'type' field"
            );
        }
        return $payload;
    }

    /**
     * Encode $payload and write it to $stream.
     *
     * @param resource             $stream
     * @param array<string, mixed> $payload
     */
    public static function writeFrame($stream, array $payload): void
    {
        if (!isset($payload['type'])) {
            throw new RuntimeException("frame payload must carry a 'type' field");
        }
        $body = json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        if ($body === false) {
            throw new RuntimeException(
                'failed to JSON-encode frame: ' . json_last_error_msg()
            );
        }
        $length = strlen($body);
        if ($length > self::MAX_FRAME_SIZE) {
            throw new FrameTooLargeException(
                "encoded frame size {$length} exceeds MAX_FRAME_SIZE " . self::MAX_FRAME_SIZE
            );
        }
        $header = pack('N', $length);
        $written = fwrite($stream, $header . $body);
        if ($written === false || $written !== ($length + 4)) {
            throw new RuntimeException(
                "incomplete frame write (wanted " . ($length + 4) . ", wrote " . ($written === false ? '0' : $written) . ")"
            );
        }
        fflush($stream);
    }

    /**
     * Read exactly $n bytes from the stream or throw on truncation.
     *
     * @param resource $stream
     */
    private static function readExact($stream, int $n): string
    {
        if ($n <= 0) {
            return '';
        }
        $buf = '';
        $remaining = $n;
        while ($remaining > 0) {
            $chunk = fread($stream, $remaining);
            if ($chunk === false || $chunk === '') {
                if (feof($stream)) {
                    throw new FrameDecodeException(
                        "connection closed mid-frame (expected {$n} bytes, got " . strlen($buf) . ')'
                    );
                }
                // Treat false / empty as transient — yield briefly and retry.
                usleep(1000);
                continue;
            }
            $buf .= $chunk;
            $remaining -= strlen($chunk);
        }
        return $buf;
    }
}
