/**
 * Hand-written core for the github client: transport, auth and errors.
 *
 * Every generated class takes a `Core` (anything satisfying `HttpEndpoint<DefaultMeta>`
 * from `@truewire/core`) and calls `request` on it; this is the one place that knows how
 * to reach the upstream API. Adapt it (base URL, headers, signing, envelope unwrapping,
 * error mapping) to your API; the generated code never changes when you do.
 */
import {
  ApiError, AuthError, BadRequest, HttpClient, RateLimited, parseJson,
  type HttpCall, type HttpEndpoint, type Query,
} from '@truewire/core'
import type { DefaultMeta } from '../meta.js'

export interface CoreOptions {
  /** Defaults to `https://api.github.com`; point it at `truewire mock` in tests. */
  baseUrl?: string
  /** A token, sent as `Authorization: Bearer` (public endpoints accept it too, and it raises the rate limit). */
  token?: string
  /** Validate responses by default; a call's own `validate` option overrides it. */
  validate?: boolean
  /** The `fetch` wrapper to send through; one is made when omitted. */
  http?: HttpClient
}

/** The shared HTTP transport: base URL, GitHub's headers, the optional token and error mapping. */
export class Core implements HttpEndpoint<DefaultMeta> {
  readonly baseUrl: string
  readonly token: string | undefined
  readonly validate: boolean
  readonly http: HttpClient

  constructor(options: CoreOptions = {}) {
    this.baseUrl = (options.baseUrl ?? 'https://api.github.com').replace(/\/+$/, '')
    this.token = options.token
    this.validate = options.validate ?? true
    this.http = options.http ?? new HttpClient()
  }

  /** Headers for one call: GitHub's media type, API version and a User-Agent, plus the token when one was given. */
  headers(_meta: DefaultMeta): Record<string, string> {
    const headers: Record<string, string> = {
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'truewire-example-github',
    }
    if (this.token !== undefined) headers.Authorization = `Bearer ${this.token}`
    return headers
  }

  /** Send one request; the decoded reply, validated unless `validate` is off. */
  async request<Req, Res>(call: HttpCall<Req, Res, DefaultMeta>): Promise<Res> {
    const wire: Record<string, unknown> =
      call.request !== undefined && call.requestCodec !== undefined
        ? (call.requestCodec.dump(call.request) as Record<string, unknown>)
        : {}
    let path = call.path
    const params: Record<string, unknown> = {}
    for (const [name, value] of Object.entries(wire)) {
      if (value === undefined) continue
      const placeholder = `{${name}}`
      if (path.includes(placeholder)) path = path.split(placeholder).join(encodeURIComponent(String(value)))
      else params[name] = value
    }
    const method = (call.method ?? 'GET').toUpperCase()
    const withBody = method === 'POST' || method === 'PUT' || method === 'PATCH'
    const response = await this.http.request(method, `${this.baseUrl}/${path.replace(/^\/+/, '')}`, {
      query: withBody ? undefined : query(params),
      json: withBody ? params : undefined,
      headers: this.headers(call.meta),
      signal: call.signal,
    })
    const text = await response.text()
    if (response.status >= 400) throw mapError(method, path, response, text)
    if (call.responseCodec === undefined) return undefined as Res
    if (call.validate ?? this.validate) return parseJson(call.responseCodec, text)
    return JSON.parse(text) as Res
  }
}

/** Query parameters from dumped request values; a nested value is sent as JSON text. */
function query(params: Record<string, unknown>): Query {
  const out: Record<string, string | number | boolean> = {}
  for (const [name, value] of Object.entries(params)) {
    if (value === null || value === undefined) continue
    out[name] = typeof value === 'object' ? JSON.stringify(value) : (value as string | number | boolean)
  }
  return out
}

/** A non-2xx reply as the `ApiError` subclass its status calls for. */
function mapError(method: string, path: string, response: Response, text: string): ApiError {
  let body: unknown = text
  try { body = JSON.parse(text) } catch { /* not JSON: keep the text */ }
  const detail = typeof body === 'object' && body !== null && 'message' in body ? String((body as { message: unknown }).message) : text.slice(0, 200)
  const message = `${method} ${path}: HTTP ${response.status}: ${detail}`
  const options = { status: response.status, body }
  if (response.status === 429 || (response.status === 403 && response.headers.get('x-ratelimit-remaining') === '0')) {
    return new RateLimited(message, options)
  }
  if (response.status === 401 || response.status === 403) return new AuthError(message, options)
  if (response.status === 400 || response.status === 404 || response.status === 422) return new BadRequest(message, options)
  return new ApiError(message, options)
}
