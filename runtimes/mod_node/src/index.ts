export { GatewayClient, ModuleError } from './client.js';
export type { GatewayClientOptions } from './client.js';
export {
  FrameDecodeError,
  FrameTooLargeError,
  GATEWAY_VERSION,
  MAX_FRAME_SIZE,
  readFrame,
  writeFrame,
} from './protocol.js';
