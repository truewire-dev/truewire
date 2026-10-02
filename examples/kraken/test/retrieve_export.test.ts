/**
 * The hand-written `spot.account.retrieveExport`, folded into the generated router through
 * `[typescript.extras]`: a signed POST whose zip body comes back as bytes, and whose JSON
 * error reply is raised. Against a fake `fetch`, since the recording holds only metadata.
 */
import { describe, expect, it } from 'vitest'
import { ApiError, HttpClient } from '@truewire/core'
import { SocketCore, SpotCore } from '../src/kraken/core/index.js'
import { Kraken } from '../src/kraken/index.js'
import { FAKE_CREDENTIALS } from './client.js'

function client(reply: () => Response, seen: Request[]): Kraken {
  const http = new HttpClient({ fetch: async input => { seen.push(input as Request); return reply() } })
  return new Kraken({
    spot_client: new SpotCore({ baseUrl: 'https://kraken.test', credentials: FAKE_CREDENTIALS, http }),
    market_client: new SocketCore({ url: 'ws://unused.test' }),
    private_client: new SocketCore({ url: 'ws://unused.test' }),
  })
}

describe('spot.account.retrieveExport (hand-written)', () => {
  it('signs the request and returns the zip as bytes', async () => {
    const seen: Request[] = []
    const zip = new Uint8Array([0x50, 0x4b, 0x03, 0x04])
    const bytes = await client(() => new Response(zip, { headers: { 'content-type': 'application/zip' } }), seen)
      .spot.account.retrieveExport({ id: 'TCJA' })
    expect(bytes).toEqual(zip)
    expect(new URL(seen[0]!.url).pathname).toBe('/0/private/RetrieveExport')
    expect(seen[0]!.headers.get('API-Sign')).toBeTruthy()
    expect(await seen[0]!.text()).toMatch(/^nonce=\d+&id=TCJA$/)
  })

  it('raises the JSON error Kraken answers with', async () => {
    const reply = () => new Response(JSON.stringify({ error: ['EGeneral:Invalid arguments'], result: {} }), { headers: { 'content-type': 'application/json' } })
    await expect(client(reply, []).spot.account.retrieveExport({ id: 'NOPE' })).rejects.toBeInstanceOf(ApiError)
  })
})
