/**
 * An in-process stand-in for the WebSocket interface `Socket` uses, plus a "server" that
 * hands them out and can answer, push to, or drop each connection. Events fire on a
 * microtask, so the ordering is the one a real socket shows to its handlers.
 */
import { CLOSED, CLOSING, CONNECTING, OPEN, type WebSocketLike } from '../src/ws/socket.js'

export type Handler = (message: string, socket: FakeWebSocket) => void

export interface FakeServerOptions {
  /** Emit `error` then `close` instead of `open`. */
  refuse?: boolean
  /** Never emit `open` at all. */
  hang?: boolean
  /** Server-side message handler. */
  onMessage?: Handler
}

export class FakeServer {
  readonly sockets: FakeWebSocket[] = []
  onMessage: Handler

  constructor(readonly options: FakeServerOptions = {}) {
    this.onMessage = options.onMessage ?? (() => {})
  }

  /** The `createWebSocket` factory a `Socket` under test is given. */
  readonly create = (url: string): FakeWebSocket => {
    const ws = new FakeWebSocket(url, this)
    this.sockets.push(ws)
    if (this.options.hang) return ws
    queueMicrotask(() => (this.options.refuse ? ws.refuse() : ws.open()))
    return ws
  }

  /** The most recently created connection. */
  get last(): FakeWebSocket {
    const ws = this.sockets.at(-1)
    if (!ws) throw new Error('no connection yet')
    return ws
  }

  /** Push one JSON message to every open connection. */
  push(message: unknown): void {
    for (const ws of this.sockets) if (ws.readyState === OPEN) ws.receive(JSON.stringify(message))
  }
}

export class FakeWebSocket implements WebSocketLike {
  readyState = CONNECTING
  binaryType?: string
  onopen: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  onclose: ((event: { code?: number; reason?: string }) => void) | null = null
  /** Everything the client sent, as text. */
  readonly sent: string[] = []
  /** Every `close()` call the client made. */
  readonly closeCalls: [code?: number, reason?: string][] = []

  constructor(readonly url: string, readonly server: FakeServer) {}

  open(): void {
    this.readyState = OPEN
    this.onopen?.({})
  }

  refuse(): void {
    this.readyState = CLOSED
    this.onerror?.({ error: new Error('ECONNREFUSED') })
    this.onclose?.({ code: 1006, reason: '' })
  }

  send(data: string | ArrayBufferLike | ArrayBufferView): void {
    if (this.readyState !== OPEN) throw new Error('WebSocket is not open')
    const text = typeof data === 'string' ? data : new TextDecoder().decode(data as ArrayBuffer)
    this.sent.push(text)
    queueMicrotask(() => this.server.onMessage(text, this))
  }

  /** Deliver one frame to the client, as the server. */
  receive(data: string | ArrayBuffer): void {
    queueMicrotask(() => this.onmessage?.({ data }))
  }

  /** Reply with JSON to the client. */
  reply(message: unknown): void {
    this.receive(JSON.stringify(message))
  }

  close(code?: number, reason?: string): void {
    this.closeCalls.push([code, reason])
    if (this.readyState >= CLOSING) return
    this.readyState = CLOSING
    queueMicrotask(() => {
      this.readyState = CLOSED
      this.onclose?.({ code: code ?? 1000, reason: reason ?? '' })
    })
  }

  /** The server drops the connection without a handshake. */
  drop(code = 1006, reason = 'dropped'): void {
    this.readyState = CLOSED
    queueMicrotask(() => this.onclose?.({ code, reason }))
  }

  /** The transport reports an error, then closes. */
  fail(error: Error = new Error('transport failure')): void {
    queueMicrotask(() => { this.onerror?.({ error }); this.drop() })
  }
}

/** Parsed JSON of every frame the client sent on `ws`. */
export function sentJson(ws: FakeWebSocket): unknown[] {
  return ws.sent.map(text => JSON.parse(text))
}

/** Let queued microtasks and timers run. */
export const tick = (ms = 0): Promise<void> => new Promise(resolve => setTimeout(resolve, ms))
