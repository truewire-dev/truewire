/**
 * Pin packages clause P18: an explicit `proxy` carries HTTP and WebSocket traffic alike.
 *
 * A caller that cannot set the process environment (an agent sandbox, a library embedding
 * the client) must still be able to route both transports through a proxy. So the proxy
 * variables are cleared first, and a local proxy that records what it was asked to reach
 * is given to the client only as `proxy`.
 *
 * The proxy speaks the two forms an HTTP proxy is sent: `CONNECT host:port` (every
 * WebSocket, and HTTP to `https://`) and an absolute-form request line such as
 * `GET http://host:port/path` (HTTP to `http://`). The upstream is one Node HTTP server
 * that answers `/hello` and speaks a subscribe dialect on every WebSocket upgrade.
 */
import { createHash } from 'node:crypto'
import { createServer as createHttpServer, type Server as HttpServer } from 'node:http'
import { connect, createServer, type AddressInfo, type Server, type Socket as NetSocket } from 'node:net'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { LogicError, NetworkError } from '../src/errors.js'
import { HttpClient } from '../src/http.js'
import type { Data } from '../src/ws/socket.js'
import { SerialReplies } from '../src/ws/serial.js'
import { Streams, type ChannelMessage } from '../src/ws/streams.js'

const PROXY_VARIABLES = ['HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'WS_PROXY', 'WSS_PROXY', 'NODE_USE_ENV_PROXY']
const saved = new Map<string, string | undefined>()

beforeAll(() => {
  for (const name of PROXY_VARIABLES.flatMap(n => [n, n.toLowerCase()])) {
    saved.set(name, process.env[name])
    delete process.env[name]
  }
})

afterAll(() => {
  for (const [name, value] of saved) if (value !== undefined) process.env[name] = value
})

// -- the proxy ------------------------------------------------------------------------

/** A local HTTP proxy that tunnels `CONNECT` and forwards absolute-form requests. */
class RecordingProxy {
  /** `METHOD target` of every request line, in arrival order. */
  readonly seen: string[] = []
  /** The `Proxy-Authorization` header of each request line, `undefined` when absent. */
  readonly auth: (string | undefined)[] = []
  readonly server: Server
  readonly sockets = new Set<NetSocket>()

  constructor() {
    this.server = createServer(client => {
      this.track(client)
      client.once('data', buf => this.handle(client, buf))
    })
  }

  get url(): string {
    return `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`
  }

  track(socket: NetSocket): void {
    this.sockets.add(socket)
    socket.on('close', () => this.sockets.delete(socket))
    socket.on('error', () => socket.destroy())
  }

  handle(client: NetSocket, head: Buffer): void {
    const text = head.toString('latin1')
    const [line = '', ...headers] = text.slice(0, text.indexOf('\r\n\r\n')).split('\r\n')
    const [method = '', target = ''] = line.split(' ')
    this.seen.push(`${method} ${target}`)
    this.auth.push(headers.find(h => h.toLowerCase().startsWith('proxy-authorization:'))?.slice(20).trim())
    const pipe = (host: string, port: number, first: (upstream: NetSocket) => void) => {
      const upstream = connect(port, host, () => { first(upstream); upstream.pipe(client); client.pipe(upstream) })
      this.track(upstream)
      upstream.on('close', () => client.destroy())
    }
    if (method === 'CONNECT') {
      const [host = '', port = ''] = target.split(':')
      pipe(host, Number(port), () => client.write('HTTP/1.1 200 Connection established\r\n\r\n'))
    } else {
      const url = new URL(target)
      pipe(url.hostname, Number(url.port), upstream => upstream.write(text.replace(target, url.pathname + url.search), 'latin1'))
    }
  }

  async start(): Promise<this> {
    await new Promise<void>(resolve => this.server.listen(0, '127.0.0.1', resolve))
    return this
  }

  async stop(): Promise<void> {
    for (const socket of this.sockets) socket.destroy()
    await new Promise(resolve => this.server.close(resolve))
  }
}

// -- the upstream ---------------------------------------------------------------------

/** One unmasked server frame. */
function frame(opcode: number, payload: Buffer): Buffer {
  const length = payload.length < 126 ? Buffer.from([payload.length]) : Buffer.from([126, payload.length >> 8, payload.length & 255])
  return Buffer.concat([Buffer.from([0x80 | opcode]), length, payload])
}

/** Split masked client frames off `buffer`; the rest is an incomplete frame. */
function readFrames(buffer: Buffer): { frames: { opcode: number; payload: Buffer }[]; rest: Buffer } {
  const frames: { opcode: number; payload: Buffer }[] = []
  let offset = 0
  while (buffer.length - offset >= 6) {
    let length = buffer[offset + 1]! & 127
    let start = offset + 2
    if (length === 126) { length = buffer.readUInt16BE(start); start += 2 }
    if (buffer.length < start + 4 + length) break
    const mask = buffer.subarray(start, start + 4)
    const payload = Buffer.from(buffer.subarray(start + 4, start + 4 + length).map((b, i) => b ^ mask[i % 4]!))
    frames.push({ opcode: buffer[offset]! & 15, payload })
    offset = start + 4 + length
  }
  return { frames, rest: buffer.subarray(offset) }
}

/** Answers `GET /hello`; on an upgrade, acks each subscribe and pushes two frames on its channel. */
async function startUpstream(): Promise<HttpServer> {
  const server = createHttpServer(async (request, response) => {
    if (request.url === '/echo') {
      const chunks: Buffer[] = []
      for await (const chunk of request) chunks.push(Buffer.from(chunk))
      response.end(JSON.stringify({ method: request.method, headers: request.headers, body: Buffer.concat(chunks).toString() }))
    } else if (request.url === '/redirect') {
      response.writeHead(302, { location: '/hello' }).end()
    } else {
      response.end('hello')
    }
  })
  server.on('upgrade', (request, socket) => {
    const key = request.headers['sec-websocket-key']
    const accept = createHash('sha1').update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64')
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`)
    const send = (msg: unknown) => socket.write(frame(1, Buffer.from(JSON.stringify(msg))))
    let pending: Buffer = Buffer.alloc(0)
    socket.on('data', (chunk: Buffer) => {
      const { frames, rest } = readFrames(Buffer.concat([pending, chunk]))
      pending = rest
      for (const { opcode, payload } of frames) {
        if (opcode === 8) { socket.end(frame(8, payload)); return }
        const msg = JSON.parse(payload.toString()) as { event: string; channel: string }
        send({ event: `${msg.event}d`, channel: msg.channel })
        if (msg.event === 'subscribe') for (const n of [0, 1]) send({ channel: msg.channel, n })
      }
    })
    socket.on('error', () => socket.destroy())
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  return server
}

type Push = { channel: string; n: number }
type Ack = { event: string; channel: string }

/** The dialect `startUpstream` speaks: acks by arrival order, pushes by channel. */
class Feed extends Streams<Push, undefined, Ack, Ack> {
  readonly serial = new SerialReplies<Ack>({ send: m => this.send(m), wait: p => this.wait(p) })
  async send(msg: unknown): Promise<void> {
    (await this.ws).send(JSON.stringify(msg))
  }
  parseMsg(msg: Data): ChannelMessage<Push> | null {
    const obj = JSON.parse(msg as string) as Ack | Push
    if ('event' in obj) { this.serial.replies.push(obj); return null }
    return { channel: obj.channel, notification: obj }
  }
  requestSubscription(channel: string): Promise<Ack> {
    return this.serial.request({ event: 'subscribe', channel })
  }
  requestUnsubscription(channel: string): Promise<Ack> {
    return this.serial.request({ event: 'unsubscribe', channel })
  }
}

// -- the tests ------------------------------------------------------------------------

let upstream: HttpServer
let host: string
let proxy: RecordingProxy

beforeAll(async () => {
  upstream = await startUpstream()
  host = `127.0.0.1:${(upstream.address() as AddressInfo).port}`
})

afterAll(async () => {
  upstream.closeAllConnections()
  await new Promise(resolve => upstream.close(resolve))
})

afterEach(async () => {
  vi.unstubAllGlobals()
  await proxy?.stop()
})

async function freshProxy(): Promise<RecordingProxy> {
  return (proxy = await new RecordingProxy().start())
}

async function hello(http: HttpClient): Promise<string> {
  return (await http.request('GET', `http://${host}/hello`)).text()
}

async function firstPush(feed: Feed, channel: string): Promise<Push> {
  try {
    await using stream = await feed.subscribe(channel)
    for await (const push of stream) return push
    throw new Error('the stream ended')
  } finally {
    await feed.close()
  }
}

describe('proxy', () => {
  it('sends an HTTP request through the given proxy', async () => {
    const { url, seen } = await freshProxy()
    expect(await hello(new HttpClient({ proxy: url }))).toBe('hello')
    expect(seen).toEqual([`GET http://${host}/hello`])
  })

  it('opens a WebSocket subscription through the given proxy, as a CONNECT tunnel', async () => {
    const { url, seen } = await freshProxy()
    expect(await firstPush(new Feed({ url: `ws://${host}/`, proxy: url }), 'ticker')).toEqual({ channel: 'ticker', n: 0 })
    expect(seen).toEqual([`CONNECT ${host}`])
  })

  it('routes both transports through one proxy with no proxy in the environment (the P18 check)', async () => {
    const { url, seen } = await freshProxy()
    const http = new HttpClient({ proxy: url })
    const feed = new Feed({ url: `ws://${host}/`, proxy: url })
    expect(await hello(http)).toBe('hello')
    expect(await firstPush(feed, 'trades')).toEqual({ channel: 'trades', n: 0 })
    expect([...seen].sort()).toEqual([`CONNECT ${host}`, `GET http://${host}/hello`])
    expect([http.proxy, feed.proxy]).toEqual([url, url])
  })

  it('tunnels https:// and wss:// with CONNECT', async () => {
    // The upstream speaks no TLS, so both fail after the tunnel: what matters is that the
    // proxy was asked for it.
    const { url, seen } = await freshProxy()
    await expect(new HttpClient({ proxy: url }).request('GET', `https://${host}/hello`)).rejects.toBeInstanceOf(NetworkError)
    await expect(new Feed({ url: `wss://${host}/`, proxy: url, timeout: 2_000 }).open()).rejects.toBeInstanceOf(NetworkError)
    expect(seen).toEqual([`CONNECT ${host}`, `CONNECT ${host}`])
  })

  it('sends the credentials in the proxy URL as Proxy-Authorization, on both transports', async () => {
    const { url, auth } = await freshProxy()
    const withCredentials = url.replace('http://', 'http://user:pa%20ss@')
    expect(await hello(new HttpClient({ proxy: withCredentials }))).toBe('hello')
    await firstPush(new Feed({ url: `ws://${host}/`, proxy: withCredentials }), 'ticker')
    const basic = `Basic ${Buffer.from('user:pa ss').toString('base64')}`
    expect(auth).toEqual([basic, basic])
  })

  it.each([['omitted', undefined], ['empty', '']])('connects directly when the proxy is %s', async (_, value) => {
    const { seen } = await freshProxy()
    const http = new HttpClient({ proxy: value })
    expect(await hello(http)).toBe('hello')
    expect(await firstPush(new Feed({ url: `ws://${host}/`, proxy: value }), 'ticker')).toEqual({ channel: 'ticker', n: 0 })
    expect(seen).toEqual([])
    expect(http.proxy).toBeUndefined()
  })

  it('fails a connection when the proxy is unreachable: the proxy is really used', async () => {
    const closed = await new RecordingProxy().start()
    const dead = closed.url
    await closed.stop()
    await expect(new HttpClient({ proxy: dead }).request('GET', `http://${host}/hello`)).rejects.toBeInstanceOf(NetworkError)
    await expect(new Feed({ url: `ws://${host}/`, proxy: dead }).open()).rejects.toBeInstanceOf(NetworkError)
  })
})

describe.each(['direct', 'proxy'] as const)('fetch(Request) parity: %s', transport => {
  async function client(): Promise<HttpClient> {
    const { url } = await freshProxy()
    return new HttpClient(transport === 'proxy' ? { proxy: url } : {})
  }

  const valid = `sha256-${createHash('sha256').update('hello').digest('base64')}`
  const invalid = `sha256-${createHash('sha256').update('different content').digest('base64')}`

  it.each([undefined, { integrity: undefined }])('rejects mismatching integrity with init %j', async init => {
    const http = await client()
    await expect(http.fetch(new Request(`http://${host}/hello`, { integrity: invalid }), init))
      .rejects.toMatchObject({ cause: { message: 'integrity mismatch' } })
    expect(proxy.seen).toEqual(transport === 'proxy' ? [`GET http://${host}/hello`] : [])
  })

  it('accepts a matching hash and honors explicit integrity overrides', async () => {
    const http = await client()
    for (const [integrity, init] of [[valid, undefined], [invalid, { integrity: valid }], [invalid, { integrity: '' }]] as const) {
      expect(await (await http.fetch(new Request(`http://${host}/hello`, { integrity }), init)).text()).toBe('hello')
    }
    await expect(http.fetch(new Request(`http://${host}/hello`, { integrity: valid }), { integrity: invalid }))
      .rejects.toMatchObject({ cause: { message: 'integrity mismatch' } })
  })

  it('preserves body, headers, cache and mode on the wire', async () => {
    const http = await client()
    // Older Node typings omit cache from RequestInit, though the runtime supports it.
    const options: RequestInit & { cache: Request['cache'] } = {
      method: 'POST', body: 'original', headers: { 'x-test': 'original' },
      cache: 'no-store', mode: 'same-origin', credentials: 'omit', keepalive: true,
      referrer: `http://${host}/private/path`, referrerPolicy: 'origin',
    }
    const response = await http.fetch(new Request(`http://${host}/echo`, options))
    expect(await response.json()).toMatchObject({
      method: 'POST', body: 'original', headers: {
        'x-test': 'original', 'cache-control': 'no-cache', pragma: 'no-cache',
        'sec-fetch-mode': 'same-origin',
      },
    })
  })

  it('applies explicit referrer and referrer policy overrides', async () => {
    const http = await client()
    const response = await http.fetch(new Request(`http://${host}/echo`), {
      referrer: `http://${host}/private/path`, referrerPolicy: 'origin',
    })
    expect(await response.json()).toMatchObject({ headers: { referer: `http://${host}/` } })
  })

  it('applies init overrides without consuming a replaced body', async () => {
    const http = await client()
    const options: RequestInit & { cache: Request['cache'] } = {
      method: 'POST', body: 'original', headers: { 'x-test': 'original' },
      cache: 'no-store', mode: 'same-origin',
    }
    const request = new Request(`http://${host}/echo`, options)
    const overrides: RequestInit & { cache: Request['cache'] } = {
      method: 'PUT', body: 'replacement', headers: { 'x-test': 'replacement' }, cache: 'reload', mode: 'cors',
    }
    const response = await http.fetch(request, overrides)
    expect(await response.json()).toMatchObject({ method: 'PUT', body: 'replacement', headers: {
      'x-test': 'replacement', 'sec-fetch-mode': 'cors', 'cache-control': 'no-cache',
    } })
    expect(request.bodyUsed).toBe(false)
    expect(await request.text()).toBe('original')
  })

  it('preserves redirect and abort behavior', async () => {
    const http = await client()
    const response = await http.fetch(new Request(`http://${host}/redirect`, { redirect: 'manual' }))
    expect(response.status).toBe(302)
    await response.text()
    await expect(http.fetch(new Request(`http://${host}/redirect`, { redirect: 'error' })))
      .rejects.toMatchObject({ cause: { message: 'unexpected redirect' } })
    const reason = new Error('synthetic abort')
    await expect(http.fetch(new Request(`http://${host}/hello`, { signal: AbortSignal.abort(reason) })))
      .rejects.toBe(reason)
  })

  it('preserves body read failures', async () => {
    const http = await client()
    const reason = new Error('synthetic body failure')
    const body = new ReadableStream<Uint8Array>({ start(c) { c.error(reason) } })
    const options: RequestInit & { duplex: 'half' } = { method: 'POST', body, duplex: 'half' }
    await expect(http.fetch(new Request(`http://${host}/echo`, options))).rejects.toThrow()
  })

  it.each(['returns', 'stalls', 'rejects'] as const)('cancels a pre-aborted upload when producer cancellation %s', async cancellation => {
    const http = await client()
    const reason = new Error('synthetic pre-aborted upload')
    let controller!: ReadableStreamDefaultController<Uint8Array>
    let finishCancellation!: () => void
    const stalled = new Promise<void>(resolve => { finishCancellation = resolve })
    const cancel = vi.fn((_reason: unknown) => {
      if (cancellation === 'stalls') return stalled
      if (cancellation === 'rejects') return Promise.reject(new Error('synthetic cancel failure'))
    })
    const body = new ReadableStream<Uint8Array>({ start(c) { controller = c }, cancel })
    const options: RequestInit & { duplex: 'half' } = {
      method: 'POST', body, duplex: 'half', signal: AbortSignal.abort(reason),
    }
    const pending = http.fetch(new Request(`http://${host}/echo`, options)).then(() => 'resolved', error => error)
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const result = await Promise.race([
        pending,
        new Promise(resolve => { timer = setTimeout(() => resolve('still pending after abort'), 500) }),
      ])
      expect(result).toBe(reason)
      expect(cancel).toHaveBeenCalledExactlyOnceWith(reason)
      // Native Request keeps the source locked until its cancel hook settles,
      // even though fetch has already rejected. Match that lifecycle as well.
      if (cancellation === 'stalls') expect(body.locked).toBe(true)
      finishCancellation()
      await expect.poll(() => body.locked).toBe(false)
      expect(proxy.seen).toEqual([])
    } finally {
      clearTimeout(timer)
      finishCancellation()
      if (cancel.mock.calls.length === 0) controller.close()
    }
  })

  it.each(['returns', 'stalls', 'rejects'] as const)('aborts a stalled upload when producer cancellation %s', async cancellation => {
    const http = await client()
    const abort = new AbortController()
    const reason = new Error('synthetic upload abort')
    let controller!: ReadableStreamDefaultController<Uint8Array>
    const cancel = vi.fn(() => {
      if (cancellation === 'stalls') return new Promise<void>(() => {})
      if (cancellation === 'rejects') return Promise.reject(new Error('synthetic cancel failure'))
    })
    const body = new ReadableStream<Uint8Array>({
      start(c) { controller = c; c.enqueue(new TextEncoder().encode('first chunk')) },
      cancel,
    })
    const options: RequestInit & { duplex: 'half' } = {
      method: 'POST', body, duplex: 'half', signal: abort.signal,
    }
    // This server consumes without replying until the producer finishes.
    const server = createHttpServer((req, res) => { req.resume(); req.on('end', () => res.end('ok')) })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const address = `http://127.0.0.1:${(server.address() as AddressInfo).port}/upload`
    const pending = http.fetch(new Request(address, options)).then(() => 'resolved', error => error)
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      await new Promise(resolve => setTimeout(resolve, 50))
      abort.abort(reason)
      const result = await Promise.race([
        pending,
        new Promise(resolve => { timer = setTimeout(() => resolve('still pending after abort'), 500) }),
      ])
      expect(result).toBe(reason)
      if (transport === 'proxy') expect(cancel).toHaveBeenCalledWith(reason)
    } finally {
      clearTimeout(timer)
      if (cancel.mock.calls.length === 0) controller.close()
      await pending
      server.closeAllConnections()
      await new Promise(resolve => server.close(resolve))
    }
  })
})

describe('proxy misconfiguration is a LogicError at construction, never ignored', () => {
  const fake = 'http://127.0.0.1:9'

  it('refuses proxy together with fetch or createWebSocket', () => {
    expect(() => new HttpClient({ proxy: fake, fetch })).toThrow(LogicError)
    expect(() => new HttpClient({ proxy: fake, fetch })).toThrow('pass `fetch` or `proxy`, not both')
    expect(() => new Feed({ url: 'ws://x.invalid/', proxy: fake, createWebSocket: () => { throw new Error('unused') } }))
      .toThrow('pass `createWebSocket` or `proxy`, not both')
  })

  it.each([
    ['not a URL', 'http://user:secret-password@proxy:port', 'is not a URL'],
    ['a SOCKS URL', 'socks5://user:secret-password@127.0.0.1:1080', 'must be an http:// or https:// URL'],
  ])('refuses %s without repeating it', (_, bad, message) => {
    for (const build of [() => new HttpClient({ proxy: bad }), () => new Feed({ url: 'ws://x.invalid/', proxy: bad })]) {
      expect(build).toThrow(LogicError)
      expect(build).toThrow(message)
      try { build() } catch (e) { expect((e as Error).message).not.toContain('secret-password') }
    }
  })

  it('refuses a proxy outside Node, where the browser applies its own', () => {
    vi.stubGlobal('process', { env: {}, versions: {} })
    expect(() => new HttpClient({ proxy: fake })).toThrow('`proxy` works on Node only; a browser sends through its own proxy settings')
    expect(() => new Feed({ url: 'ws://x.invalid/', proxy: fake })).toThrow('works on Node only')
    expect(() => new HttpClient()).not.toThrow()
  })
})
