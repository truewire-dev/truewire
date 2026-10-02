/**
 * W15: `truewire.toml` refuses `spot.funding.withdraw`, so the generated method rejects
 * with `RefusedByPolicy` before any request. A fake `fetch` records every request that
 * reaches it: none for the refused endpoint, one for a sibling that is not refused.
 */
import { describe, expect, it } from 'vitest'
import { HttpClient, LogicError, TruewireError } from '@truewire/core'
import { SocketCore, SpotCore } from '../src/kraken/core/index.js'
import { Kraken, RefusedByPolicy } from '../src/kraken/index.js'
import { sign } from '../src/kraken/core/auth.js'
import { FAKE_CREDENTIALS } from './client.js'

function client(seen: Request[]): Kraken {
  const reply = () => new Response(JSON.stringify({ error: ['EGeneral:Permission denied'] }), { headers: { 'content-type': 'application/json' } })
  const http = new HttpClient({ fetch: async input => { seen.push(input as Request); return reply() } })
  return new Kraken({
    spot_client: new SpotCore({ baseUrl: 'https://kraken.test', credentials: FAKE_CREDENTIALS, http }),
    market_client: new SocketCore({ url: 'ws://unused.test' }),
    private_client: new SocketCore({ url: 'ws://unused.test' }),
  })
}

describe('[policy].refuse', () => {
  it('rejects spot.funding.withdraw before any request', async () => {
    const seen: Request[] = []
    const kraken = client(seen)
    const request = { asset: 'XBT', key: 'cold-storage', amount: '0.5' }

    const refused = await kraken.spot.funding.withdraw(request).catch((error: unknown) => error)
    expect(refused).toBeInstanceOf(RefusedByPolicy)
    expect(refused).toBeInstanceOf(LogicError)
    expect((refused as RefusedByPolicy).endpoint).toBe('spot.funding.withdraw')
    await expect(kraken.spot.funding.withdraw(request, { validate: false })).rejects.toBeInstanceOf(RefusedByPolicy)
    expect(seen).toHaveLength(0)

    // The fake does see what the client sends: a sibling that is not refused reaches it.
    const allowed = await kraken.spot.funding.withdrawMethods({}).catch((error: unknown) => error)
    expect(allowed).toBeInstanceOf(TruewireError)
    expect(allowed).not.toBeInstanceOf(RefusedByPolicy)
    expect(seen).toHaveLength(1)
  })
})

it('retries a private request with a fresh nonce and matching signature', async () => {
  const seen: Request[] = []
  const http = new HttpClient({ retry: true, fetch: async input => {
    seen.push(input as Request)
    return new Response(JSON.stringify({ error: [], result: { token: 'synthetic-token', expires: 900 } }), {
      status: seen.length === 1 ? 503 : 200, headers: { 'Retry-After': '0' },
    })
  } })
  const core = new SpotCore({ baseUrl: 'https://kraken.test', credentials: FAKE_CREDENTIALS, http })
  await core.getWsToken()
  expect(seen).toHaveLength(2)
  const nonces: number[] = []
  for (const request of seen) {
    const body = await request.text()
    const nonce = Number(new URLSearchParams(body).get('nonce'))
    nonces.push(nonce)
    expect(request.headers.get('API-Sign')).toBe(sign('/0/private/GetWebSocketsToken', nonce, body, FAKE_CREDENTIALS.privateKey))
  }
  expect(nonces[1]).toBeGreaterThan(nonces[0]!)
})
