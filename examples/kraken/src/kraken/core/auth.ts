/**
 * Kraken Spot's REST authentication -- `API-Key`/`API-Sign` headers over a strictly
 * increasing nonce -- and the WebSocket token the private connection sends instead.
 */
import { createHash, createHmac } from 'node:crypto'

/** A Kraken API key pair. The private key is base64 and never leaves the process. */
export interface Credentials {
  apiKey: string
  privateKey: string
}

/**
 * The `API-Sign` header value: HMAC-SHA512, keyed by the decoded private key, over the
 * request path followed by `SHA256(nonce + body)`, base64-encoded.
 *
 * `encodedBody` is the exact body being sent, `nonce` included: the signature covers the
 * bytes on the wire, not a reconstruction of them.
 *
 * @see https://docs.kraken.com/api/docs/guides/spot-rest-auth
 */
export function sign(path: string, nonce: number, encodedBody: string, privateKey: string): string {
  const digest = createHash('sha256').update(`${nonce}${encodedBody}`).digest()
  return createHmac('sha512', Buffer.from(privateKey, 'base64'))
    .update(Buffer.concat([Buffer.from(path), digest]))
    .digest('base64')
}

/**
 * A strictly increasing nonce, as Kraken requires per key: the millisecond clock, bumped
 * by one whenever two calls land in the same millisecond.
 */
export class Nonce {
  #last = 0

  next(): number {
    const now = Date.now()
    this.#last = now > this.#last ? now : this.#last + 1
    return this.#last
  }
}

/** A `GetWebSocketsToken` result: the token, and how many seconds it stays valid. */
export interface WsToken {
  token: string
  expires: number
}

/** Refresh the WebSocket token this long before its ~900 s lifetime ends. */
const REFRESH_BUFFER_MS = 30_000

/**
 * The WebSocket token, fetched through the signed REST call on first use and refreshed
 * before it expires. One per private connection; concurrent callers share one fetch.
 */
export class TokenCache {
  #token: string | null = null
  #expiresAt = 0
  #pending: Promise<string> | null = null

  constructor(readonly fetch: () => Promise<WsToken>) {}

  get(): Promise<string> {
    if (this.#token !== null && Date.now() < this.#expiresAt) return Promise.resolve(this.#token)
    this.#pending ??= this.fetch()
      .then(result => {
        this.#token = result.token
        this.#expiresAt = Date.now() + result.expires * 1000 - REFRESH_BUFFER_MS
        return result.token
      })
      .finally(() => { this.#pending = null })
    return this.#pending
  }
}
