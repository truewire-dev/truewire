/**
 * Base WebSocket client over the global `WebSocket` (Node 22+, browsers), or any
 * compatible object handed in through `createWebSocket` (the `ws` package on Node 20, a
 * test double).
 */
import { NetworkError } from '../errors.js'
import { Deferred } from './async.js'

/** What a socket receives: text frames as strings, binary frames as `ArrayBuffer`s. */
export type Data = string | ArrayBuffer

/** The subset of the WebSocket interface this module uses. */
export interface WebSocketLike {
  readonly readyState: number
  binaryType?: string
  send(data: string | ArrayBufferLike | ArrayBufferView): void
  close(code?: number, reason?: string): void
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onerror: ((event: unknown) => void) | null
  onclose: ((event: { code?: number; reason?: string }) => void) | null
}

export const CONNECTING = 0, OPEN = 1, CLOSING = 2, CLOSED = 3

/** A live connection, plus the promise `wait` races every request against. */
export interface Context {
  readonly ws: WebSocketLike
  /**
   * Never resolves; rejects once the connection is gone -- closed by the peer, failed,
   * a background task threw -- or `abort` was called.
   */
  readonly closed: Promise<never>
  /** End background work and fail `closed` with `reason`; a no-op after the first call. */
  readonly abort: (reason: Error) => void
}

export interface SocketOptions {
  url: string
  /** Milliseconds allowed to open (and to close) the connection. Default 10 s. */
  timeout?: number
  /** Milliseconds between `ping` calls, when `ping` is implemented. Default 24 h. */
  pingInterval?: number
  /** Factory for the raw connection; defaults to the global `WebSocket`. */
  createWebSocket?: (url: string) => WebSocketLike
}

type WebSocketCtor = new (url: string) => WebSocketLike

/**
 * Base WebSocket client: lazy connection, an optional periodic `ping`, a message listener,
 * and error propagation via `wait(...)`.
 *
 * Requires implementing `onMsg`; override `ping` if the server needs application-level
 * pinging.
 *
 * Concurrency contract: the connection opens on first use and closes on `close()` (or
 * `await using`); many concurrent `wait()` calls are fine.
 *
 * The connection can fail without a request noticing: a promise waiting for a reply the
 * socket will never deliver waits forever. `wait(promise)` races it against the
 * connection's own fate, so a dropped connection rejects it with a `NetworkError`.
 */
export abstract class Socket implements AsyncDisposable {
  readonly url: string
  readonly timeout: number
  readonly pingInterval: number
  protected readonly createWebSocket: (url: string) => WebSocketLike
  #opening: Promise<Context> | null = null
  #current: Context | null = null
  #closing: Promise<void> | null = null

  constructor(options: SocketOptions) {
    this.url = options.url
    this.timeout = options.timeout ?? 10_000
    this.pingInterval = options.pingInterval ?? 86_400_000
    this.createWebSocket = options.createWebSocket ?? (url => new ((globalThis as { WebSocket: WebSocketCtor }).WebSocket)(url))
  }

  /** Handle one incoming message. Throwing fails the connection; see `wait`. */
  abstract onMsg(msg: Data): void

  /** Ping the server; called every `pingInterval` while connected, if implemented. */
  ping?(ws: WebSocketLike): Promise<void> | void

  /** Whether a connection is open or being opened. */
  get isOpen(): boolean {
    return this.#opening !== null
  }

  /** The current connection context, opening one first if none exists yet. */
  get ctx(): Promise<Context> {
    return this.open()
  }

  get ws(): Promise<WebSocketLike> {
    return this.open().then(ctx => ctx.ws)
  }

  /** Open the connection, or return the one already open (reconnecting if the peer closed it). */
  async open(): Promise<Context> {
    if (this.#opening) {
      const ctx = await this.#opening
      if (ctx.ws.readyState >= CLOSING) {
        await this.closeContext(ctx)
        return this.open()
      }
      return ctx
    }
    const opening = this.forceOpen().then(
      ctx => { this.#current = ctx; return ctx },
      e => { if (this.#opening === opening) this.#opening = null; throw e },
    )
    this.#opening = opening
    // `return await`, not `return opening`: resolving an async function with a promise
    // costs extra microtask ticks, which would let a concurrent second caller (already
    // awaiting `#opening` above) proceed first and send its frame ahead of this one.
    return await opening
  }

  /**
   * Connect and hand the live connection to the listener and pinger. An override that
   * needs a handshake can `const ctx = await super.forceOpen()` and then `wait(reply, ctx)`.
   */
  protected async forceOpen(): Promise<Context> {
    const ws = await this.connect()
    return this.attach(ws)
  }

  /** Dial `url`; a failure or a `timeout` is a `NetworkError`. */
  protected connect(): Promise<WebSocketLike> {
    return new Promise<WebSocketLike>((resolve, reject) => {
      let ws: WebSocketLike
      try { ws = this.createWebSocket(this.url) } catch (e) {
        return reject(new NetworkError(`Failed to connect to ${this.url}`, { cause: e }))
      }
      const timer = setTimeout(() => { fail(new Error(`timed out after ${this.timeout} ms`)); ws.close() }, this.timeout)
      const fail = (cause: unknown) => {
        clearTimeout(timer)
        reject(new NetworkError(`Failed to connect to ${this.url}`, { cause }))
      }
      ws.onopen = () => { clearTimeout(timer); resolve(ws) }
      ws.onerror = event => fail((event as { error?: unknown })?.error ?? event)
      ws.onclose = event => fail(new Error(`closed (${event.code ?? ''} ${event.reason ?? ''})`.trim()))
      if (ws.readyState === OPEN) { clearTimeout(timer); resolve(ws) }
    })
  }

  /** Wrap an open connection: route frames to `onMsg`, start the pinger, build the `Context`. */
  protected attach(ws: WebSocketLike): Context {
    const closed = new Deferred<never>()
    closed.promise.catch(() => {})
    ws.binaryType = 'arraybuffer'
    const timer = this.ping
      ? setInterval(() => { Promise.resolve().then(() => this.ping!(ws)).catch(abort) }, this.pingInterval)
      : undefined
    ;(timer as { unref?: () => void } | undefined)?.unref?.()
    const abort = (reason: Error) => {
      if (timer !== undefined) clearInterval(timer)
      ws.onmessage = ws.onerror = ws.onclose = null
      closed.reject(reason)
    }
    ws.onmessage = event => {
      try { this.onMsg(event.data as Data) } catch (e) {
        abort(e instanceof Error ? e : new Error(String(e)))
        ws.close()
      }
    }
    ws.onerror = event => abort(new NetworkError('WebSocket error', { cause: (event as { error?: unknown })?.error ?? event }))
    ws.onclose = event => abort(new NetworkError(`Connection closed (${event.code ?? ''} ${event.reason ?? ''})`.trim()))
    return { ws, closed: closed.promise, abort }
  }

  /** Close the connection if one was opened; do nothing when none ever was. */
  async close(): Promise<void> {
    if (!this.#opening) return
    const ctx = await this.#opening.catch(() => null)
    if (ctx) await this.closeContext(ctx)
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close()
  }

  /** Close `ctx`, once, and forget it so the next use opens afresh. */
  protected closeContext(ctx: Context): Promise<void> {
    if (this.#closing) return this.#closing
    if (this.#current === ctx) { this.#current = null; this.#opening = null }
    this.#closing = this.forceClose(ctx).finally(() => { this.#closing = null })
    return this.#closing
  }

  /** Tear down `ctx`: fail every pending `wait`, close the connection and wait for it to end. */
  protected async forceClose(ctx: Context): Promise<void> {
    ctx.abort(new NetworkError('Connection closed'))
    const { ws } = ctx
    if (ws.readyState === CLOSED) return
    await new Promise<void>(resolve => {
      const timer = setTimeout(resolve, this.timeout)
      ;(timer as { unref?: () => void }).unref?.()
      ws.onclose = () => { clearTimeout(timer); resolve() }
      ws.onerror = null
      ws.close()
    })
  }

  /**
   * Wait for `promise`, rejecting instead if the connection fails first.
   *
   * `ctx` defaults to the current connection (opening one if needed); pass it explicitly
   * only from a `forceOpen` override waiting on its own handshake, where the connection
   * being opened is not yet the current one.
   */
  async wait<T>(promise: Promise<T>, ctx?: Context): Promise<T> {
    const live = ctx ?? await this.ctx
    return Promise.race([promise, live.closed])
  }
}
