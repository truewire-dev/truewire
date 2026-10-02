/**
 * The generated `adoptionsPaged` against a fake venue whose moving bound is inclusive: it
 * re-serves the row at `from`, so a page must hold at least 2 rows for the walk to advance
 * (ADR 0013). The walk sends the size it clamps, on every request.
 */
import { describe, expect, it } from 'vitest'
import type { HttpEndpoint } from '@truewire/core'
import { Adoptions, type Adoption } from './fixture/src/pets/pets/adoptions.js'

const ROWS: Adoption[] = Array.from({ length: 10 }, (_, at) => ({ at, pet: `pet${at}` }))

/** Serves the oldest `min(limit, 500)` rows of `[from, to]`, both bounds inclusive; logs each request. */
function venue(sent: Record<string, unknown>[]): HttpEndpoint {
  return {
    async request(call: any) {
      const q = call.requestCodec.dump(call.request) as { from?: number; to?: number; limit?: number }
      sent.push(q)
      const kept = ROWS.filter(row => row.at >= (q.from ?? -Infinity) && row.at <= (q.to ?? Infinity))
      return call.responseCodec.parse(kept.slice(0, Math.min(q.limit ?? 3, 500)))
    },
  } as HttpEndpoint
}

describe('a seek walk over an inclusive moving bound', () => {
  it('limit 1 walks every row, requesting pages of 2', async () => {
    const sent: Record<string, unknown>[] = []
    const rows = await new Adoptions(venue(sent)).adoptionsPaged({ from: 0, to: 9, limit: 1 })
    expect(rows.map(row => row.at)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    expect(new Set(sent.map(q => q.limit))).toEqual(new Set([2]))
  })

  it('limit 600 sends the maximum, 500, from the first request on', async () => {
    const sent: Record<string, unknown>[] = []
    const rows = await new Adoptions(venue(sent)).adoptionsPaged({ from: 0, to: 9, limit: 600 })
    expect(rows.length).toBe(10)
    expect(sent.map(q => q.limit)).toEqual([500])
  })

  it('no limit stays unset on the wire', async () => {
    const sent: Record<string, unknown>[] = []
    const rows = await new Adoptions(venue(sent)).adoptionsPaged({ from: 0, to: 9 })
    expect(rows.length).toBe(10)
    expect(sent.every(q => !('limit' in q))).toBe(true)
  })
})
