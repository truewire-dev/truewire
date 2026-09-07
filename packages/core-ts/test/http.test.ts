/**
 * Pins `HttpClient`: requests are built from `query`/`json`/`headers`, a failure to reach
 * the server is a `NetworkError`, and every exchange can be recorded at the wire level with
 * the response body still readable by the caller.
 */
import { describe, expect, it } from 'vitest'
import { NetworkError } from '../src/errors.js'
import { HttpClient, type Exchange } from '../src/http.js'

/** A `fetch` double: records every request and answers with `reply`. */
function fakeFetch(reply: (request: Request) => Response | Promise<Response>) {
  const requests: Request[] = []
  const fetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(input, init)
    requests.push(request)
    return reply(request)
  }
  return { fetch: fetch as typeof globalThis.fetch, requests }
}

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

describe('HttpClient.request', () => {
  it('appends query parameters, skipping null and undefined, and uppercases the method', async () => {
    const { fetch, requests } = fakeFetch(() => ok({}))
    const client = new HttpClient({ fetch })
    const response = await client.request('get', 'https://api.example.invalid/orders?page=1', {
      query: { symbol: 'BTC/USD', limit: 10, open: true, cursor: undefined, since: null },
    })
    expect(response.status).toBe(200)
    expect(requests[0]!.method).toBe('GET')
    expect(requests[0]!.url).toBe('https://api.example.invalid/orders?page=1&symbol=BTC%2FUSD&limit=10&open=true')
  })

  it('accepts URLSearchParams and a URL', async () => {
    const { fetch, requests } = fakeFetch(() => ok({}))
    await new HttpClient({ fetch }).request('GET', new URL('https://api.example.invalid/x'), { query: new URLSearchParams({ a: '1' }) })
    expect(requests[0]!.url).toBe('https://api.example.invalid/x?a=1')
  })

  it('sends json as an application/json body, keeping an explicit content-type', async () => {
    const { fetch, requests } = fakeFetch(() => ok({}))
    const client = new HttpClient({ fetch })
    await client.request('POST', 'https://api.example.invalid/orders', { json: { symbol: 'BTC/USD', qty: '1.5' } })
    expect(requests[0]!.headers.get('content-type')).toBe('application/json')
    expect(await requests[0]!.json()).toEqual({ symbol: 'BTC/USD', qty: '1.5' })
    await client.request('POST', 'https://api.example.invalid/orders', { json: {}, headers: { 'content-type': 'application/vnd.api+json' } })
    expect(requests[1]!.headers.get('content-type')).toBe('application/vnd.api+json')
  })

  it('sends a raw body and headers as given', async () => {
    const { fetch, requests } = fakeFetch(() => ok({}))
    await new HttpClient({ fetch }).request('PUT', 'https://api.example.invalid/x', { body: 'a=1', headers: [['x-api-key', 'k']] })
    expect(await requests[0]!.text()).toBe('a=1')
    expect(requests[0]!.headers.get('x-api-key')).toBe('k')
  })

  it('returns the reply whatever its status', async () => {
    const { fetch } = fakeFetch(() => new Response('nope', { status: 429 }))
    const response = await new HttpClient({ fetch }).request('GET', 'https://api.example.invalid/x')
    expect(response.status).toBe(429)
    expect(await response.text()).toBe('nope')
  })

  it('a fetch failure is a NetworkError naming the request, with the cause', async () => {
    const cause = new TypeError('fetch failed')
    const { fetch } = fakeFetch(() => { throw cause })
    const err = await new HttpClient({ fetch }).request('get', 'https://api.example.invalid/x').catch(e => e)
    expect(err).toBeInstanceOf(NetworkError)
    expect(err.message).toBe('Error sending request to GET https://api.example.invalid/x')
    expect(err.cause).toBe(cause)
  })

  it('a timeout is a NetworkError', async () => {
    const { fetch } = fakeFetch(request => new Promise((_, reject) => {
      request.signal.addEventListener('abort', () => reject(request.signal.reason))
    }))
    const client = new HttpClient({ fetch, timeout: 10 })
    await expect(client.request('GET', 'https://api.example.invalid/slow')).rejects.toBeInstanceOf(NetworkError)
    await expect(client.request('GET', 'https://api.example.invalid/slow', { timeout: 5 })).rejects.toThrow(/Error sending request/)
  })

  it("the caller's own abort is rethrown as-is", async () => {
    const { fetch } = fakeFetch(request => new Promise((_, reject) => {
      request.signal.addEventListener('abort', () => reject(request.signal.reason))
    }))
    const controller = new AbortController()
    const pending = new HttpClient({ fetch }).request('GET', 'https://api.example.invalid/x', { signal: controller.signal })
    controller.abort(new Error('user cancelled'))
    await expect(pending).rejects.toThrow('user cancelled')
  })
})

describe('recording', () => {
  it('records every exchange in order, at the wire level, without consuming the body', async () => {
    let n = 0
    const { fetch } = fakeFetch(() => ok({ n: ++n }))
    const client = new HttpClient({ fetch })
    const rec = client.recording()
    const first = await client.request('GET', 'https://api.example.invalid/a')
    await client.request('POST', 'https://api.example.invalid/b', { json: { x: 1 } })
    rec.stop()
    await client.request('GET', 'https://api.example.invalid/c')

    expect(rec.exchanges.map(x => x.request.url)).toEqual(['https://api.example.invalid/a', 'https://api.example.invalid/b'])
    expect(await first.json()).toEqual({ n: 1 })
    expect(await rec.exchanges[0]!.response.json()).toEqual({ n: 1 })
    expect(await rec.exchanges[1]!.request.json()).toEqual({ x: 1 })
    expect(rec.exchanges[1]!.response.status).toBe(200)
  })

  it('is a Disposable', async () => {
    const { fetch } = fakeFetch(() => ok({}))
    const client = new HttpClient({ fetch })
    let exchanges: Exchange[]
    {
      using rec = client.recording()
      exchanges = rec.exchanges
      await client.request('GET', 'https://api.example.invalid/a')
    }
    await client.request('GET', 'https://api.example.invalid/b')
    expect(exchanges).toHaveLength(1)
  })

  it('onExchange is a permanent hook, and recordings may overlap', async () => {
    const { fetch } = fakeFetch(() => ok({}))
    const seen: string[] = []
    const client = new HttpClient({ fetch, onExchange: x => { seen.push(x.request.url) } })
    const a = client.recording()
    await client.request('GET', 'https://api.example.invalid/1')
    const b = client.recording()
    await client.request('GET', 'https://api.example.invalid/2')
    a.stop()
    await client.request('GET', 'https://api.example.invalid/3')
    b.stop()
    expect(seen).toHaveLength(3)
    expect(a.exchanges).toHaveLength(2)
    expect(b.exchanges).toHaveLength(2)
  })

  it('records nothing when a fetch fails', async () => {
    const { fetch } = fakeFetch(() => { throw new TypeError('fetch failed') })
    const client = new HttpClient({ fetch })
    const rec = client.recording()
    await expect(client.request('GET', 'https://api.example.invalid/x')).rejects.toBeInstanceOf(NetworkError)
    expect(rec.exchanges).toHaveLength(0)
  })
})
