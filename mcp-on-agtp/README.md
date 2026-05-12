# mcp-on-agtp

The MCP-on-AGTP bridge product. A revamp of the Model Context Protocol
to live on AGTP rather than HTTPS, surfacing MCP tool catalogs as
AGTP methods with the same identity, scope, and method-catalog
guarantees the rest of AGTP enjoys.

This directory is currently a placeholder. The first cut will live
alongside the other product directories (`server/`, `client/`, etc.)
with its own entry point and a thin layer over `core` for the wire
format.

Today, MCP catalogs are surfaced as a read-only side panel in elemen
when a server's manifest declares `hosted_protocols: [{ "protocol": "mcp", ... }]`.
That elemen-side rendering is the prototype; the full bridge product
ships from this directory.
