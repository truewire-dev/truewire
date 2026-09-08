/**
 * HTTP transport over the global `fetch` (Node 22+, browsers, workers).
 *
 * One `HttpClient` per generated client. It does two things `fetch` alone does not:
 * every failure to reach the server becomes a `NetworkError`, and every exchange can be
 * observed at the wire level (the request and the response, before the client core unwraps
 * an envelope or maps an error), which is what `truewire capture` needs to record examples.
 */
import { NetworkError } from './errors.js'

/** One request and the response it got, exactly as they crossed the wire. */
export interface Exchange {
  request: Request
  /** A clone: reading its body does not consume the caller's. */
  response: Response
}

type HeadersInit = NonNullable<ConstructorParameters<typeof Headers>[0]>
type BodyInit = NonNullable<NonNullable<ConstructorParameters<typeof Request>[1]>['body']>

export type Query = Record<string, string | number | boolean | null | undefined> | URLSearchParams

export interface RequestOptions {
  /** Query parameters appended to `url`; `null`/`undefined` values are skipped. */
  query?: Query
  headers?: HeadersInit
  /** Raw body, sent as-is. */
  body?: BodyInit | null
  /** JSON body: serialized and sent as `application/json`. Mutually exclusive with `body`. */
  json?: unknown
  signal?: AbortSignal
  /** Milliseconds before the request is abandoned with a `NetworkError`; overrides the client default. */
  timeout?: number
}

export interface HttpClientOptions {
  /** Replacement for the global `fetch` (an agent wrapper, a test double). */
  fetch?: typeof fetch
  /** Default per-request timeout in milliseconds; none when omitted. */
  timeout?: number
  /** Permanent exchange hook; see `recording()` for a scoped one. */
  onExchange?: (exchange: Exchange) => void
}

/** A scoped recording: exchanges appended in order until `stop()`/disposal. */
export interface Recording extends Disposable {
  readonly exchanges: Exchange[]
  stop(): void
}

/**
 * Managed HTTP client over `fetch`.
 *
 * Many concurrent `request()` calls are fine; there is no connection to own, so nothing
 * to open or close.
 */
export class HttpClient {
  readonly fetch: typeof fetch
  readonly timeout: number | undefined
  private readonly hooks = new Set<(exchange: Exchange) => void>()

  constructor(options: HttpClientOptions = {}) {
    this.fetch = options.fetch ?? globalThis.fetch
    this.timeout = options.timeout
    if (options.onExchange) this.hooks.add(options.onExchange)
  }

  /**
   * Record every exchange this client makes until the recording is stopped, in order.
   *
   * Everything the client sent is in here, in order, not just the call's own request: a
   * token mint, a refresh, a retry. Pick the exchange you mean by its request, never by
   * position -- `at(-1)` may be some other endpoint's, and its body may be a credential.
   *
   * ```ts
   * using rec = client.recording()
   * const pet = await client.pets.getPet({ petId: 42 })
   * const mine = rec.exchanges.filter((x) => new URL(x.request.url).pathname.endsWith('/pets/42'))
   * const { status } = mine.at(-1)!.response
   * ```
   */
  recording(): Recording {
    const exchanges: Exchange[] = []
    const hook = (x: Exchange) => { exchanges.push(x) }
    this.hooks.add(hook)
    const stop = () => { this.hooks.delete(hook) }
    return { exchanges, stop, [Symbol.dispose]: stop }
  }

  /** Send one request; the reply, whatever its status. Failing to get one is a `NetworkError`. */
  async request(method: string, url: string | URL, options: RequestOptions = {}): Promise<Response> {
    const target = new URL(url)
    if (options.query) {
      const params = options.query instanceof URLSearchParams ? options.query : Object.entries(options.query)
      for (const [key, value] of params) {
        if (value !== null && value !== undefined) target.searchParams.append(key, String(value))
      }
    }
    const headers = new Headers(options.headers)
    let body = options.body ?? null
    if (options.json !== undefined) {
      body = JSON.stringify(options.json)
      if (!headers.has('content-type')) headers.set('content-type', 'application/json')
    }
    const timeout = options.timeout ?? this.timeout
    const signals = [options.signal, timeout === undefined ? undefined : AbortSignal.timeout(timeout)].filter(s => s !== undefined)
    const signal = signals.length ? AbortSignal.any(signals) : undefined
    const request = new Request(target, { method: method.toUpperCase(), headers, body, signal })
    let response: Response
    try {
      response = await this.fetch(this.hooks.size ? request.clone() : request)
    } catch (e) {
      if (options.signal?.aborted) throw e
      throw new NetworkError(`Error sending request to ${method.toUpperCase()} ${target}`, { cause: e })
    }
    if (this.hooks.size) {
      const exchange: Exchange = { request, response: response.clone() }
      for (const hook of this.hooks) hook(exchange)
    }
    return response
  }
}
