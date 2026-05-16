<?php

declare(strict_types=1);

namespace Agtp;

/**
 * Success-shape returned from an AGTP handler.
 *
 * Mirrors agtp.handlers.EndpointResponse and is validated against
 * core/schemas/endpoint-response.schema.json. The body is validated
 * against the endpoint's output schema by agtpd before it's
 * serialized onto the AGTP wire; an output-schema failure becomes a
 * 500 logged against the module.
 */
final class EndpointResponse
{
    /**
     * @param array<string, mixed>       $body
     * @param array<string, string>|null $headers
     */
    public function __construct(
        public readonly array $body,
        public readonly int $status = 200,
        public readonly ?array $headers = null,
    ) {
    }

    /**
     * Wire form for the gateway response frame's envelope field.
     *
     * @return array<string, mixed>
     */
    public function toEnvelope(): array
    {
        $envelope = [
            'body' => $this->body,
            'status' => $this->status,
        ];
        if ($this->headers !== null) {
            $envelope['headers'] = $this->headers;
        }
        return $envelope;
    }
}
