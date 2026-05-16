/**
 * @agtp/agtp-node — public AGTP handler API for Node.js / TypeScript.
 *
 * Mirrors agtp/ (Python) and agtp-php/ (PHP). Handlers are async
 * functions; the runtime client (mod_node) awaits them. Returns are
 * EndpointResponse on success or EndpointError on declared failure;
 * throw for unexpected errors.
 */

export interface EndpointContext {
  readonly input: Record<string, unknown>;
  readonly agentId: string;
  readonly principalId: string;
  readonly agentScopes: readonly string[];
  readonly authorityScope: readonly string[];
  readonly sessionId: string | null;
  readonly taskId: string | null;
  readonly requestId: string;
  readonly method: string;
  readonly path: string;
  readonly headers: Readonly<Record<string, string>>;
}

export class EndpointResponse {
  constructor(
    public readonly body: Record<string, unknown>,
    public readonly status: number = 200,
    public readonly headers: Record<string, string> | null = null,
  ) {}
}

export class EndpointError {
  constructor(
    public readonly code: string,
    public readonly message: string,
    public readonly details: Record<string, unknown> | null = null,
  ) {}
}

export type HandlerResult = EndpointResponse | EndpointError;

export type HandlerFn = (
  ctx: EndpointContext,
) => HandlerResult | Promise<HandlerResult>;

export interface RegisteredHandler {
  readonly method: string;
  readonly path: string;
  readonly handler: HandlerFn;
  readonly errors: readonly string[];
  readonly requiredScopes: readonly string[];
  readonly description: string;
}

export interface RegisterOptions {
  errors?: string[];
  requiredScopes?: string[];
  description?: string;
}

/**
 * Process-wide registry of AGTP handlers, keyed by (method, path).
 * Most apps build one at startup and hand it to mod_node's
 * GatewayClient. For tests, instantiate fresh.
 */
export class HandlerRegistry {
  private readonly handlers = new Map<string, RegisteredHandler>();

  register(
    method: string,
    path: string,
    handler: HandlerFn,
    opts: RegisterOptions = {},
  ): RegisteredHandler {
    const key = this.key(method, path);
    if (this.handlers.has(key)) {
      throw new Error(`handler already registered for (${method.toUpperCase()}, ${path})`);
    }
    const entry: RegisteredHandler = {
      method: method.toUpperCase(),
      path,
      handler,
      errors: opts.errors ?? [],
      requiredScopes: opts.requiredScopes ?? [],
      description: opts.description ?? '',
    };
    this.handlers.set(key, entry);
    return entry;
  }

  lookup(method: string, path: string): RegisteredHandler | undefined {
    return this.handlers.get(this.key(method, path));
  }

  all(): RegisteredHandler[] {
    return [...this.handlers.values()];
  }

  count(): number {
    return this.handlers.size;
  }

  clear(): void {
    this.handlers.clear();
  }

  private key(method: string, path: string): string {
    return `${method.toUpperCase()} ${path}`;
  }
}

/** Default process-wide registry. */
export const registry = new HandlerRegistry();

/**
 * Build a synthetic EndpointContext for unit testing.
 */
export function makeContext(
  overrides: Partial<EndpointContext> = {},
): EndpointContext {
  return {
    input: overrides.input ?? {},
    agentId: overrides.agentId ?? 'test-agent',
    principalId: overrides.principalId ?? '',
    agentScopes: overrides.agentScopes ?? [],
    authorityScope: overrides.authorityScope ?? [],
    sessionId: overrides.sessionId ?? null,
    taskId: overrides.taskId ?? null,
    requestId: overrides.requestId ?? 'test-req-1',
    method: (overrides.method ?? 'QUERY').toUpperCase(),
    path: overrides.path ?? '/',
    headers: overrides.headers ?? {},
  };
}

/**
 * Assert that result is an EndpointResponse; return it. Throws with
 * a clear message on mismatch.
 */
export function assertOk(result: HandlerResult): EndpointResponse {
  if (result instanceof EndpointError) {
    throw new Error(
      `expected EndpointResponse, got EndpointError code=${result.code} message=${result.message}`,
    );
  }
  if (!(result instanceof EndpointResponse)) {
    throw new Error(`expected EndpointResponse, got ${typeof result}`);
  }
  return result;
}

/**
 * Assert that result is an EndpointError; optionally match its code.
 */
export function assertError(
  result: HandlerResult,
  code?: string,
): EndpointError {
  if (result instanceof EndpointResponse) {
    throw new Error(
      `expected EndpointError, got EndpointResponse status=${result.status}`,
    );
  }
  if (!(result instanceof EndpointError)) {
    throw new Error(`expected EndpointError, got ${typeof result}`);
  }
  if (code !== undefined && result.code !== code) {
    throw new Error(
      `expected EndpointError code=${code}, got code=${result.code}`,
    );
  }
  return result;
}
