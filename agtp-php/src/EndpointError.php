<?php

declare(strict_types=1);

namespace Agtp;

/**
 * Declared-failure shape returned from an AGTP handler.
 *
 * Mirrors agtp.handlers.EndpointError and is validated against
 * core/schemas/endpoint-error.schema.json. The code MUST be one of
 * the names in the endpoint's declared errors list (passed in the
 * gateway register frame); undeclared codes are a protocol violation
 * and become 500 errors logged against the module.
 *
 * Use EndpointError for the failure modes the contract describes.
 * Throw an exception for unexpected failures — the gateway client
 * converts those to a generic handler_exception frame.
 */
final class EndpointError
{
    /**
     * @param array<string, mixed>|null $details
     */
    public function __construct(
        public readonly string $code,
        public readonly string $message,
        public readonly ?array $details = null,
    ) {
    }

    /**
     * Wire form for the gateway response frame's envelope field.
     *
     * @return array<string, mixed>
     */
    public function toEnvelope(): array
    {
        $err = [
            'code' => $this->code,
            'message' => $this->message,
        ];
        if ($this->details !== null) {
            $err['details'] = $this->details;
        }
        return ['endpoint_error' => $err];
    }
}
