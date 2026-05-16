# Install Guide — AGTP for Drupal

The smallest possible installation, from a Drupal 10/11 site to a
working AGTP endpoint. Assumes you already have `agtpd` running
somewhere (see the top-level [`README.md`](../README.md)).

## 1. Add the packages

From your site root:

```bash
# Until the packages publish to Packagist, point Composer at the
# monorepo via path repositories. Replace /opt/agtp with wherever
# you cloned the AGTP source tree.
composer config repositories.agtp-php   path /opt/agtp/agtp-php
composer config repositories.mod-php    path /opt/agtp/mod_php
composer config repositories.agtp-drupal path /opt/agtp/agtp_drupal

composer require agtp/agtp-drupal:@dev
```

## 2. Enable the module

```bash
drush en agtp_drupal
```

`agtp_drupal` enables; no further admin UI steps yet.

## 3. Write a handler

Create a custom module in `web/modules/custom/my_agtp/`:

```bash
mkdir -p web/modules/custom/my_agtp/src/Agtp
```

`web/modules/custom/my_agtp/my_agtp.info.yml`:

```yaml
name: My AGTP handlers
type: module
package: AGTP
core_version_requirement: ^10.2 || ^11
dependencies:
  - agtp:agtp_drupal
```

`web/modules/custom/my_agtp/my_agtp.services.yml`:

```yaml
services:
  my_agtp.echo_handler:
    class: Drupal\my_agtp\Agtp\EchoHandler
    tags:
      - { name: agtp.endpoint }
```

`web/modules/custom/my_agtp/src/Agtp/EchoHandler.php`:

```php
<?php
namespace Drupal\my_agtp\Agtp;

use Agtp\AgtpEndpoint;
use Agtp\EndpointContext;
use Agtp\EndpointResponse;

final class EchoHandler
{
    #[AgtpEndpoint(method: 'QUERY', path: '/echo')]
    public function echo(EndpointContext $ctx): EndpointResponse
    {
        return new EndpointResponse(body: [
            'echo' => (string) ($ctx->input['value'] ?? ''),
            'agent' => $ctx->agentId,
        ]);
    }
}
```

Enable:

```bash
drush cr
drush en my_agtp
```

## 4. Run the worker

In one shell — the daemon (run separately, on the host):

```bash
python -m server 4480 \
    --agents-dir /etc/agtpd/agents \
    --endpoints-dir /etc/agtpd/endpoints \
    --gateway-socket /var/run/agtpd/gateway.sock
```

In another shell — Drupal worker:

```bash
drush agtp:serve --gateway-socket=/var/run/agtpd/gateway.sock
```

The worker logs:

```
[notice] Collected 1 AGTP endpoint binding(s) from tagged services.
```

The daemon logs:

```
[gateway] module connected: agtp_drupal v0.1.0 (N endpoints)
```

## 5. Invoke from a client

```bash
agtp agtp://localhost:4480 QUERY --path /echo --param value="hello"
```

Response body:

```json
{
  "echo": "hello",
  "agent": "<your agent id>"
}
```

## Troubleshooting

**`No services tagged "agtp.endpoint" were found.`**
Check that your service has the `tags: - { name: agtp.endpoint }`
key. Run `drush cr` after changing `*.services.yml`.

**`module refused registration: handler_not_found`**
The daemon advertised an endpoint whose handler-reference doesn't
resolve in Drupal. Check that the daemon's endpoint TOML matches
what your Drupal handler declares (method + path must match exactly).

**`could not connect to gateway socket`**
Make sure `agtpd` is running and the socket path is correct.
Verify with `ls -l /var/run/agtpd/gateway.sock` — the worker's uid
must have read+write access.

**The worker exits immediately.**
Check `drush agtp:serve` stderr. The most common cause is a
handler class that throws during construction (Drupal couldn't
build the service). Run `drush cr` and try again.
