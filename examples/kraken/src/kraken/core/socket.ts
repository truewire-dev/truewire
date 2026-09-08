/**
 * The Spot WebSocket v2 transport. Kraken correlates every reply -- trading-method
 * replies and subscribe/unsubscribe acks alike -- by the client's `req_id`, so one
 * `StreamsRpc` connection serves both verbs: `request` for `trading_ws.*` and `ping`,
 * `subscribe` for every channel. The private connection differs from the public one only
 * by the token it merges into every outgoing `params`.
 */
import {
  ws, type CommandCall, type CommandEndpoint, type StreamEndpoint, type SubscribeCall, type Subscription,
} from '@truewire/core'
import { raiseError } from './envelope.js'

export const SPOT_WS_URL = 'wss://ws.kraken.com/v2'
export const SPOT_WS_AUTH_URL = 'wss://ws-auth.kraken.com/v2'

/** Commands whose reply is the whole frame rather than its `result`; neither declares an `envelope`. */
export const RAW_METHODS = new Set(['ping', 'batch_cancel'])

/** Kraken closes a connection idle for about a minute; an application-level ping keeps it open. */
const PING_INTERVAL_MS = 30_000

export interface Request {
  method: string
  params: Record<string, unknown>
}

/** The reply to a `req_id`-correlated request: a method call, or a subscribe/unsubscribe ack. */
export interface Reply {
  method: string
  req_id: number
  success?: boolean
  result?: unknown
  error?: string
  time_in?: string
  time_out?: string
}

/** One channel push: a snapshot or update, or the automatic `heartbeat`. */
export interface Notification {
  channel: string
  type?: 'snapshot' | 'update'
  data?: unknown
}

type Params = Record<string, unknown>

export interface SocketOptions extends Omit<ws.SocketOptions, 'url' | 'pingInterval'> {
  /** `SPOT_WS_URL` (the default) for the public connection, `SPOT_WS_AUTH_URL` for the private one. */
  url?: string
  /** The token the private connection sends with every request; none on the public one. */
  tokenSource?: () => Promise<string>
  /** Validate pushed messages and replies by default; a call's own `validate` option overrides it. */
  validate?: boolean
}

/** One connection: frames in and out, in Kraken's dialect. */
export class SocketConnection extends ws.StreamsRpc<Request, Reply, Notification, Params, Reply, Reply> {
  readonly tokenSource: (() => Promise<string>) | undefined

  constructor(options: SocketOptions) {
    super({ ...options, url: options.url ?? SPOT_WS_URL, pingInterval: PING_INTERVAL_MS })
    this.tokenSource = options.tokenSource
  }

  async rpcSend(id: number, request: Request): Promise<void> {
    const params = this.tokenSource ? { ...request.params, token: await this.tokenSource() } : request.params
    ;(await this.ws).send(JSON.stringify({ ...request, params, req_id: id }))
  }

  parseMsg(msg: ws.Data): ws.Message<Reply, Notification> | null {
    const frame = JSON.parse(typeof msg === 'string' ? msg : new TextDecoder().decode(msg)) as Record<string, unknown>
    if ('req_id' in frame) return { kind: 'response', id: frame.req_id as number, response: frame as unknown as Reply }
    if ('channel' in frame) return { kind: 'subscription', channel: frame.channel as string, notification: frame as unknown as Notification }
    return null
  }

  async requestSubscription(channel: string, params?: Params): Promise<Reply> {
    return this.checked('subscribe', await this.rpcRequest({ method: 'subscribe', params: { channel, ...params } }))
  }

  async requestUnsubscription(channel: string, params?: Params): Promise<Reply> {
    return this.checked('unsubscribe', await this.rpcRequest({ method: 'unsubscribe', params: { channel, ...params } }))
  }

  override ping(socket: ws.WebSocketLike): void {
    socket.send(JSON.stringify({ method: 'ping' }))
  }

  /** `reply`, unless it reports a failure. `success` is absent from a whole-frame reply with nothing to fail on (`pong`). */
  private checked(method: string, reply: Reply): Reply {
    if (reply.success === false) raiseError([reply.error ?? `"${method}" failed`])
    return reply
  }
}

/** The transport every `streams.*` and `trading_ws.*` endpoint calls: `CommandEndpoint` and `StreamEndpoint` by shape. */
export class SocketCore implements CommandEndpoint, StreamEndpoint {
  readonly conn: SocketConnection
  readonly validate: boolean

  constructor(options: SocketOptions = {}) {
    this.conn = new SocketConnection(options)
    this.validate = options.validate ?? true
  }

  /** One method call; the reply's `result` (the whole frame for `RAW_METHODS`), validated unless `validate` is off. */
  async request<Req, Res>(call: CommandCall<Req, Res, Record<string, never>>): Promise<Res> {
    const params =
      call.request !== undefined && call.requestCodec !== undefined
        ? (call.requestCodec.dump(call.request) as Params)
        : {}
    const reply = await this.conn.rpcRequest({ method: call.path, params })
    if (reply.success === false) raiseError([reply.error ?? `"${call.path}" failed`])
    const value = RAW_METHODS.has(call.path) ? reply : reply.result
    if (call.responseCodec === undefined) return undefined as Res
    return (call.validate ?? this.validate) ? call.responseCodec.parse(value) : (value as Res)
  }

  /** One channel subscription; each pushed message validated unless `validate` is off. */
  subscribe<Params, Message>(call: SubscribeCall<Params, Message, Record<string, never>>): Subscription<Message> {
    const params =
      call.parameters !== undefined && call.parametersCodec !== undefined
        ? (call.parametersCodec.dump(call.parameters) as Record<string, unknown>)
        : undefined
    const subscription = this.conn.subscribe(call.channel, params)
    const codec = call.messageCodec
    if (codec === undefined || !(call.validate ?? this.validate)) return subscription as Subscription<Message>
    return subscription.map(message => codec.parse(message))
  }

  close(): Promise<void> {
    return this.conn.close()
  }
}
