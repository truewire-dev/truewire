/**
 * The Spot REST transport: unsigned GETs for public endpoints, signed POSTs for private
 * ones (form-urlencoded, or JSON for the two paths Kraken rejects a form body on), the
 * `{error, result}` envelope unwrapped and the reply validated.
 */
import { AuthError, HttpClient, t, type HttpCall, type HttpEndpoint, type Query } from '@truewire/core'
import type { SpotMeta } from '../meta.js'
import { Nonce, sign, type Credentials, type WsToken } from './auth.js'
import { unwrap } from './envelope.js'

export const SPOT_API_URL = 'https://api.kraken.com'

/** Private endpoints Kraken rejects a form-urlencoded body on; they are sent as JSON. */
export const JSON_BODY_PATHS = new Set(['/0/private/AddOrderBatch', '/0/private/CancelOrderBatch'])

const WS_TOKEN_PATH = '/0/private/GetWebSocketsToken'
const WsToken = t.object({ token: t.string, expires: t.integer })

export interface SpotOptions {
  /** Defaults to `https://api.kraken.com`; point it at `truewire mock` in tests. */
  baseUrl?: string
  /** The key pair private endpoints are signed with; without one only public endpoints can be called. */
  credentials?: Credentials
  /** Validate responses by default; a call's own `validate` option overrides it. */
  validate?: boolean
  /** The `fetch` wrapper to send through; one is made when omitted. */
  http?: HttpClient
}

/** The transport every `spot.*` endpoint calls; `HttpEndpoint<SpotMeta>` by shape. */
export class SpotCore implements HttpEndpoint<SpotMeta> {
  readonly baseUrl: string
  readonly credentials: Credentials | undefined
  readonly validate: boolean
  readonly http: HttpClient
  readonly nonce = new Nonce()

  constructor(options: SpotOptions = {}) {
    this.baseUrl = (options.baseUrl ?? SPOT_API_URL).replace(/\/+$/, '')
    this.credentials = options.credentials
    this.validate = options.validate ?? true
    this.http = options.http ?? new HttpClient()
  }

  /** Send one call; the envelope's `result`, validated unless `validate` is off. */
  async request<Req, Res>(call: HttpCall<Req, Res, SpotMeta>): Promise<Res> {
    const values: Record<string, unknown> =
      call.request !== undefined && call.requestCodec !== undefined
        ? (call.requestCodec.dump(call.request) as Record<string, unknown>)
        : {}
    const result = call.meta.signed
      ? await this.signed(call.path, values, call.signal)
      : await this.public(call.path, values, call.signal)
    if (call.responseCodec === undefined) return undefined as Res
    return (call.validate ?? this.validate) ? call.responseCodec.parse(result) : (result as Res)
  }

  /** The WebSocket token the private socket sends with every request. */
  async getWsToken(): Promise<WsToken> {
    return WsToken.parse(await this.signed(WS_TOKEN_PATH, {}))
  }

  private async public(path: string, values: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> {
    const response = await this.http.request('GET', this.baseUrl + path, { query: query(values), signal })
    return unwrap(response.status, await response.text())
  }

  /**
   * Sign and send a private POST. The body is encoded once, and that string is both what
   * the signature covers and what is sent.
   */
  private async signed(path: string, values: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> {
    if (this.credentials === undefined) throw new AuthError('No credentials: this client can only call public endpoints.')
    const nonce = this.nonce.next()
    const json = JSON_BODY_PATHS.has(path)
    const body = json
      ? JSON.stringify({ nonce, ...values })
      : new URLSearchParams(query({ nonce, ...values }) as Record<string, string>).toString()
    const response = await this.http.request('POST', this.baseUrl + path, {
      body,
      headers: {
        'API-Key': this.credentials.apiKey,
        'API-Sign': sign(path, nonce, body, this.credentials.privateKey),
        'Content-Type': json ? 'application/json' : 'application/x-www-form-urlencoded',
      },
      signal,
    })
    return unwrap(response.status, await response.text())
  }
}

/** Query or form parameters from dumped request values; a nested value is sent as JSON text. */
function query(values: Record<string, unknown>): Query {
  const out: Record<string, string> = {}
  for (const [name, value] of Object.entries(values)) {
    if (value === null || value === undefined) continue
    out[name] = typeof value === 'object' ? JSON.stringify(value) : String(value)
  }
  return out
}
