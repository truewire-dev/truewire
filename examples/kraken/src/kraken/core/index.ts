/**
 * Hand-written core for the kraken client: the three transports the generated `Kraken`
 * is built from, satisfying `KrakenCore` by shape.
 *
 * `spot_client` is the REST transport (`HttpEndpoint<SpotMeta>`: signing, envelope,
 * errors); `market_client` and `private_client` are the two WebSocket v2 connections
 * (`CommandEndpoint & StreamEndpoint`), the private one sending the token it fetches
 * through the REST transport. `streams.private` and `trading_ws` share the private one.
 * Adapt this directory to the API; the generated code never changes when you do.
 */
import type { HttpClient } from '@truewire/core'
import type { KrakenCore } from '../main.js'
import { TokenCache, type Credentials } from './auth.js'
import { SPOT_WS_AUTH_URL, SPOT_WS_URL, SocketCore, type SocketOptions } from './socket.js'
import { SpotCore } from './spot.js'

export { Nonce, TokenCache, sign, type Credentials, type WsToken } from './auth.js'
export { raiseError, unwrap } from './envelope.js'
export { JSON_BODY_PATHS, SPOT_API_URL, SpotCore, type SpotOptions } from './spot.js'
export {
  RAW_METHODS, SPOT_WS_AUTH_URL, SPOT_WS_URL, SocketConnection, SocketCore,
  type Notification, type Reply, type Request, type SocketOptions,
} from './socket.js'

export interface CoreOptions {
  /** Defaults to `https://api.kraken.com`. */
  baseUrl?: string
  /** Defaults to `wss://ws.kraken.com/v2`. */
  wsUrl?: string
  /** Defaults to `wss://ws-auth.kraken.com/v2`. */
  wsAuthUrl?: string
  /** The key pair; without one the client can only call public endpoints and channels. */
  credentials?: Credentials
  /** Validate responses and pushed messages by default; a call's own `validate` option overrides it. */
  validate?: boolean
  /** The `fetch` wrapper to send through; one is made when omitted. */
  http?: HttpClient
  /** Factory for the raw WebSocket connections; the global `WebSocket` when omitted. */
  createWebSocket?: SocketOptions['createWebSocket']
}

/** The three transports, built together: `new Kraken(new Core({ credentials }))`. */
export class Core implements KrakenCore, AsyncDisposable {
  readonly spot_client: SpotCore
  readonly market_client: SocketCore
  readonly private_client: SocketCore

  constructor(options: CoreOptions = {}) {
    const { validate, createWebSocket } = options
    this.spot_client = new SpotCore({ baseUrl: options.baseUrl, credentials: options.credentials, validate, http: options.http })
    this.market_client = new SocketCore({ url: options.wsUrl ?? SPOT_WS_URL, validate, createWebSocket })
    const token = options.credentials === undefined ? undefined : new TokenCache(() => this.spot_client.getWsToken())
    this.private_client = new SocketCore({
      url: options.wsAuthUrl ?? SPOT_WS_AUTH_URL, tokenSource: token && (() => token.get()), validate, createWebSocket,
    })
  }

  /** Close both WebSocket connections; the next use opens them again. */
  async close(): Promise<void> {
    await Promise.all([this.market_client.close(), this.private_client.close()])
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close()
  }
}
