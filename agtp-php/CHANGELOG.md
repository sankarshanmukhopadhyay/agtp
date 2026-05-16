# agtp-php changelog

The handler-author-facing PHP library for AGTP. Pairs with `mod_php`.

The rule mirrors [`../agtp/CHANGELOG.md`](../agtp/CHANGELOG.md) and
[`../core/schemas/CHANGELOG.md`](../core/schemas/CHANGELOG.md): every
change to the public API surface (the classes under
`Agtp\` re-exported from this README and the worked examples) lands
here. Internal-only refactors do not.

## Versioning

The package versions independently of the AGTP wire format, the
method catalog, and the gateway protocol. Major bumps coordinate with
gateway-protocol majors (see
[`../docs/architecture/gateway-protocol-v1.md` §12.1](../docs/architecture/gateway-protocol-v1.md#121-when-v2-cuts)).
Minor bumps add features; patch bumps fix bugs or clarify
documentation.

## [Unreleased]

### Added — M4 initial release

Initial PHP handler-author library. Mirrors the validated Python
library [`agtp/`](../agtp/) value-for-value.

Public classes:

- `Agtp\EndpointContext` — per-request envelope. Readonly properties.
- `Agtp\EndpointResponse` — handler success shape.
- `Agtp\EndpointError` — handler declared-failure shape.
- `Agtp\AgtpEndpoint` — attribute marking a method or function as
  an AGTP endpoint handler.
- `Agtp\HandlerRegistry` — process-wide registry with `register()`,
  `registerClass()`, `registerInstance()`, `registerFunction()`.
- `Agtp\RegisteredHandler` — value object describing one binding.
- `Agtp\Testing` — `makeContext()`, `assertOk()`, `assertError()`
  for unit-testing handlers without `agtpd`.

The shapes match `EndpointContext` v1.0.0, `EndpointResponse` v1.0.0,
and `EndpointError` v1.0.0 frozen in
[`../core/schemas/`](../core/schemas/).
