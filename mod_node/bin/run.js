#!/usr/bin/env node
// mod_node CLI entry.
//
//   node mod_node/bin/run.js \
//       --gateway-socket /var/run/agtpd/gateway.sock \
//       --bootstrap path/to/handlers-bootstrap.mjs
//
// The bootstrap module imports @agtp/agtp-node, builds a registry,
// registers handlers, and exports a default-export of that registry.
// This script imports the bootstrap, takes its default export, and
// hands it to the GatewayClient.

import process from 'node:process';
import { pathToFileURL } from 'node:url';
import { resolve as resolvePath } from 'node:path';

import { GatewayClient, ModuleError } from '../dist/index.js';
import { HandlerRegistry, registry as defaultRegistry } from '@agtp/agtp-node';

function parseArgs(argv) {
  const args = {
    gatewaySocket: null,
    bootstrap: null,
    moduleId: 'mod_node',
    moduleVersion: '0.1.0',
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--gateway-socket') args.gatewaySocket = argv[++i];
    else if (a === '--bootstrap') args.bootstrap = argv[++i];
    else if (a === '--module-id') args.moduleId = argv[++i];
    else if (a === '--module-version') args.moduleVersion = argv[++i];
    else if (a === '-h' || a === '--help') {
      process.stderr.write(
        'Usage: node mod_node/bin/run.js --gateway-socket PATH [--bootstrap FILE]\n',
      );
      process.exit(0);
    } else {
      process.stderr.write(`[mod_node] unknown argument: ${a}\n`);
      process.exit(2);
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.gatewaySocket) {
    process.stderr.write('[mod_node] --gateway-socket is required\n');
    process.exit(2);
  }

  let registry = defaultRegistry;
  if (args.bootstrap) {
    const url = pathToFileURL(resolvePath(args.bootstrap)).href;
    const mod = await import(url);
    if (mod.default instanceof HandlerRegistry) {
      registry = mod.default;
    }
    process.stderr.write(
      `[mod_node] bootstrap loaded: ${args.bootstrap} (${registry.count()} handlers)\n`,
    );
  }

  const client = new GatewayClient({
    socketPath: args.gatewaySocket,
    registry,
    moduleId: args.moduleId,
    moduleVersion: args.moduleVersion,
  });

  process.on('SIGTERM', () => {
    process.stderr.write('[mod_node] shutting down on SIGTERM\n');
    client.shutdown();
  });
  process.on('SIGINT', () => {
    process.stderr.write('[mod_node] shutting down on SIGINT\n');
    client.shutdown();
  });

  try {
    await client.run();
  } catch (e) {
    if (e instanceof ModuleError) {
      process.stderr.write(`[mod_node] ${e.message}\n`);
      process.exit(1);
    }
    throw e;
  }
}

main().catch((err) => {
  process.stderr.write(`[mod_node] fatal: ${err.stack || err.message}\n`);
  process.exit(1);
});
