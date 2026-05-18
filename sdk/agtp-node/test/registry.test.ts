import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  EndpointContext,
  EndpointError,
  EndpointResponse,
  HandlerRegistry,
  assertError,
  assertOk,
  makeContext,
} from '../src/index.js';

describe('HandlerRegistry', () => {
  it('registers and looks up by uppercase method', () => {
    const r = new HandlerRegistry();
    r.register('book', '/room', () => new EndpointResponse({ ok: true }));
    assert.ok(r.lookup('BOOK', '/room'));
    assert.ok(r.lookup('book', '/room'));
  });

  it('rejects duplicate registration', () => {
    const r = new HandlerRegistry();
    r.register('BOOK', '/room', () => new EndpointResponse({}));
    assert.throws(
      () => r.register('BOOK', '/room', () => new EndpointResponse({})),
      /already registered/,
    );
  });

  it('carries options through', () => {
    const r = new HandlerRegistry();
    const entry = r.register('BOOK', '/room', () => new EndpointResponse({}), {
      errors: ['room_unavailable'],
      requiredScopes: ['booking:write'],
      description: 'Books a room.',
    });
    assert.deepEqual(entry.errors, ['room_unavailable']);
    assert.deepEqual(entry.requiredScopes, ['booking:write']);
    assert.equal(entry.description, 'Books a room.');
  });

  it('count and all() report state', () => {
    const r = new HandlerRegistry();
    r.register('QUERY', '/a', () => new EndpointResponse({}));
    r.register('QUERY', '/b', () => new EndpointResponse({}));
    assert.equal(r.count(), 2);
    assert.equal(r.all().length, 2);
  });

  it('clear resets', () => {
    const r = new HandlerRegistry();
    r.register('X', '/y', () => new EndpointResponse({}));
    r.clear();
    assert.equal(r.count(), 0);
  });
});

describe('makeContext', () => {
  it('defaults', () => {
    const ctx = makeContext();
    assert.equal(ctx.method, 'QUERY');
    assert.equal(ctx.path, '/');
    assert.equal(ctx.agentId, 'test-agent');
  });

  it('overrides', () => {
    const ctx = makeContext({
      method: 'book',
      path: '/room',
      input: { value: 'x' },
      authorityScope: ['a', 'b'],
    });
    assert.equal(ctx.method, 'BOOK');
    assert.equal(ctx.input.value, 'x');
    assert.deepEqual(ctx.authorityScope, ['a', 'b']);
  });
});

describe('assertOk / assertError', () => {
  it('assertOk passes responses', () => {
    const r = new EndpointResponse({ ok: true });
    assert.equal(assertOk(r), r);
  });

  it('assertOk throws on error', () => {
    assert.throws(() => assertOk(new EndpointError('x', 'y')), /EndpointError/);
  });

  it('assertError matches code', () => {
    const e = new EndpointError('room_unavailable', '');
    assertError(e, 'room_unavailable');
    assert.throws(() => assertError(e, 'wrong'), /code=room_unavailable/);
  });

  it('assertError throws on response', () => {
    assert.throws(
      () => assertError(new EndpointResponse({})),
      /EndpointResponse/,
    );
  });
});

describe('round-trip handler', () => {
  it('exercises a handler as a plain async function', async () => {
    const r = new HandlerRegistry();
    r.register(
      'BOOK',
      '/room',
      async (ctx: EndpointContext) => {
        if (ctx.input.room_type === 'presidential_suite') {
          return new EndpointError('room_unavailable', 'no');
        }
        return new EndpointResponse({ reservation_id: 'res-1' });
      },
      { errors: ['room_unavailable'] },
    );
    const entry = r.lookup('BOOK', '/room');
    assert.ok(entry);

    const ok = assertOk(await entry.handler(makeContext({ input: { room_type: 'double' } })));
    assert.equal(ok.body.reservation_id, 'res-1');

    const err = assertError(
      await entry.handler(makeContext({ input: { room_type: 'presidential_suite' } })),
      'room_unavailable',
    );
    assert.equal(err.message, 'no');
  });
});
