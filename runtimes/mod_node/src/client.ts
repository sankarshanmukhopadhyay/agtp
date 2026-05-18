/**
 * Module-side gateway client for Node.js.
 *
 * Port of mod_python/client.py and mod_go/client/client.go.
 * Connects to agtpd over a Unix socket or TCP loopback, performs the
 * handshake, receives the daemon's endpoint registration, dispatches
 * request frames to the local HandlerRegistry.
 *
 * Async-first: handlers may return either an EndpointResponse /
 * EndpointError directly or a Promise resolving to one. The client
 * awaits before writing the response frame.
 */

import { Socket } from 'node:net';
import { createConnection } from 'node:net';

import {
  EndpointContext,
  EndpointError,
  EndpointResponse,
  HandlerRegistry,
  RegisteredHandler,
} from '@agtp/agtp-node';

import {
  FrameDecodeError,
  FrameTooLargeError,
  GATEWAY_VERSION,
  readFrame,
  writeFrame,
} from './protocol.js';

export class ModuleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ModuleError';
  }
}

export interface GatewayClientOptions {
  socketPath: string;
  registry: HandlerRegistry;
  moduleId?: string;
  moduleVersion?: string;
  cachedManifestHash?: string;
}

export class GatewayClient {
  readonly socketPath: string;
  readonly registry: HandlerRegistry;
  readonly moduleId: string;
  readonly moduleVersion: string;
  cachedManifestHash: string;

  private socket: Socket | null = null;
  private stop = false;
  private bindings = new Map<string, RegisteredHandler>();
  private cachedBindings = new Map<string, RegisteredHandler>();

  constructor(opts: GatewayClientOptions) {
    this.socketPath = opts.socketPath;
    this.registry = opts.registry;
    this.moduleId = opts.moduleId ?? 'mod_node';
    this.moduleVersion = opts.moduleVersion ?? '0.1.0';
    this.cachedManifestHash = opts.cachedManifestHash ?? '';
  }

  async run(): Promise<void> {
    await this.connect();
    try {
      await this.handshake();
      await this.serveLoop();
    } finally {
      this.close();
    }
  }

  shutdown(): void {
    this.stop = true;
    // Force the read loop to wake up by destroying the socket.
    if (this.socket) {
      this.socket.destroy();
    }
  }

  private connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      let socket: Socket;
      if (this.isHostPort(this.socketPath)) {
        const [host, portStr] = this.socketPath.split(':');
        socket = createConnection({ host, port: parseInt(portStr, 10) });
      } else {
        socket = createConnection(this.socketPath);
      }
      // Pause so we can drive reads explicitly via readFrame.
      socket.pause();
      socket.once('connect', () => {
        this.socket = socket;
        resolve();
      });
      socket.once('error', (err) => reject(err));
    });
  }

  private close(): void {
    if (this.socket) {
      this.socket.destroy();
      this.socket = null;
    }
  }

  private async handshake(): Promise<void> {
    if (!this.socket) throw new ModuleError('socket not connected');
    const hello: Record<string, unknown> = {
      type: 'hello',
      gateway_versions: [GATEWAY_VERSION],
      module: {
        id: this.moduleId,
        version: this.moduleVersion,
        runtime: `Node ${process.version}`,
        pid: process.pid,
      },
      capabilities: ['registered_function'],
    };
    if (this.cachedManifestHash) {
      hello.cached_manifest_hash = this.cachedManifestHash;
    }
    await writeFrame(this.socket, hello);

    const welcome = await readFrame(this.socket);
    if (welcome.type === 'error') {
      throw new ModuleError(
        `daemon refused handshake: ${welcome.code}: ${welcome.message}`,
      );
    }
    if (welcome.type !== 'welcome') {
      throw new ModuleError(`expected welcome, got type=${String(welcome.type)}`);
    }
    if (welcome.gateway_version !== GATEWAY_VERSION) {
      throw new ModuleError(
        `daemon chose gateway version ${String(welcome.gateway_version)}; this module speaks ${GATEWAY_VERSION}`,
      );
    }

    const register = await readFrame(this.socket);
    if (register.type === 'register_resume') {
      await this.handleRegisterResume(register);
    } else if (register.type === 'register') {
      await this.handleRegister(register);
    } else {
      throw new ModuleError(
        `expected register or register_resume, got type=${String(register.type)}`,
      );
    }
  }

  private async handleRegister(register: Record<string, unknown>): Promise<void> {
    if (!this.socket) throw new ModuleError('socket not connected');
    const manifestHash = String(register.manifest_hash ?? '');
    const endpoints = (register.endpoints as Array<Record<string, unknown>>) ?? [];
    const resolved: string[] = [];
    const errors: Array<Record<string, unknown>> = [];
    const newBindings = new Map<string, RegisteredHandler>();

    for (const ep of endpoints) {
      const method = String(ep.method ?? '').toUpperCase();
      const path = String(ep.path ?? '/');
      const ref = String(ep.handler_reference ?? '');
      const entry = this.registry.lookup(method, path);
      if (!entry) {
        errors.push({
          endpoint: `${method} ${path}`,
          reason: 'handler_not_found',
          detail: `no registration matches (${method}, ${path}); reference was ${JSON.stringify(ref)}`,
        });
        continue;
      }
      newBindings.set(`${method} ${path}`, entry);
      resolved.push(`${method} ${path}`);
    }

    if (errors.length > 0) {
      await writeFrame(this.socket, {
        type: 'register_ack',
        ok: false,
        errors,
      });
      throw new ModuleError(
        `could not resolve ${errors.length} endpoint reference(s): ${JSON.stringify(errors)}`,
      );
    }

    this.bindings = newBindings;
    this.cachedBindings = new Map(newBindings);
    this.cachedManifestHash = manifestHash;
    await writeFrame(this.socket, {
      type: 'register_ack',
      ok: true,
      resolved,
    });
  }

  private async handleRegisterResume(register: Record<string, unknown>): Promise<void> {
    if (!this.socket) throw new ModuleError('socket not connected');
    const manifestHash = String(register.manifest_hash ?? '');
    if (this.cachedBindings.size === 0 || manifestHash !== this.cachedManifestHash) {
      await writeFrame(this.socket, {
        type: 'register_ack',
        ok: false,
        errors: [{
          endpoint: '*',
          reason: 'cache_miss',
          detail: `no cached bindings for manifest_hash=${JSON.stringify(manifestHash)}`,
        }],
      });
      throw new ModuleError(`register_resume cache miss for hash ${manifestHash}`);
    }
    this.bindings = new Map(this.cachedBindings);
    const resolved = [...this.bindings.keys()];
    await writeFrame(this.socket, {
      type: 'register_ack',
      ok: true,
      resolved,
    });
  }

  private async serveLoop(): Promise<void> {
    if (!this.socket) throw new ModuleError('socket not connected');
    while (!this.stop) {
      let frame: Record<string, unknown>;
      try {
        frame = await readFrame(this.socket);
      } catch (e) {
        if (e instanceof FrameDecodeError || e instanceof FrameTooLargeError) {
          return;
        }
        // Socket destroyed or other I/O failure — exit cleanly.
        return;
      }
      const type = frame.type;
      if (type === 'goodbye') return;
      if (type === 'ping') {
        await writeFrame(this.socket, {
          type: 'pong',
          nonce: String(frame.nonce ?? ''),
        });
        continue;
      }
      if (type !== 'request') {
        await writeFrame(this.socket, {
          type: 'error',
          code: 'phase_violation',
          message: `unexpected frame type ${JSON.stringify(type)}`,
        });
        continue;
      }
      await this.handleRequest(frame);
    }
  }

  private async handleRequest(frame: Record<string, unknown>): Promise<void> {
    if (!this.socket) throw new ModuleError('socket not connected');
    const requestId = String(frame.request_id ?? '');
    const envelope = (frame.envelope as Record<string, unknown>) ?? {};
    const method = String(envelope.method ?? '').toUpperCase();
    const path = String(envelope.path ?? '/');
    const entry = this.bindings.get(`${method} ${path}`);
    if (!entry) {
      await writeFrame(this.socket, {
        type: 'error',
        request_id: requestId,
        code: 'handler_exception',
        message: `no handler bound for (${method}, ${path})`,
      });
      return;
    }

    const ctx: EndpointContext = {
      input: (envelope.input as Record<string, unknown>) ?? {},
      agentId: String(envelope.agent_id ?? ''),
      principalId: String(envelope.principal_id ?? ''),
      agentScopes: (envelope.agent_scopes as string[]) ?? [],
      authorityScope: (envelope.authority_scope as string[]) ?? [],
      sessionId: typeof envelope.session_id === 'string' ? envelope.session_id : null,
      taskId: typeof envelope.task_id === 'string' ? envelope.task_id : null,
      requestId: String(envelope.request_id ?? requestId),
      method,
      path,
      headers: (envelope.headers as Record<string, string>) ?? {},
    };

    let result: unknown;
    try {
      result = await entry.handler(ctx);
    } catch (e) {
      const err = e as Error;
      await writeFrame(this.socket, {
        type: 'error',
        request_id: requestId,
        code: 'handler_exception',
        message: `${err.name}: ${err.message}`,
        details: { exception_type: err.name },
      });
      return;
    }

    if (result instanceof EndpointResponse) {
      const respEnv: Record<string, unknown> = {
        body: result.body,
        status: result.status,
      };
      if (result.headers) respEnv.headers = result.headers;
      await writeFrame(this.socket, {
        type: 'response',
        request_id: requestId,
        envelope: respEnv,
      });
      return;
    }
    if (result instanceof EndpointError) {
      const errEnv: Record<string, unknown> = {
        code: result.code,
        message: result.message,
      };
      if (result.details !== null) errEnv.details = result.details;
      await writeFrame(this.socket, {
        type: 'response',
        request_id: requestId,
        envelope: { endpoint_error: errEnv },
      });
      return;
    }
    await writeFrame(this.socket, {
      type: 'error',
      request_id: requestId,
      code: 'handler_exception',
      message: `handler returned unexpected type ${typeof result}`,
    });
  }

  private isHostPort(s: string): boolean {
    if (!s.includes(':')) return false;
    const [host] = s.split(':');
    return host === 'localhost' || host === '127.0.0.1' || host === '::1';
  }
}
