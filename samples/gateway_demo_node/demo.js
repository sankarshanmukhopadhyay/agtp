#!/usr/bin/env node
// Sample Node.js handler binary for the gateway-protocol e2e test.
//
// Registers two handlers (echo + book_room) and runs the gateway
// client. Mirrors samples/gateway_demo.py and samples/gateway_demo.php.

import process from 'node:process';

import {
  EndpointError,
  EndpointResponse,
  HandlerRegistry,
} from '@agtp/agtp-node';
import { GatewayClient, ModuleError } from '@agtp/mod-node';

function parseArgs(argv) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--gateway-socket') return argv[i + 1];
  }
  return null;
}

const socket = parseArgs(process.argv.slice(2));
if (!socket) {
  process.stderr.write('[gateway-demo-node] --gateway-socket is required\n');
  process.exit(2);
}

const registry = new HandlerRegistry();

registry.register('QUERY', '/echo', async (ctx) => {
  return new EndpointResponse({ echo: String(ctx.input.value ?? '') });
});

registry.register(
  'BOOK',
  '/room',
  async (ctx) => {
    const roomType = String(ctx.input.room_type ?? 'double');
    if (roomType === 'presidential_suite') {
      return new EndpointError(
        'room_unavailable',
        'The presidential suite is not available.',
        { room_type: roomType },
      );
    }
    const guest = String(ctx.input.guest ?? 'anon');
    return new EndpointResponse({
      reservation_id: `res-${guest}-${roomType}`,
      agent: ctx.agentId,
    });
  },
  { errors: ['room_unavailable'] },
);

const client = new GatewayClient({
  socketPath: socket,
  registry,
  moduleId: 'gateway-demo-node',
});

process.on('SIGTERM', () => client.shutdown());
process.on('SIGINT', () => client.shutdown());

try {
  await client.run();
} catch (e) {
  if (e instanceof ModuleError) {
    process.stderr.write(`[gateway-demo-node] ${e.message}\n`);
    process.exit(1);
  }
  throw e;
}
