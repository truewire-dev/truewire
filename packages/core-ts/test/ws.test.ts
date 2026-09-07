/**
 * Drive `Rpc`, `Streams`, `StreamsRpc` and `SerialReplies` against an in-process WebSocket
 * double speaking a Kraken-like protocol: `req_id`-correlated replies, `channel` pushes,
 * and (for `Streams` + `SerialReplies`) an uncorrelated `event` acknowledgement.
 */
import { describe, expect, it } from 'vitest'
import { LogicError, NetworkError } from '../src/errors.js'
import { Rpc } from '../src/ws/rpc.js'
import { SerialReplies } from '../src/ws/serial.js'
import { type Data } from '../src/ws/socket.js'
import { Streams, Subscription, type ChannelMessage } from '../src/ws/streams.js'
import { StreamsRpc, type Message } from '../src/ws/streamsRpc.js'
import { FakeServer, FakeWebSocket, sentJson, tick } from './fakeWebSocket.js'

interface Reply { req_id: number; method: string; success: boolean; result?: unknown; error?: string }
interface Push { channel: string; symbol?: string; data: unknown }
interface Params { symbol: string }

const text = (msg: Data) => (typeof msg === 'string' ? msg : new TextDecoder().decode(msg))

/** A Kraken-like server: answers `method` calls, acks subscribe/unsubscribe, pushes on demand. */
function krakenServer() {
  const server = new FakeServer({
    onMessage: (message, ws) => {
      const req = JSON.parse(message) as { req_id?: number; method: string; params?: Record<string, unknown>; event?: string; channel?: string }
      if (req.event === 'subscribe' || req.event === 'unsubscribe') {
        ws.reply({ event: `${req.event}d`, channel: req.channel, ok: req.channel !== 'forbidden' })
        return
      }
      const base = { req_id: req.req_id, method: req.method }
      switch (req.method) {
        case 'add': ws.reply({ ...base, success: true, result: (req.params!.a as number) + (req.params!.b as number) }); break
        case 'slow': setTimeout(() => ws.reply({ ...base, success: true, result: 'slow' }), 15); break
        case 'silent': break
        case 'fail': ws.reply({ ...base, success: false, error: 'EGeneral:Invalid' }); break
        case 'subscribe': {
          const channel = req.params!.channel as string
          ws.reply({ ...base, success: channel !== 'forbidden', result: { channel }, error: channel === 'forbidden' ? 'ESubscription:Forbidden' : undefined })
          break
        }
        case 'unsubscribe': ws.reply({ ...base, success: true, result: { channel: req.params!.channel } }); break
        default: ws.reply({ ...base, success: false, error: 'Unknown method' })
      }
    },
  })
  return server
}

class Calc extends Rpc<{ method: string; params?: unknown }, Reply> {
  constructor(readonly server: FakeServer) {
    super({ url: 'wss://ws.example.invalid/v2', createWebSocket: server.create })
  }
  parseResponse(msg: Data): { id: number; reply: Reply } | null {
    const obj = JSON.parse(text(msg)) as Reply
    return 'req_id' in obj ? { id: obj.req_id, reply: obj } : null
  }
  async rpcSend(id: number, req: { method: string; params?: unknown }): Promise<void> {
    (await this.ws).send(JSON.stringify({ ...req, req_id: id }))
  }
}

describe('Rpc', () => {
  it('correlates concurrent requests by id, out of order', async () => {
    const server = krakenServer()
    const rpc = new Calc(server)
    try {
      const [slow, sum] = await Promise.all([
        rpc.rpcRequest({ method: 'slow' }),
        rpc.rpcRequest({ method: 'add', params: { a: 1, b: 2 } }),
      ])
      expect(slow.result).toBe('slow')
      expect(sum.result).toBe(3)
      expect(sentJson(server.last).map(m => (m as { req_id: number }).req_id)).toEqual([0, 1])
      expect(rpc.replies.size).toBe(0)
      expect(rpc.counter).toBe(2)
    } finally {
      await rpc.close()
    }
  })

  it('opens the connection lazily, once, on the first request', async () => {
    const server = krakenServer()
    const rpc = new Calc(server)
    expect(server.sockets).toHaveLength(0)
    await Promise.all([rpc.rpcRequest({ method: 'add', params: { a: 1, b: 1 } }), rpc.rpcRequest({ method: 'add', params: { a: 2, b: 2 } })])
    expect(server.sockets).toHaveLength(1)
    await rpc.close()
    expect(server.last.closeCalls).toHaveLength(1)
  })

  it('ignores a reply with an unknown id and frames that are not replies', async () => {
    const server = krakenServer()
    const rpc = new Calc(server)
    await rpc.open()
    server.last.reply({ req_id: 99, method: 'x', success: true })
    server.last.reply({ channel: 'heartbeat' })
    await tick()
    expect((await rpc.rpcRequest({ method: 'add', params: { a: 2, b: 3 } })).result).toBe(5)
    await rpc.close()
  })

  it('a dropped connection rejects pending requests, and the next request reconnects', async () => {
    const server = krakenServer()
    const rpc = new Calc(server)
    const pending = rpc.rpcRequest({ method: 'silent' })
    await tick()
    server.last.drop()
    await expect(pending).rejects.toBeInstanceOf(NetworkError)
    expect(rpc.replies.size).toBe(0)
    expect((await rpc.rpcRequest({ method: 'add', params: { a: 1, b: 1 } })).result).toBe(2)
    expect(server.sockets).toHaveLength(2)
    await rpc.close()
  })

  it('close() fails pending requests and clears the reply table', async () => {
    const server = krakenServer()
    const rpc = new Calc(server)
    const pending = rpc.rpcRequest({ method: 'silent' })
    await tick()
    await rpc.close()
    await expect(pending).rejects.toThrow('Connection closed')
    expect(rpc.replies.size).toBe(0)
  })

  it('a refused connection is a NetworkError', async () => {
    const rpc = new Calc(new FakeServer({ refuse: true }))
    await expect(rpc.rpcRequest({ method: 'add' })).rejects.toBeInstanceOf(NetworkError)
  })
})

/** `Streams` over a protocol whose acks carry no id: `SerialReplies` matches them by order. */
class Feed extends Streams<Push, Params, { event: string; ok: boolean }, { event: string; ok: boolean }> {
  readonly serial = new SerialReplies<{ event: string; ok: boolean }>({ send: m => this.send(m), wait: p => this.wait(p) })
  constructor(readonly server: FakeServer) {
    super({ url: 'wss://ws.example.invalid/v1', createWebSocket: server.create })
  }
  async send(msg: unknown): Promise<void> {
    (await this.ws).send(JSON.stringify(msg))
  }
  parseMsg(msg: Data): ChannelMessage<Push> | null {
    const obj = JSON.parse(text(msg)) as Record<string, unknown>
    if ('event' in obj) { this.serial.replies.push(obj as { event: string; ok: boolean }); return null }
    if ('channel' in obj) return { channel: obj.channel as string, notification: obj as unknown as Push }
    return null
  }
  async requestSubscription(channel: string, params?: Params) {
    const reply = await this.serial.request({ event: 'subscribe', channel, ...params })
    if (!reply.ok) throw new Error(`subscribe "${channel}" refused`)
    return reply
  }
  requestUnsubscription(channel: string, params?: Params) {
    return this.serial.request({ event: 'unsubscribe', channel, ...params })
  }
}

async function take<T>(iterable: AsyncIterable<T>, n: number): Promise<T[]> {
  const out: T[] = []
  if (n === 0) return out
  for await (const item of iterable) {
    out.push(item)
    if (out.length === n) break
  }
  return out
}

describe('Streams', () => {
  it('awaiting a subscription subscribes and yields pushes in order until unsubscribed', async () => {
    const server = krakenServer()
    const feed = new Feed(server)
    const stream = await feed.subscribe('ticker', { symbol: 'BTC/USD' })
    expect(stream.reply).toEqual({ event: 'subscribed', channel: 'ticker', ok: true })
    expect(sentJson(server.last)).toEqual([{ event: 'subscribe', channel: 'ticker', symbol: 'BTC/USD' }])
    expect(feed.subscriptions.has('ticker')).toBe(true)

    server.push({ channel: 'ticker', data: 1 })
    server.push({ channel: 'ticker', data: 2 })
    server.push({ channel: 'book', data: 'ignored' })
    server.push({ channel: 'ticker', data: 3 })
    const received = take(stream, 3)
    expect((await received).map(p => p.data)).toEqual([1, 2, 3])

    expect(await stream.unsubscribe()).toEqual({ event: 'unsubscribed', channel: 'ticker', ok: true })
    expect(feed.subscriptions.has('ticker')).toBe(false)
    expect(await stream.unsubscribe()).toEqual({ event: 'unsubscribed', channel: 'ticker', ok: true })
    expect(sentJson(server.last)).toHaveLength(2)
    await feed.close()
  })

  it('iterating a Subscription connects on the first pull, and iteration ends on unsubscribe', async () => {
    const server = krakenServer()
    const feed = new Feed(server)
    const subscription = feed.subscribe('ticker', { symbol: 'ETH/USD' })
    expect(server.sockets).toHaveLength(0)
    const items: unknown[] = []
    const iterating = (async () => { for await (const push of subscription) items.push(push.data) })()
    await tick(5)
    expect(subscription.stream).not.toBeNull()
    server.push({ channel: 'ticker', data: 'a' })
    await tick()
    await subscription.stream!.unsubscribe()
    await iterating
    expect(items).toEqual(['a'])
    await feed.close()
  })

  it('await using unsubscribes on scope exit', async () => {
    const server = krakenServer()
    const feed = new Feed(server)
    await (async () => {
      await using stream = feed.subscribe('ticker', { symbol: 'BTC/USD' })
      const first = take(stream, 1)
      await tick(5)
      server.push({ channel: 'ticker', data: 1 })
      expect((await first)[0]!.data).toBe(1)
    })()
    expect(feed.subscriptions.size).toBe(0)
    expect(sentJson(server.last).at(-1)).toEqual({ event: 'unsubscribe', channel: 'ticker', symbol: 'BTC/USD' })
    await feed.close()
  })

  it('disposing a Subscription that never connected does nothing', async () => {
    const feed = new Feed(krakenServer())
    await feed.subscribe('ticker')[Symbol.asyncDispose]()
    expect(feed.isOpen).toBe(false)
  })

  it('messageKey and requestChannel route one wire channel to several local ones', async () => {
    const server = krakenServer()
    const feed = new Feed(server)
    const key = (push: Push) => `ticker:${push.symbol}`
    const btc = await feed.subscribe('ticker:BTC/USD', { symbol: 'BTC/USD' }, { requestChannel: 'ticker', messageKey: key })
    const eth = await feed.subscribe('ticker:ETH/USD', { symbol: 'ETH/USD' }, { requestChannel: 'ticker', messageKey: key })
    expect(sentJson(server.last).map(m => (m as { channel: string }).channel)).toEqual(['ticker', 'ticker'])
    server.push({ channel: 'ticker', symbol: 'ETH/USD', data: 'e' })
    server.push({ channel: 'ticker', symbol: 'BTC/USD', data: 'b' })
    expect((await take(btc, 1))[0]!.data).toBe('b')
    expect((await take(eth, 1))[0]!.data).toBe('e')
    await feed.close()
  })

  it('subscribing twice to one local channel is a LogicError', async () => {
    const feed = new Feed(krakenServer())
    await feed.subscribe('ticker')
    await expect(feed.subscribe('ticker')).rejects.toBeInstanceOf(LogicError)
    await feed.close()
  })

  it('a refused subscription leaves no channel behind', async () => {
    const feed = new Feed(krakenServer())
    await expect(feed.subscribe('forbidden')).rejects.toThrow('refused')
    expect(feed.subscriptions.size).toBe(0)
    await feed.subscribe('forbidden').then(() => { throw new Error('unreachable') }, () => {})
    await feed.close()
  })

  it('map and filter on Subscription and Stream', async () => {
    const server = krakenServer()
    const feed = new Feed(server)
    const evens = feed.subscribe('ticker').map(p => p.data as number).filter(n => n % 2 === 0)
    expect(evens).toBeInstanceOf(Subscription)
    const stream = await evens
    const doubled = stream.map(n => n * 2)
    for (let i = 1; i <= 4; i++) server.push({ channel: 'ticker', data: i })
    expect(await take(doubled, 2)).toEqual([4, 8])
    expect(doubled.reply).toEqual(stream.reply)
    await doubled.unsubscribe()
    expect(feed.subscriptions.size).toBe(0)
    await feed.close()
  })

  it('a dropped connection ends iteration with a NetworkError and forgets the channel', async () => {
    const server = krakenServer()
    const feed = new Feed(server)
    const stream = await feed.subscribe('ticker')
    const pulling = take(stream, 1)
    await tick()
    server.last.drop()
    await expect(pulling).rejects.toBeInstanceOf(NetworkError)
    expect(feed.subscriptions.size).toBe(0)
    await feed.close()
  })

  it('close() clears every subscription', async () => {
    const feed = new Feed(krakenServer())
    await feed.subscribe('a')
    await feed.subscribe('b')
    expect(feed.subscriptions.size).toBe(2)
    await feed.close()
    expect(feed.subscriptions.size).toBe(0)
    expect(feed.isOpen).toBe(false)
  })
})

describe('SerialReplies', () => {
  it('serializes concurrent requests and matches replies by arrival order', async () => {
    const sent: unknown[] = []
    const serial = new SerialReplies<string>({ send: async m => { sent.push(m) } })
    const a = serial.request('a')
    const b = serial.request('b')
    await tick()
    expect(sent).toEqual(['a'])
    serial.replies.push('reply-a')
    expect(await a).toBe('reply-a')
    await tick()
    expect(sent).toEqual(['a', 'b'])
    serial.replies.push('reply-b')
    expect(await b).toBe('reply-b')
    expect(serial.lock.locked).toBe(false)
  })

  it('a failed send releases the lock', async () => {
    const serial = new SerialReplies<string>({ send: async m => { if (m === 'bad') throw new Error('send failed') } })
    await expect(serial.request('bad')).rejects.toThrow('send failed')
    const next = serial.request('good')
    serial.replies.push('ok')
    expect(await next).toBe('ok')
  })

  it('a wait hook can fail a pending reply', async () => {
    const serial = new SerialReplies<string>({ send: async () => {}, wait: () => Promise.reject(new NetworkError('gone')) })
    await expect(serial.request('x')).rejects.toBeInstanceOf(NetworkError)
  })
})

/** Kraken Spot v2: subscribe/unsubscribe acks and method replies all correlated by `req_id`. */
class Spot extends StreamsRpc<{ method: string; params?: Record<string, unknown> }, Reply, Push, Params, Reply, Reply> {
  constructor(readonly server: FakeServer) {
    super({ url: 'wss://ws.example.invalid/v2', createWebSocket: server.create })
  }
  async rpcSend(id: number, req: { method: string; params?: Record<string, unknown> }): Promise<void> {
    (await this.ws).send(JSON.stringify({ ...req, req_id: id }))
  }
  parseMsg(msg: Data): Message<Reply, Push> | null {
    const obj = JSON.parse(text(msg)) as Record<string, unknown>
    if ('req_id' in obj) return { kind: 'response', id: obj.req_id as number, response: obj as unknown as Reply }
    if ('channel' in obj) return { kind: 'subscription', channel: obj.channel as string, notification: obj as unknown as Push }
    return null
  }
  async requestSubscription(channel: string, params?: Params): Promise<Reply> {
    const reply = await this.rpcRequest({ method: 'subscribe', params: { channel, ...params } })
    if (!reply.success) throw new Error(reply.error)
    return reply
  }
  requestUnsubscription(channel: string, params?: Params): Promise<Reply> {
    return this.rpcRequest({ method: 'unsubscribe', params: { channel, ...params } })
  }
}

describe('StreamsRpc', () => {
  it('serves rpc calls and subscriptions on one connection', async () => {
    const server = krakenServer()
    const spot = new Spot(server)
    const stream = await spot.subscribe('ticker', { symbol: 'BTC/USD' })
    expect(stream.reply.result).toEqual({ channel: 'ticker' })
    const sum = await spot.rpcRequest({ method: 'add', params: { a: 20, b: 22 } })
    expect(sum.result).toBe(42)
    server.push({ channel: 'ticker', data: 'tick' })
    expect((await take(stream, 1))[0]!.data).toBe('tick')
    expect((await stream.unsubscribe())!.result).toEqual({ channel: 'ticker' })
    expect(server.sockets).toHaveLength(1)
    expect(sentJson(server.last).map(m => (m as { method: string }).method)).toEqual(['subscribe', 'add', 'unsubscribe'])
    await spot.close()
  })

  it('a refused subscription rejects and is cleaned up', async () => {
    const spot = new Spot(krakenServer())
    await expect(spot.subscribe('forbidden')).rejects.toThrow('ESubscription:Forbidden')
    expect(spot.subscriptions.size).toBe(0)
    expect(spot.replies.size).toBe(0)
    await spot.close()
  })

  it('ignores frames that are neither reply nor push', async () => {
    const server = krakenServer()
    const spot = new Spot(server)
    await spot.open()
    server.last.reply({ event: 'heartbeat' })
    await tick()
    expect((await spot.rpcRequest({ method: 'add', params: { a: 1, b: 1 } })).result).toBe(2)
    await spot.close()
  })

  it('close() clears subscriptions and replies', async () => {
    const server = krakenServer()
    const spot = new Spot(server)
    await spot.subscribe('ticker')
    const pending = spot.rpcRequest({ method: 'silent' })
    await tick()
    await spot.close()
    await expect(pending).rejects.toThrow('Connection closed')
    expect(spot.subscriptions.size).toBe(0)
    expect(spot.replies.size).toBe(0)
    expect((server.last as FakeWebSocket).closeCalls).toHaveLength(1)
  })

  it('a forceOpen override can handshake with wait(reply, ctx) before the connection is current', async () => {
    const server = krakenServer()
    class Authed extends Spot {
      handshake: unknown
      protected override async forceOpen() {
        const ctx = await super.forceOpen()
        this.handshake = await this.wait(this.rpcRequestOn(ctx, { method: 'add', params: { a: 1, b: 1 } }), ctx)
        return ctx
      }
      private async rpcRequestOn(ctx: Awaited<ReturnType<Spot['open']>>, req: { method: string; params?: Record<string, unknown> }) {
        const id = this.counter++
        const { Deferred } = await import('../src/ws/async.js')
        const reply = new Deferred<Reply>()
        this.replies.set(id, reply)
        ctx.ws.send(JSON.stringify({ ...req, req_id: id }))
        try { return await this.wait(reply.promise, ctx) } finally { this.replies.delete(id) }
      }
    }
    const spot = new Authed(server)
    expect((await spot.rpcRequest({ method: 'add', params: { a: 2, b: 2 } })).result).toBe(4)
    expect((spot.handshake as Reply).result).toBe(2)
    expect(server.sockets).toHaveLength(1)
    await spot.close()
  })
})
