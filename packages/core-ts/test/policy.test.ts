/**
 * Pins `HttpClient({ rate, retry })`: the runtime side of `[policy].rate` and
 * `[policy].retry` (workspace clause W15).
 *
 * Every test talks to a real local HTTP server that answers from a script, so pacing is
 * measured on requests that crossed a socket and a retry is a second request the server saw.
 */
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NetworkError } from '../src/errors.js'
import { HttpClient, RETRY_ATTEMPTS, type Exchange } from '../src/http.js'

type Step = [status: number, headers?: Record<string, string>]

interface Scripted {
  url: string
  /** `performance.now()` of every request's arrival, in order. */
  starts: number[]
  server: Server
}

const servers: Server[] = []
afterEach(async () => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  await Promise.all(servers.splice(0).map(s => new Promise(resolve => s.close(resolve))))
})

/** A server answering each request with the next step of `script`, then 200s. */
async function serve(script: Step[] = [], port = 0): Promise<Scripted> {
  const starts: number[] = []
  const server = createServer((request, response) => {
    starts.push(performance.now())
    request.resume()
    const [status, headers] = script.shift() ?? [200]
    response.writeHead(status, headers).end('ok')
  })
  servers.push(server)
  await new Promise<void>(resolve => server.listen(port, '127.0.0.1', resolve))
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/`, starts, server }
}

async function freePort(): Promise<number> {
  const probe = createServer()
  await new Promise<void>(resolve => probe.listen(0, '127.0.0.1', resolve))
  const { port } = probe.address() as AddressInfo
  await new Promise(resolve => probe.close(resolve))
  return port
}

describe('rate', () => {
  it('ten calls at rate 5 take at least 1.8 s', async () => {
    const { url, starts } = await serve()
    const http = new HttpClient({ rate: 5 })
    const began = performance.now()
    for (let i = 0; i < 10; i++) expect((await http.request('GET', url)).status).toBe(200)
    const elapsed = performance.now() - began
    expect(elapsed).toBeGreaterThanOrEqual(1800)
    expect(starts).toHaveLength(10)
  })

  it('concurrent callers share the pace', async () => {
    const { url } = await serve()
    const http = new HttpClient({ rate: 5 })
    const began = performance.now()
    const replies = await Promise.all(Array.from({ length: 10 }, () => http.request('GET', url)))
    expect(replies.map(r => r.status)).toEqual(Array(10).fill(200))
    expect(performance.now() - began).toBeGreaterThanOrEqual(1800)
  })

  it('a fractional rate spaces by its inverse', async () => {
    const { url } = await serve()
    const http = new HttpClient({ rate: 2.5 })
    const began = performance.now()
    for (let i = 0; i < 3; i++) await http.request('GET', url)
    expect(performance.now() - began).toBeGreaterThanOrEqual(800)
  })

  it('no rate sends at once', async () => {
    const { url } = await serve()
    const http = new HttpClient()
    const began = performance.now()
    for (let i = 0; i < 10; i++) await http.request('GET', url)
    expect(performance.now() - began).toBeLessThan(1000)
  })

  it.each([0, -1, Infinity, NaN])('refuses rate %s', rate => {
    expect(() => new HttpClient({ rate })).toThrow(RangeError)
  })

  it('an abort while waiting for the slot rejects with its reason', async () => {
    const { url, starts } = await serve()
    const http = new HttpClient({ rate: 1 })
    await http.request('GET', url)
    const controller = new AbortController()
    const pending = http.request('GET', url, { signal: controller.signal })
    controller.abort(new Error('stop'))
    await expect(pending).rejects.toThrow('stop')
    expect(starts).toHaveLength(1)
  })
})

describe('retry', () => {
  it('503 then 200 succeeds with retry', async () => {
    const { url, starts } = await serve([[503], [200]])
    const response = await new HttpClient({ retry: true }).request('GET', url)
    expect(response.status).toBe(200)
    expect(starts).toHaveLength(2)
  })

  it('503 then 200 fails without retry', async () => {
    const { url, starts } = await serve([[503], [200]])
    const response = await new HttpClient({ retry: false }).request('GET', url)
    expect(response.status).toBe(503)
    expect(starts).toHaveLength(1)
  })

  it('429 is retried after its Retry-After seconds, body and all', async () => {
    const { url, starts } = await serve([[429, { 'Retry-After': '1' }]])
    const response = await new HttpClient({ retry: true }).request('POST', url, { json: { a: 1 } })
    expect(response.status).toBe(200)
    expect(starts[1]! - starts[0]!).toBeGreaterThanOrEqual(950)
  })

  it('Retry-After as an HTTP date', async () => {
    const when = new Date(Date.now() + 2000).toUTCString()
    const deadline = performance.now() + Date.parse(when) - Date.now()
    const { url, starts } = await serve([[503, { 'Retry-After': when }]])
    const response = await new HttpClient({ retry: true }).request('GET', url)
    expect(response.status).toBe(200)
    // Client setup can consume part of an absolute HTTP-date wait.
    expect(starts[1]!).toBeGreaterThanOrEqual(deadline - 50)
  })

  it('a Retry-After over the cap returns the reply', async () => {
    const { url, starts } = await serve([[503, { 'Retry-After': '3600' }]])
    const response = await new HttpClient({ retry: true }).request('GET', url)
    expect(response.status).toBe(503)
    expect(starts).toHaveLength(1)
  })

  it('attempts are bounded', async () => {
    const { url, starts } = await serve(Array.from({ length: RETRY_ATTEMPTS + 1 }, (): Step => [503, { 'Retry-After': '0' }]))
    const response = await new HttpClient({ retry: true }).request('GET', url)
    expect(response.status).toBe(503)
    expect(starts).toHaveLength(RETRY_ATTEMPTS)
  })

  it.each([500, 502, 504, 401, 404])('does not retry %s', async status => {
    const { url, starts } = await serve([[status, { 'Retry-After': '0' }]])
    const response = await new HttpClient({ retry: true }).request('GET', url)
    expect(response.status).toBe(status)
    expect(starts).toHaveLength(1)
  })

  it('records every attempt', async () => {
    const { url } = await serve([[503, { 'Retry-After': '0' }]])
    const exchanges: Exchange[] = []
    await new HttpClient({ retry: true, onExchange: x => exchanges.push(x) }).request('GET', url)
    expect(exchanges.map(x => x.response.status)).toEqual([503, 200])
  })

  it('paces retries', async () => {
    const { url, starts } = await serve([[503, { 'Retry-After': '0' }]])
    const began = performance.now()
    await new HttpClient({ rate: 2, retry: true }).request('GET', url)
    // Timed at the caller: pacing spaces the sends, and a late first arrival shortens the
    // gap the server sees.
    expect(performance.now() - began).toBeGreaterThanOrEqual(490)
    expect(starts).toHaveLength(2)
  })

  it('retries a refused connection', async () => {
    const port = await freePort()
    setTimeout(() => { void serve([], port) }, 200)
    const response = await new HttpClient({ retry: true }).request('GET', `http://127.0.0.1:${port}/`)
    expect(response.status).toBe(200)
  })

  it('a refused connection is a NetworkError without retry', async () => {
    const port = await freePort()
    await expect(new HttpClient().request('GET', `http://127.0.0.1:${port}/`)).rejects.toBeInstanceOf(NetworkError)
  })
})

// Signing runs on each paced attempt, with recording observing the signed request.
it('prepares a fresh signed body after every pace and retry', async () => {
  const { url, starts } = await serve([[503, { 'Retry-After': '1' }]])
  const http = new HttpClient({ rate: 5, retry: true })
  const recording = http.recording()
  const stamps: number[] = []
  const response = await http.request('POST', url, {
    prepare: request => {
      stamps.push(performance.now())
      request.headers.set('X-Nonce', String(stamps.length))
      return new Request(request, { body: `nonce=${stamps.length}` })
    },
  })
  expect(response.status).toBe(200)
  expect(stamps).toHaveLength(2)
  expect(stamps[1]! - stamps[0]!).toBeGreaterThanOrEqual(1000)
  expect(starts.every((start, i) => start - stamps[i]! < 200)).toBe(true)
  expect(recording.exchanges.map(x => x.request.headers.get('X-Nonce'))).toEqual(['1', '2'])
  expect(await Promise.all(recording.exchanges.map(x => x.request.text()))).toEqual(['nonce=1', 'nonce=2'])
  recording.stop()
})

it.each([303, 307])('does not repeat a POST after a %s redirect connection failure', async status => {
  const port = await freePort()
  const { url, starts } = await serve([[status, { Location: `http://127.0.0.1:${port}/` }]])
  await expect(new HttpClient({ retry: true }).request('POST', url, { body: 'order' })).rejects.toBeInstanceOf(NetworkError)
  expect(starts).toHaveLength(1)
})

it('does not repeat a POST when a redirect target answers 503', async () => {
  const target = await serve([[503, { 'Retry-After': '0' }]])
  const origin = await serve([[303, { Location: target.url }]])
  expect((await new HttpClient({ retry: true }).request('POST', origin.url, { body: 'order' })).status).toBe(503)
  expect(origin.starts).toHaveLength(1)
})

it('cancelled sleeping and queued pacing waiters release their slots', async () => {
  const { url, starts } = await serve()
  const http = new HttpClient({ rate: 2 })
  await http.request('GET', url)
  const controllers = Array.from({ length: 4 }, () => new AbortController())
  const pending = controllers.map(controller => http.request('GET', url, { signal: controller.signal }))
  const settled = Promise.allSettled(pending)
  await new Promise(resolve => setTimeout(resolve, 50))
  for (const controller of controllers) controller.abort(new Error('cancelled'))
  expect((await settled).every(result => result.status === 'rejected')).toBe(true)
  const began = performance.now()
  await http.request('GET', url)
  expect(performance.now() - began).toBeLessThan(700)
  expect(starts).toHaveLength(2)
  expect(starts[1]! - starts[0]!).toBeGreaterThanOrEqual(450)
})

it('does not prepare queued requests until their pacing turn', async () => {
  const { url } = await serve()
  const http = new HttpClient({ rate: 5 })
  const stamps: number[] = []
  await Promise.all(Array.from({ length: 3 }, () => http.request('POST', url, {
    prepare: request => { stamps.push(performance.now()); return request },
  })))
  expect(stamps).toHaveLength(3)
  expect(stamps[1]! - stamps[0]!).toBeGreaterThanOrEqual(190)
  expect(stamps[2]! - stamps[1]!).toBeGreaterThanOrEqual(190)
})

it('aborts an asynchronous signer promptly and never sends its late result', async () => {
  let sends = 0
  const http = new HttpClient({ fetch: async () => { sends++; return new Response('ok') } })
  const controller = new AbortController()
  let prepared!: Request
  let finish!: (request: Request) => void
  const pending = http.request('POST', 'https://example.test', {
    signal: controller.signal,
    prepare: request => { prepared = request; return new Promise(resolve => { finish = resolve }) },
  })
  const reason = new Error('signing cancelled')
  controller.abort(reason)
  await expect(pending).rejects.toBe(reason)
  finish(prepared)
  await Promise.resolve()
  expect(sends).toBe(0)
})

it('applies the attempt timeout while awaiting an asynchronous signer', async () => {
  let sends = 0
  const http = new HttpClient({ timeout: 20, fetch: async () => { sends++; return new Response('ok') } })
  await expect(http.request('POST', 'https://example.test', {
    prepare: () => new Promise<Request>(() => {}),
  })).rejects.toBeInstanceOf(NetworkError)
  expect(sends).toBe(0)
})

it('does not retry or wrap a signing error', async () => {
  let prepares = 0
  let sends = 0
  const reason = new Error('signing failed')
  const http = new HttpClient({ retry: true, fetch: async () => { sends++; return new Response('ok') } })
  await expect(http.request('POST', 'https://example.test', {
    prepare: () => { prepares++; throw reason },
  })).rejects.toBe(reason)
  expect(prepares).toBe(1)
  expect(sends).toBe(0)
})

it.each(['timeout', 'caller'] as const)('a replacement request retains %s cancellation after send', async kind => {
  const server = createServer((request, response) => {
    request.resume()
    const timer = setTimeout(() => response.end('late success'), 350)
    response.on('close', () => clearTimeout(timer))
  })
  servers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`
  const controller = new AbortController()
  const reason = new Error('cancelled by caller')
  const client = new HttpClient(kind === 'timeout' ? { timeout: 40 } : {})
  const pending = client.request('POST', url, {
    signal: controller.signal,
    prepare: request => new Request(request.url, { method: request.method, body: 'signed payload' }),
  })
  const assertion = kind === 'timeout'
    ? expect(pending).rejects.toBeInstanceOf(NetworkError)
    : expect(pending).rejects.toBe(reason)
  const timer = kind === 'caller' ? setTimeout(() => controller.abort(reason), 40) : undefined
  try { await assertion } finally { clearTimeout(timer) }
})

it('a replacement request also retains its own cancellation signal', async () => {
  const prepared = new AbortController()
  const reason = new Error('signer cancelled')
  const http = new HttpClient({ timeout: 1000, fetch: async input => {
    const signal = (input as Request).signal
    prepared.abort(reason)
    signal.throwIfAborted()
    return new Response('unexpected')
  } })
  await expect(http.request('GET', 'https://example.test', {
    prepare: request => new Request(request.url, { signal: prepared.signal }),
  })).rejects.toMatchObject({ cause: reason })
})

it('asynchronous preparation keeps FIFO send spacing without waiting for responses', async () => {
  vi.useFakeTimers()
  const sent: { id: string | null; time: number }[] = []
  let finishFirst!: (response: Response) => void
  const http = new HttpClient({ rate: 2, fetch: async input => {
    const request = input as Request
    sent.push({ id: request.headers.get('X-Id'), time: performance.now() })
    if (sent.length === 1) return new Promise(resolve => { finishFirst = resolve })
    return new Response('ok')
  } })
  const pending = Promise.all([1, 2].map(id => http.request('POST', 'https://example.test', {
    prepare: async request => {
      if (id === 1) await new Promise(resolve => setTimeout(resolve, 700))
      request.headers.set('X-Id', String(id))
      return request
    },
  })))
  await vi.advanceTimersByTimeAsync(700)
  expect(sent.map(x => x.id)).toEqual(['1'])
  await vi.advanceTimersByTimeAsync(499)
  expect(sent).toHaveLength(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(sent.map(x => x.id)).toEqual(['1', '2'])
  expect(sent[1]!.time - sent[0]!.time).toBe(500)
  finishFirst(new Response('ok'))
  await pending
})

it.each(['abort', 'error'] as const)('releases an unused preparation turn on %s', async kind => {
  vi.useFakeTimers()
  const controller = new AbortController()
  const reason = new Error('preparation stopped')
  let fail!: (error: Error) => void
  let late!: (request: Request) => void
  let unsigned!: Request
  let sends = 0
  const http = new HttpClient({ rate: 1, fetch: async () => { sends++; return new Response('ok') } })
  const first = http.request('GET', 'https://example.test', {
    signal: controller.signal,
    prepare: request => {
      unsigned = request
      return new Promise<Request>((resolve, reject) => { late = resolve; fail = reject })
    },
  })
  const rejected = expect(first).rejects.toBe(reason)
  const second = http.request('GET', 'https://example.test')
  await vi.advanceTimersByTimeAsync(0)
  expect(sends).toBe(0)
  if (kind === 'abort') controller.abort(reason)
  else fail(reason)
  await rejected
  await vi.advanceTimersByTimeAsync(0)
  expect(sends).toBe(1)
  await second
  late(unsigned)
  await vi.advanceTimersByTimeAsync(1000)
  expect(sends).toBe(1)
})

it('a replacement request keeps the deadline measured before preparation', async () => {
  vi.useFakeTimers()
  vi.spyOn(AbortSignal, 'timeout').mockImplementation(ms => {
    const controller = new AbortController()
    setTimeout(() => controller.abort(new DOMException('deadline', 'TimeoutError')), ms)
    return controller.signal
  })
  let sends = 0
  const http = new HttpClient({ timeout: 100, fetch: async input => {
    sends++
    return new Promise<Response>((_resolve, reject) => {
      const signal = (input as Request).signal
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    })
  } })
  const pending = http.request('GET', 'https://example.test', {
    prepare: async request => {
      await new Promise(resolve => setTimeout(resolve, 70))
      return new Request(request.url)
    },
  })
  const rejected = expect(pending).rejects.toBeInstanceOf(NetworkError)
  await vi.advanceTimersByTimeAsync(70)
  expect(sends).toBe(1)
  await vi.advanceTimersByTimeAsync(30)
  await rejected
  expect(AbortSignal.timeout).toHaveBeenCalledTimes(1)
})
