/**
 * HTTP transport over the global `fetch` (Node 22+, browsers, workers).
 *
 * One `HttpClient` per generated client. It does two things `fetch` alone does not:
 * every failure to reach the server becomes a `NetworkError`, and every exchange can be
 * observed at the wire level (the request and the response, before the client core unwraps
 * an envelope or maps an error), which is what `truewire capture` needs to record examples.
 */
import { stringifyJson } from './json.js'
import { LogicError, NetworkError } from './errors.js'
import { checkProxy, redactedProxy, proxiedFetch } from './proxy.js'

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
  /**
   * Prepare/sign each attempt after pacing. Return the request to send; never reuse a consumed body.
   * Caller cancellation and the attempt timeout also cover asynchronous preparation and
   * any replacement request. A paced client keeps its send turn until preparation finishes.
   */
  prepare?: (request: Request) => Request | Promise<Request>
  /** Query parameters appended before signing; `null`/`undefined` values are skipped. */
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
  /**
   * HTTP(S) proxy URL every request goes through (`http://host:3128`, credentials in the
   * userinfo): a `CONNECT` tunnel for `https://`, an absolute-form request for `http://`.
   * Node only, and it needs the optional peer dependency `undici`; in a browser it throws,
   * since the browser sends through its own proxy settings. Omitted or `''`, requests go
   * through the global `fetch` as before, which on Node ignores `HTTPS_PROXY` by default.
   * Not together with `fetch`. Packages clause P18: a sandbox or a library caller cannot
   * always set the process environment.
   */
  proxy?: string
  /** Default per-request timeout in milliseconds; none when omitted. */
  timeout?: number
  /** Permanent exchange hook; see `recording()` for a scoped one. */
  onExchange?: (exchange: Exchange) => void
  /** Requests per second to pace to (`[policy].rate`); no pacing when omitted. */
  rate?: number | undefined
  /** Retry a request that did not reach the server or got a 429 or 503 (`[policy].retry`); off when omitted. */
  retry?: boolean | undefined
}

/** Attempts a request gets with `retry: true`, the first included. */
export const RETRY_ATTEMPTS = 3
/** Statuses retried with `retry: true`: the server said it did not handle the request. */
export const RETRY_STATUSES: ReadonlySet<number> = new Set([429, 503])
/** Longest `Retry-After`, in milliseconds, a retry waits; a longer one returns the reply as is. */
export const RETRY_AFTER_CAP = 30_000
/** Milliseconds before the first retry when the server names no `Retry-After`; doubled after. */
export const RETRY_BACKOFF = 500

/**
 * Error codes Node's `fetch` gives the `cause` of a failure to connect: nothing was sent,
 * so sending again cannot repeat an action. A browser's `fetch` says no more than
 * `TypeError`, so there no connection failure is retried.
 */
const CONNECT_ERRORS = new Set([
  'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH', 'UND_ERR_CONNECT_TIMEOUT',
])

function failedToConnect(error: unknown): boolean {
  const cause = (error as { cause?: { code?: unknown } } | null)?.cause
  return typeof cause?.code === 'string' && CONNECT_ERRORS.has(cause.code)
}

/**
 * The reply's `Retry-After` in milliseconds from now, or `undefined` when absent or
 * unreadable: delay-seconds (`120`) or an HTTP date, never negative.
 */
export function retryAfter(response: Response): number | undefined {
  const value = response.headers.get('retry-after')?.trim()
  if (value === undefined || value === '') return undefined
  if (/^\d+$/.test(value)) return Number(value) * 1000
  const when = Date.parse(value)
  return Number.isNaN(when) ? undefined : Math.max(0, when - Date.now())
}

/** Resolve after `ms`, or reject with the signal's reason once it aborts. */
function sleep(ms: number, signal: AbortSignal | undefined): Promise<void> {
  if (ms <= 0 && !signal?.aborted) return Promise.resolve()
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason)
    const done = () => { signal?.removeEventListener('abort', abort); resolve() }
    const timer = setTimeout(done, ms)
    const abort = () => { clearTimeout(timer); reject(signal!.reason) }
    signal?.addEventListener('abort', abort, { once: true })
  })
}

/** Wait for work or cancellation, removing the listener whichever settles first. */
function waitWithSignal<T>(work: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (!signal) return work
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason)
    // Always observe the work, even if already aborted: a late signer rejection
    // must not become an unhandled rejection after the caller has left.
    work.then(
      value => { signal.removeEventListener('abort', abort); resolve(value) },
      error => { signal.removeEventListener('abort', abort); reject(error) },
    )
    if (signal.aborted) abort()
    else signal.addEventListener('abort', abort, { once: true })
  })
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
 *
 * `rate` is the requests per second the client paces itself to (`[policy].rate`,
 * workspace clause W15): request starts are spaced `1000 / rate` ms apart, shared by every
 * caller of this client, so ten requests at `rate: 5` span 1.8 s. Omitted, each goes at once.
 *
 * `retry: true` (`[policy].retry`) sends a request again, up to `RETRY_ATTEMPTS` in all,
 * when it did not reach the server (a failure to connect, before anything was sent) or the
 * reply is 429 or 503. A retry waits the reply's `Retry-After` (seconds or an HTTP date),
 * else `RETRY_BACKOFF` doubling; a `Retry-After` over `RETRY_AFTER_CAP` returns the reply
 * instead. Nothing else is retried: not another status, not a connection lost after the
 * request was sent, and not a `ReadableStream` body, which cannot be sent twice. Each
 * attempt is paced and recorded like any request. Connection failures with automatic
 * redirects are retried only for safe methods; a POST may already have executed.
 * Off, each request is sent once.
 */
export class HttpClient {
  readonly fetch: typeof fetch
  /** The proxy every request goes through, with credentials removed, when one was given. */
  readonly proxy: string | undefined
  readonly timeout: number | undefined
  readonly rate: number | undefined
  readonly retry: boolean
  private readonly hooks = new Set<(exchange: Exchange) => void>()
  private nextStart = 0
  private paceTail = Promise.resolve()

  constructor(options: HttpClientOptions = {}) {
    // Bound to the global: a browser's `fetch` requires its own receiver, and calling it
    // as a method of this object throws `Illegal invocation`. Node's is tolerant, so an
    // unbound reference passes every test that does not run in a browser.
    const proxy = options.proxy || undefined
    if (proxy !== undefined) {
      checkProxy('HttpClient', proxy)
      if (options.fetch) throw new LogicError('HttpClient: pass `fetch` or `proxy`, not both')
    }
    this.proxy = proxy === undefined ? undefined : redactedProxy(proxy)
    this.fetch = proxy !== undefined ? proxiedFetch(proxy) : options.fetch ?? globalThis.fetch.bind(globalThis)
    this.timeout = options.timeout
    if (options.rate !== undefined && !(Number.isFinite(options.rate) && options.rate > 0)) {
      throw new RangeError(`rate must be a positive number of requests per second; got ${options.rate}`)
    }
    this.rate = options.rate
    this.retry = options.retry ?? false
    if (options.onExchange) this.hooks.add(options.onExchange)
  }

  /** Acquire a send turn; the caller releases it after starting fetch or abandoning the attempt. */
  private async pace(signal: AbortSignal | undefined): Promise<() => void> {
    const previous = this.paceTail
    let release!: () => void
    const turn = new Promise<void>(resolve => { release = resolve })
    // An aborted queued waiter releases its own turn, but still preserves the
    // predecessor barrier so later callers cannot jump past the active sleeper.
    this.paceTail = previous.then(() => turn)
    try {
      await waitWithSignal(previous, signal)
      signal?.throwIfAborted()
      for (let wait = this.nextStart - performance.now(); wait > 0; wait = this.nextStart - performance.now()) {
        await sleep(Math.ceil(wait), signal)
      }
      signal?.throwIfAborted()
      return release
    } catch (error) {
      release()
      throw error
    }
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
      body = stringifyJson(options.json)
      if (!headers.has('content-type')) headers.set('content-type', 'application/json')
    }
    const timeout = options.timeout ?? this.timeout
    const retry = this.retry && !(body instanceof ReadableStream)
    for (let attempt = 1; ; attempt++) {
      const last = !retry || attempt === RETRY_ATTEMPTS
      const backoff = RETRY_BACKOFF * 2 ** (attempt - 1)
      const release = this.rate === undefined ? undefined : await this.pace(options.signal)
      try {
        // The attempt timeout starts after pacing and includes preparation and fetch.
        const signals = [options.signal, timeout === undefined ? undefined : AbortSignal.timeout(timeout)].filter(s => s !== undefined)
        const signal = signals.length ? AbortSignal.any(signals) : undefined
        const unsigned = new Request(target, { method: method.toUpperCase(), headers, body, signal })
        let request = unsigned
        try {
          signal?.throwIfAborted()
          if (options.prepare) request = await waitWithSignal(Promise.resolve(options.prepare(unsigned)), signal)
          signal?.throwIfAborted()
          // A signer may return an independently constructed Request. Keep both its
          // signal and the original attempt deadline/caller cancellation alive.
          if (signal && request !== unsigned) {
            request = new Request(request, { signal: AbortSignal.any([signal, request.signal]) })
          }
          request.signal.throwIfAborted()
        } catch (e) {
          if (signal?.aborted && !options.signal?.aborted) {
            throw new NetworkError(`Error preparing request to ${method.toUpperCase()} ${target}`, { cause: e })
          }
          throw e
        }
        let response: Response
        try {
          let pending: Promise<Response>
          try {
            const outgoing = this.hooks.size ? request.clone() : request
            if (this.rate !== undefined) this.nextStart = performance.now() + 1000 / this.rate
            pending = this.fetch(outgoing)
          } finally {
            // The next caller may prepare while this response is still pending.
            release?.()
          }
          response = await pending
        } catch (e) {
          if (options.signal?.aborted || e instanceof LogicError) throw e
          // Fetch hides redirect history on errors. A POST may already have executed
          // before the redirect target refused the connection; only safe methods can
          // retry that ambiguous failure (or a request explicitly disabling redirects).
          const safeConnectRetry = request.redirect !== 'follow' || /^(GET|HEAD|OPTIONS|TRACE)$/.test(request.method)
          if (!last && safeConnectRetry && failedToConnect(e)) {
            await sleep(backoff, options.signal)
            continue
          }
          throw new NetworkError(`Error sending request to ${method.toUpperCase()} ${target}`, { cause: e })
        }
        if (this.hooks.size) {
          const exchange: Exchange = { request, response: response.clone() }
          for (const hook of this.hooks) hook(exchange)
        }
        if (last || response.redirected || !RETRY_STATUSES.has(response.status)) return response
        const delay = retryAfter(response)
        if (delay !== undefined && delay > RETRY_AFTER_CAP) return response
        // Not awaited: with a recording's clone unread, the tee settles the cancel only
        // once both branches are cancelled.
        response.body?.cancel().catch(() => {})
        await sleep(delay ?? backoff, options.signal)
      } finally {
        // Construction/preparation errors and cancellation consume no send slot.
        release?.()
      }
    }
  }
}
