# agtp-mcp

The MCP-on-AGTP bridge product. A revamp of the Model Context Protocol
to live on AGTP rather than HTTPS, surfacing MCP tool catalogs as
AGTP methods with the same identity, scope, and method-catalog
guarantees the rest of AGTP enjoys.

> This directory was previously named `mcp-on-agtp/`. Renamed to
> `agtp-mcp/` in the M7 layout pass for consistency with the other
> connector packages (`agtp-go`, `agtp-php`, `agtp-rust`, …). The
> bridge itself is unchanged; only the directory name moved.

This directory is currently a placeholder. The first cut will live
alongside the other product directories (`server/`, `client/`, etc.)
with its own entry point and a thin layer over `core` for the wire
format.

Today, MCP catalogs are surfaced as a read-only side panel in elemen
when a server's manifest declares `hosted_protocols: [{ "protocol": "mcp", ... }]`.
That elemen-side rendering is the prototype; the full bridge product
ships from this directory.
