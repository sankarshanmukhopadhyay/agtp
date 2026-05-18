/**
 * AGTP gateway protocol frame codec for Node.js.
 *
 * 4-byte big-endian unsigned length prefix, then UTF-8 JSON,
 * max 16 MiB. Mirrors core/gateway/protocol.py.
 */

import { Readable, Writable } from 'node:stream';

export const GATEWAY_VERSION = '1.0';
export const MAX_FRAME_SIZE = 16 * 1024 * 1024;

export class FrameDecodeError extends Error {
  constructor(reason: string) {
    super(`frame decode: ${reason}`);
    this.name = 'FrameDecodeError';
  }
}

export class FrameTooLargeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'FrameTooLargeError';
  }
}

/**
 * Read exactly `n` bytes from a readable stream that's in non-flowing
 * (paused) mode. Resolves with the chunk; rejects on EOF.
 */
async function readExact(stream: Readable, n: number): Promise<Buffer> {
  if (n <= 0) {
    return Buffer.alloc(0);
  }
  const chunks: Buffer[] = [];
  let collected = 0;
  while (collected < n) {
    const remaining = n - collected;
    const chunk = stream.read(remaining) as Buffer | null;
    if (chunk === null) {
      // Wait for more data or end.
      await new Promise<void>((resolve, reject) => {
        const onReadable = () => {
          cleanup();
          resolve();
        };
        const onEnd = () => {
          cleanup();
          reject(new FrameDecodeError(`connection closed mid-frame (expected ${n} bytes, got ${collected})`));
        };
        const onError = (err: Error) => {
          cleanup();
          reject(err);
        };
        const cleanup = () => {
          stream.off('readable', onReadable);
          stream.off('end', onEnd);
          stream.off('error', onError);
        };
        stream.once('readable', onReadable);
        stream.once('end', onEnd);
        stream.once('error', onError);
      });
      continue;
    }
    chunks.push(chunk);
    collected += chunk.length;
  }
  return Buffer.concat(chunks, collected);
}

export async function readFrame(stream: Readable): Promise<Record<string, unknown>> {
  const header = await readExact(stream, 4);
  const length = header.readUInt32BE(0);
  if (length > MAX_FRAME_SIZE) {
    throw new FrameTooLargeError(
      `frame length ${length} exceeds MAX_FRAME_SIZE ${MAX_FRAME_SIZE}`,
    );
  }
  if (length === 0) {
    throw new FrameDecodeError('empty frame (length=0)');
  }
  const body = await readExact(stream, length);
  let payload: unknown;
  try {
    payload = JSON.parse(body.toString('utf-8'));
  } catch (e) {
    throw new FrameDecodeError(`not valid JSON: ${(e as Error).message}`);
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new FrameDecodeError('frame body must be a JSON object');
  }
  const obj = payload as Record<string, unknown>;
  if (!('type' in obj)) {
    throw new FrameDecodeError("missing required 'type' field");
  }
  return obj;
}

export function writeFrame(
  stream: Writable,
  payload: Record<string, unknown>,
): Promise<void> {
  if (!('type' in payload)) {
    throw new Error("frame payload must carry a 'type' field");
  }
  const body = Buffer.from(JSON.stringify(payload), 'utf-8');
  if (body.length > MAX_FRAME_SIZE) {
    throw new FrameTooLargeError(
      `encoded frame size ${body.length} exceeds MAX_FRAME_SIZE ${MAX_FRAME_SIZE}`,
    );
  }
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length, 0);
  return new Promise<void>((resolve, reject) => {
    stream.write(Buffer.concat([header, body]), (err) => {
      if (err) reject(err);
      else resolve();
    });
  });
}
