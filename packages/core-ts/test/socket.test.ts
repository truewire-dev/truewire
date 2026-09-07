/**
 * Pin `Socket`'s lazy connection contract: constructing and taking ownership opens nothing,
 * first use opens exactly one connection, `close()` closes exactly what was opened (and
 * nothing when nothing was), and `wait` surfaces the connection's failure.
 */
import { describe, expect, it } from 'vitest'
import { NetworkError } from '../src/errors.js'
import { Deferred } from '../src/ws/async.js'
import { Socket, type Context, type Data, type WebSocketLike } from '../src/ws/socket.js'
import { FakeServer, FakeWebSocket, tick } from './fakeWebSocket.js'

/** `Socket` whose transport is a call-recorder instead of a real connection. */
class RecordingSocket extends Socket {
  openCalls = 0
  lastConnection: FakeWebSocket | null = null
  lastClosed: Deferred<never> | null = null

  constructor(readonly server = new FakeServer()) {
    super({ url: 'wss://example.invalid', createWebSocket: server.create })
  }

  onMsg(_msg: Data): void {}

  protected override async forceOpen(): Promise<Context> {
    this.openCalls++
    const ws = await this.connect()
    this.lastConnection = ws as FakeWebSocket
    const closed = new Deferred<never>()
    closed.promise.catch(() => {})
    this.lastClosed = closed
    return { ws, closed: closed.promise, abort: reason => closed.reject(reason) }
  }
}

/** `Socket` whose `forceOpen` throws, to prove a code path never dials out. */
class RaisingSocket extends Socket {
  constructor() {
    super({ url: 'wss://example.invalid', createWebSocket: () => { throw new Error('createWebSocket must not be called') } })
  }

  onMsg(_msg: Data): void {}

  protected override forceOpen(): Promise<Context> {
    throw new Error('forceOpen must not be called')
  }
}

describe('Socket lifecycle', () => {
  it('constructing opens nothing', () => {
    const socket = new RaisingSocket()
    expect(socket.isOpen).toBe(false)
  })

  it('closing a socket that was never used connects nothing', async () => {
    const socket = new RaisingSocket()
    await socket.close()
    await socket[Symbol.asyncDispose]()
    expect(socket.isOpen).toBe(false)
  })

  it('first use opens the socket exactly once', async () => {
    const socket = new RecordingSocket()
    try {
      const ctx = await socket.open()
      expect(socket.openCalls).toBe(1)
      expect(ctx.ws).toBe(socket.lastConnection)
      expect(socket.isOpen).toBe(true)
    } finally {
      await socket.close()
    }
  })

  it('two touches open once, concurrently too', async () => {
    const socket = new RecordingSocket()
    try {
      const [a, b] = await Promise.all([socket.open(), socket.ws])
      await socket.ctx
      expect(a.ws).toBe(b)
      expect(socket.openCalls).toBe(1)
    } finally {
      await socket.close()
    }
  })

  it('close() closes exactly what was opened', async () => {
    const socket = new RecordingSocket()
    await socket.open()
    await socket.close()
    expect(socket.lastConnection!.closeCalls).toHaveLength(1)
    expect(socket.isOpen).toBe(false)
    await socket.close()
    expect(socket.lastConnection!.closeCalls).toHaveLength(1)
  })

  it('an exception in the body still closes what was opened', async () => {
    const socket = new RecordingSocket()
    await expect((async () => {
      await using s = socket
      await s.open()
      throw new Error('boom')
    })()).rejects.toThrow('boom')
    expect(socket.lastConnection!.closeCalls).toHaveLength(1)
  })

  it('reopening after close gets a fresh connection', async () => {
    const socket = new RecordingSocket()
    await socket.open()
    const first = socket.lastConnection!
    await socket.close()
    const ctx = await socket.open()
    expect(socket.openCalls).toBe(2)
    expect(first.closeCalls).toHaveLength(1)
    expect(ctx.ws).toBe(socket.lastConnection)
    expect(ctx.ws).not.toBe(first)
    await socket.close()
  })

  it('a connection the peer closed is reopened on the next use', async () => {
    const socket = new RecordingSocket()
    const first = await socket.open()
    ;(first.ws as FakeWebSocket).drop()
    await tick()
    const second = await socket.open()
    expect(second).not.toBe(first)
    expect(socket.openCalls).toBe(2)
    await socket.close()
  })

  it('wait rejects with the connection failure instead of hanging', async () => {
    const socket = new RecordingSocket()
    const never = new Promise<never>(() => {})
    const waiting = socket.wait(never)
    await socket.open()
    socket.lastClosed!.reject(new NetworkError('dropped'))
    await expect(waiting).rejects.toBeInstanceOf(NetworkError)
    await socket.close()
  })

  it('a pending wait fails when the socket is closed deliberately', async () => {
    const socket = new RecordingSocket()
    await socket.open()
    const waiting = socket.wait(new Promise<never>(() => {}))
    await socket.close()
    await expect(waiting).rejects.toThrow('Connection closed')
  })

  it('wait accepts the context a forceOpen override is still building', async () => {
    const socket = new RecordingSocket()
    const ws = await socket.connectPublic()
    const ctx = socket.attachPublic(ws)
    await expect(socket.wait(Promise.resolve(42), ctx)).resolves.toBe(42)
    expect(socket.isOpen).toBe(false)
    ctx.abort(new NetworkError('done'))
    ws.close()
  })
})

interface RecordingSocket {
  connectPublic(): Promise<WebSocketLike>
  attachPublic(ws: WebSocketLike): Context
}
RecordingSocket.prototype.connectPublic = function (this: RecordingSocket) { return this['connect']() }
RecordingSocket.prototype.attachPublic = function (this: RecordingSocket, ws: WebSocketLike) { return this['attach'](ws) }

describe('Socket transport', () => {
  class Echo extends Socket {
    received: Data[] = []
    pings = 0
    constructor(server: FakeServer, options: { timeout?: number; pingInterval?: number; withPing?: boolean } = {}) {
      super({ url: 'wss://example.invalid', createWebSocket: server.create, timeout: options.timeout, pingInterval: options.pingInterval })
      if (options.withPing) this.ping = async () => { this.pings++ }
    }
    onMsg(msg: Data): void { this.received.push(msg) }
  }

  it('routes frames to onMsg', async () => {
    const server = new FakeServer()
    const socket = new Echo(server)
    const ctx = await socket.open()
    server.last.receive('hello')
    server.last.receive(new TextEncoder().encode('bin').buffer)
    await tick()
    expect(socket.received[0]).toBe('hello')
    expect(new TextDecoder().decode(socket.received[1] as ArrayBuffer)).toBe('bin')
    expect(ctx.ws.binaryType).toBe('arraybuffer')
    await socket.close()
  })

  it('a refused connection is a NetworkError, and the next open retries', async () => {
    const server = new FakeServer({ refuse: true })
    const socket = new Echo(server)
    await expect(socket.open()).rejects.toBeInstanceOf(NetworkError)
    expect(socket.isOpen).toBe(false)
    server.options.refuse = false
    await socket.open()
    expect(server.sockets).toHaveLength(2)
    await socket.close()
  })

  it('a connection that never opens times out into a NetworkError', async () => {
    const server = new FakeServer({ hang: true })
    const socket = new Echo(server, { timeout: 10 })
    await expect(socket.open()).rejects.toThrow(/Failed to connect/)
    expect(server.last.closeCalls).toHaveLength(1)
  })

  it('an unexpected close fails the context and pending waits', async () => {
    const server = new FakeServer()
    const socket = new Echo(server)
    const ctx = await socket.open()
    const waiting = socket.wait(new Promise<never>(() => {}))
    await tick()
    server.last.drop(1006, 'gone')
    await expect(waiting).rejects.toThrow('Connection closed (1006 gone)')
    await expect(ctx.closed).rejects.toBeInstanceOf(NetworkError)
    await socket.close()
  })

  it('a transport error fails the context', async () => {
    const server = new FakeServer()
    const socket = new Echo(server)
    const ctx = await socket.open()
    server.last.fail()
    await expect(ctx.closed).rejects.toThrow('WebSocket error')
    await socket.close()
  })

  it('an exception thrown by onMsg fails the context and closes the socket', async () => {
    const server = new FakeServer()
    const socket = new Echo(server)
    socket.onMsg = () => { throw new Error('bad frame') }
    const ctx = await socket.open()
    server.last.receive('x')
    await expect(ctx.closed).rejects.toThrow('bad frame')
    expect(server.last.closeCalls).toHaveLength(1)
    await socket.close()
  })

  it('pings on the interval when ping is implemented', async () => {
    const server = new FakeServer()
    const socket = new Echo(server, { pingInterval: 5, withPing: true })
    await socket.open()
    await tick(30)
    expect(socket.pings).toBeGreaterThanOrEqual(2)
    await socket.close()
    const pings = socket.pings
    await tick(15)
    expect(socket.pings).toBe(pings)
  })

  it('a failing ping fails the context', async () => {
    const server = new FakeServer()
    const socket = new Echo(server, { pingInterval: 5, withPing: true })
    socket.ping = async () => { throw new Error('ping failed') }
    const ctx = await socket.open()
    await expect(ctx.closed).rejects.toThrow('ping failed')
    await socket.close()
  })
})
