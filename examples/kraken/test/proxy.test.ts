import { Kraken } from '../src/kraken/main.js'
/**
 * Packages clause P18 through the hand-written core: one `proxy` reaches the REST
 * transport and both WebSocket connections. That the runtime then sends through it is
 * `@truewire/core`'s own test (`test/proxy.test.ts`).
 */
import { HttpClient, LogicError } from '@truewire/core'
import { describe, expect, it } from 'vitest'
import { Core } from '../src/kraken/core/index.js'

const PROXY = 'http://proxy.example:3128'

describe('Core({ proxy })', () => {
  it('hands the proxy to the REST transport and to both sockets', () => {
    const core = new Core({ proxy: PROXY })
    expect([core.spot_client.http.proxy, core.market_client.conn.proxy, core.private_client.conn.proxy])
      .toEqual([PROXY, PROXY, PROXY])
    const direct = new Core()
    expect([direct.spot_client.http.proxy, direct.market_client.conn.proxy]).toEqual([undefined, undefined])
  })

  it('refuses a proxy beside a ready-made HTTP client, which would ignore it', () => {
    expect(() => new Core({ proxy: PROXY, http: new HttpClient() })).toThrow(LogicError)
  })
})

it('passes root rate/retry through the combined core with and without a proxy', () => {
  const rate = Object.getOwnPropertyDescriptor(Kraken, 'RATE')!
  const retry = Object.getOwnPropertyDescriptor(Kraken, 'RETRY')!
  try {
    Object.defineProperty(Kraken, 'RATE', { value: 3 })
    Object.defineProperty(Kraken, 'RETRY', { value: true })
    for (const proxy of [undefined, PROXY]) {
      const core = new Core({ proxy })
      expect(core.spot_client.http.rate).toBe(3)
      expect(core.spot_client.http.retry).toBe(true)
    }
  } finally {
    Object.defineProperty(Kraken, 'RATE', rate)
    Object.defineProperty(Kraken, 'RETRY', retry)
  }
})
