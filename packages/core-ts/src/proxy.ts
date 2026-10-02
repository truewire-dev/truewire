/**
 * An explicit proxy for `HttpClient` and `Socket` (packages clause P18): undici's own
 * `fetch` and `WebSocket` over a `ProxyAgent`, on Node only.
 *
 * `undici` is an optional peer dependency, imported the first time a proxied client
 * sends anything; a client without `proxy` never loads it, and a browser bundle never
 * sees it. undici's own `fetch` and `WebSocket` are used, not the globals with an undici
 * dispatcher: the globals are Node's bundled undici, and a dispatcher from another major
 * version fails inside them (undici 8 on Node 24: `invalid onRequestStart method`).
 */
import { LogicError } from './errors.js'
import type { WebSocketLike } from './ws/socket.js'

interface Undici {
  ProxyAgent: new (options: { uri: string; proxyTunnel?: boolean }) => object
  // Older Node typings omit cache from RequestInit, though Request exposes it.
  fetch: (input: string, init: Omit<RequestInit, 'dispatcher'> & { cache?: Request['cache']; dispatcher: object }) => Promise<Response>
  WebSocket: new (url: string, init: { dispatcher: object }) => WebSocketLike
}

/**
 * Refuse a `proxy` that cannot apply here, before anything is sent: a browser (which
 * sends through its own proxy settings), or a URL that is not `http://` or `https://`.
 * The message never repeats the URL, which may carry credentials.
 */
export function checkProxy(owner: string, proxy: string): void {
  const node = (globalThis as { process?: { versions?: { node?: string } } }).process?.versions?.node
  if (node === undefined) {
    throw new LogicError(`${owner}: \`proxy\` works on Node only; a browser sends through its own proxy settings`)
  }
  let url: URL
  try { url = new URL(proxy) } catch {
    // Node's invalid-URL cause includes the complete input, including credentials.
    throw new LogicError(`${owner}: \`proxy\` is not a URL`)
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new LogicError(`${owner}: \`proxy\` must be an http:// or https:// URL`)
  }
}

/** Safe to inspect: keep credentials only in the transport closure. */
export function redactedProxy(proxy: string): string {
  const url = new URL(proxy)
  if (!url.username && !url.password) return proxy
  url.username = ''
  url.password = ''
  return url.href
}

let loading: Promise<Undici> | undefined

/** undici, imported once; its absence is a `LogicError` that says how to install it. */
function undici(): Promise<Undici> {
  // A variable, not a literal: a bundler targeting the browser must not try to resolve it.
  const specifier = 'undici'
  return loading ??= import(/* webpackIgnore: true */ /* @vite-ignore */ specifier).then(
    (module: Undici & { default?: Undici }) => (module.ProxyAgent ? module : module.default!),
    (e: unknown) => {
      loading = undefined
      throw new LogicError('`proxy` needs the optional peer dependency `undici` (npm install undici)', { cause: e })
    },
  )
}

/** Buffer across Request implementations without waiting on an aborted producer. */
async function requestBody(request: Request): Promise<ArrayBuffer | null> {
  const { body, signal } = request
  if (signal.aborted) {
    // Request construction already transferred the body. Cancel it even before
    // the first read, without waiting for or propagating a producer hook failure.
    if (body !== null) void body.cancel(signal.reason).catch(() => {})
    throw signal.reason
  }
  if (body === null) return null
  const reader = body.getReader()
  let onAbort!: () => void
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => {
      reject(signal.reason)
      // Cancellation closes pending reads, but a producer's cancel hook may itself
      // stall or reject. Neither may delay abort or replace the caller's reason.
      void reader.cancel(signal.reason).catch(() => {})
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
  try {
    const chunks: Uint8Array[] = []
    let length = 0
    while (true) {
      const { done, value } = await Promise.race([reader.read(), aborted])
      if (done) break
      if (!(value instanceof Uint8Array)) throw new TypeError('Received non-Uint8Array chunk')
      chunks.push(value)
      length += value.byteLength
    }
    const result = new Uint8Array(length)
    let offset = 0
    for (const chunk of chunks) {
      result.set(chunk, offset)
      offset += chunk.byteLength
    }
    return result.buffer
  } finally {
    signal.removeEventListener('abort', onAbort)
    reader.releaseLock()
  }
}

/**
 * A `fetch` that sends through `proxy`: a `CONNECT` tunnel for `https://`, an absolute-form
 * request line for `http://`, as httpx and reqwest do. One agent, so connections to the
 * proxy are reused.
 */
export function proxiedFetch(proxy: string): typeof fetch {
  let agent: object | undefined
  return async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const { ProxyAgent, fetch } = await undici()
    agent ??= new ProxyAgent({ uri: proxy, proxyTunnel: false })
    if (!(input instanceof Request)) return fetch(String(input), { ...init, dispatcher: agent })
    // undici's `fetch` knows only its own `Request` class and would read a global one as
    // the string "[object Request]", so it gets the request's parts instead. Apply init
    // through Request first: undefined options inherit, and a replacement body must not
    // consume the original. Copy every exposed RequestInit setting, including integrity.
    const request = new Request(input, init)
    return fetch(request.url, {
      ...init,
      method: request.method,
      headers: request.headers,
      body: await requestBody(request),
      signal: request.signal,
      redirect: request.redirect,
      cache: request.cache,
      credentials: request.credentials,
      integrity: request.integrity,
      keepalive: request.keepalive,
      mode: request.mode,
      referrer: request.referrer,
      referrerPolicy: request.referrerPolicy,
      dispatcher: agent,
    })
  }
}

/**
 * The factory a `Socket` opens connections with when given `proxy`, resolved once undici
 * is loaded. Every connection is a `CONNECT` tunnel, `ws://` included (undici 8 would
 * otherwise send a plain `ws://` upgrade as an absolute-form request, which proxies
 * rarely pass on).
 */
export function proxiedWebSocket(proxy: string): () => Promise<(url: string) => WebSocketLike> {
  let agent: object | undefined
  return async () => {
    const { ProxyAgent, WebSocket } = await undici()
    const dispatcher = agent ??= new ProxyAgent({ uri: proxy, proxyTunnel: true })
    return url => new WebSocket(url, { dispatcher })
  }
}
