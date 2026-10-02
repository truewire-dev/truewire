/**
 * `seek` (ADR 0013), against the same venues `packages/truewire/test/test_codegen_paged.py`
 * walks the generated Python walker over: candles kept from either anchor, fills sharing
 * one millisecond, string ids walked downwards, a span-limited venue, raw timestamp keys.
 */
import { describe, expect, it } from 'vitest'
import { LogicError } from '../src/errors.js'
import { rowField, seek, type SeekKey, type SeekOptions } from '../src/seek.js'
import { timestampMillis, PreciseDate, epochNanoseconds, timestampNanos } from '../src/times.js'

type Candle = [number, string]

/** A venue serving one candle per `step` ticks over `[start, end]`, keeping the `cap` rows nearest `anchor`. */
function candles(start: number, end: number, anchor: 'start' | 'end', cap: number, step = 1): Candle[] {
  const rows: Candle[] = []
  for (let t = start; t <= end; t += step) rows.push([t, 'o'])
  return anchor === 'start' ? rows.slice(0, cap) : rows.slice(-cap)
}

interface Call { pos: SeekKey | undefined; edge: SeekKey | undefined }

async function walk<Row, Key extends SeekKey>(start: Key | undefined, options: Omit<SeekOptions<Row, Key>, 'fetch' | 'method' | 'field'> & { venue: (pos: Key | undefined, edge: Key | undefined) => Row[] }) {
  const calls: Call[] = []
  const pages: Row[][] = []
  const walker = seek<Row, Key>(start, {
    method: 'ordersPaged', field: '[0]', ...options,
    fetch: async (pos, edge) => { calls.push({ pos, edge }); return options.venue(pos, edge) },
  })
  for await (const page of walker.pages()) pages.push(page.rows)
  return { pages: pages.filter(rows => rows.length > 0), calls }
}

const candleKey = { read: (row: Candle) => rowField(row, [0]), keys: 'number' as const, unique: true }

describe('seek', () => {
  it('reads a row field by index and key, undefined when absent', () => {
    expect(rowField([5, 'a'], [0])).toBe(5)
    expect(rowField([5, 'a'], [-1])).toBe('a')
    expect(rowField({ t: { v: 3 } }, ['t', 'v'])).toBe(3)
    expect(rowField({ t: null }, ['t', 'v'])).toBeUndefined()
    expect(rowField([1], [4])).toBeUndefined()
  })

  it('walks forwards over a range wider than one page, never leaving it', async () => {
    const { pages, calls } = await walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: 3, far: 9, venue: pos => candles(pos!, 9, 'start', 3),
    })
    expect(pages.flat().map(r => r[0])).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    expect(calls.map(c => c.pos)).toEqual([0, 2, 4, 6, 8])
  })

  it('walks backwards by moving the end bound', async () => {
    const { pages, calls } = await walk<Candle, number>(9, {
      ...candleKey, descending: true, cap: 3, far: 0, venue: pos => candles(0, pos!, 'end', 3),
    })
    expect(pages.map(rows => rows.map(r => r[0]))).toEqual([[7, 8, 9], [5, 6], [3, 4], [1, 2], [0]])
    expect(calls.map(c => c.pos)).toEqual([9, 7, 5, 3, 1])
  })

  it('ends on a short page when the cap is known', async () => {
    const { calls } = await walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: 3, far: 1, venue: pos => candles(pos!, 1, 'start', 3),
    })
    expect(calls.length).toBe(1)
  })

  it('confirms exhaustion with one more request when no cap is known', async () => {
    const { pages, calls } = await walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: undefined, far: 4, venue: pos => candles(pos!, 4, 'start', 3),
    })
    expect(pages.flat().map(r => r[0])).toEqual([0, 1, 2, 3, 4])
    expect(calls.map(c => c.pos)).toEqual([0, 2, 4])
  })

  it('drops a re-served boundary row by key, not content', async () => {
    let served = 0
    const { pages } = await walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: 3, far: 4,
      venue: pos => { served += 1; return candles(pos!, 4, 'start', 3).map(([t]) => [t, `v${served}`] as Candle) },
    })
    expect(pages.flat().map(r => r[0])).toEqual([0, 1, 2, 3, 4])
  })

  it('computes the extreme key whatever the wire order', async () => {
    const { pages, calls } = await walk<Candle, number>(9, {
      ...candleKey, descending: true, cap: 3, far: 0, venue: pos => candles(0, pos!, 'end', 3).reverse(),
    })
    expect(pages.flat().map(r => r[0]).sort((a, b) => a - b)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    expect(calls.map(c => c.pos)).toEqual([9, 7, 5, 3, 1])
  })

  it('raises on a full page sharing one key', async () => {
    await expect(walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: 3, far: 9, venue: pos => [[pos!, 'a'], [pos!, 'b'], [pos!, 'c']],
    })).rejects.toThrow(LogicError)
  })

  it('names the cursor field relative to one row in its errors, and a bare-row cursor in words', async () => {
    await expect(walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: 3, far: 9, venue: pos => [[pos!, 'a'], [pos!, 'b'], [pos!, 'c']],
    })).rejects.toThrow('all sharing one `[0]` value')
    const bare = seek<number, number>(0, {
      method: 'ticksPaged', field: '', read: row => row, keys: 'number', unique: true, descending: false, cap: 2,
      fetch: async pos => [pos!, pos!],
    })
    await expect(bare.pages().next()).rejects.toThrow('all sharing one row value')
  })

  it('starts from the venue default when the moving bound is omitted, and takes the last string id in wire order', async () => {
    const ids = ['9', '8', '7', '6', '5']
    const { pages, calls } = await walk<{ id: string }, string>(undefined, {
      read: row => rowField(row, ['id']), keys: 'string', unique: true, descending: true, cap: 3,
      venue: pos => (pos === undefined ? ids : ids.filter(i => Number(i) < Number(pos))).slice(0, 3).map(id => ({ id })),
    })
    expect(pages.flat().map(r => r.id)).toEqual(ids)
    expect(calls.map(c => c.pos)).toEqual([undefined, '7'])
  })

  it('carries non-unique keys over and drops them by content', async () => {
    const rows = [{ t: 1, i: 'a' }, { t: 2, i: 'b' }, { t: 2, i: 'c' }, { t: 3, i: 'e' }, { t: 4, i: 'f' }]
    const { pages, calls } = await walk<{ t: number; i: string }, number>(0, {
      read: row => rowField(row, ['t']), keys: 'number', unique: false, descending: false, cap: 3, far: 9,
      venue: pos => rows.filter(r => pos! <= r.t && r.t <= 9).slice(0, 3),
    })
    expect(pages.flat().map(r => r.i)).toEqual(['a', 'b', 'c', 'e', 'f'])
    expect(calls.map(c => c.pos)).toEqual([0, 2, 3])
  })

  it('walks an integer-string cursor as bigint keys, beyond 2^53', async () => {
    const base = 2n ** 64n
    const rows = [0n, 1n, 2n, 3n, 4n].map(n => ({ id: String(base + n) }))
    const { pages, calls } = await walk<{ id: string }, bigint>(base, {
      read: row => rowField(row, ['id']), keys: 'bigint', unique: true, descending: false, cap: 2, far: base + 9n,
      venue: pos => rows.filter(r => pos! <= BigInt(r.id)).slice(0, 2),
    })
    expect(pages.flat().map(r => r.id)).toEqual(rows.map(r => r.id))
    const positions = calls.map(c => c.pos)
    expect(positions[0]).toBe(base)
    expect(positions.every((pos, i) => typeof pos === 'bigint' && (i === 0 || pos > (positions[i - 1] as bigint)))).toBe(true)
  })

  it('shifts a bigint bound by its span', async () => {
    const { calls } = await walk<{ id: string }, bigint>(10n, {
      read: row => rowField(row, ['id']), keys: 'bigint', unique: true, descending: false, cap: 100, far: 25n, span: 10,
      venue: () => [],
    })
    expect(calls).toEqual([{ pos: 10n, edge: 20n }, { pos: 20n, edge: 25n }])
  })

  it('raises when a carried row goes missing from the next page', async () => {
    let n = 0
    await expect(walk<{ t: number; i: string }, number>(0, {
      read: row => rowField(row, ['t']), keys: 'number', unique: false, descending: false, cap: 3, far: 9,
      venue: () => (++n === 1 ? [{ t: 1, i: 'a' }, { t: 2, i: 'b' }, { t: 2, i: 'c' }] : [{ t: 2, i: 'zzz' }, { t: 3, i: 'e' }]),
    })).rejects.toThrow(/no longer returned/)
  })

  it('covers a wide range in span-bounded requests', async () => {
    const { pages, calls } = await walk<Candle, number>(0, {
      ...candleKey, descending: false, cap: 3, far: 9, span: 4,
      venue: (pos, edge) => {
        expect((edge as number) - (pos as number)).toBeLessThanOrEqual(4)
        return candles(pos!, edge as number, 'start', 3)
      },
    })
    expect(pages.flat().map(r => r[0])).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    expect(calls.every(c => 0 <= (c.pos as number) && (c.edge as number) <= 9)).toBe(true)
  })

  it('refuses a span walk without both bounds', () => {
    expect(() => seek<Candle, number>(0, { method: 'ordersPaged', field: '[0]', ...candleKey, descending: false, cap: 3, span: 4, fetch: async () => [] }))
      .toThrow(/pass both bounds/)
  })

  it('parses raw timestamp keys through the bound\'s converter', async () => {
    const t = (ms: number) => new Date(ms)
    const { pages, calls } = await walk<[number, string], Date>(t(9000), {
      read: row => rowField(row, [0]), keys: timestampMillis, unique: true, descending: true, cap: 3, far: t(0),
      venue: pos => candles(0, pos!.getTime(), 'end', 3, 1000),
    })
    expect(pages.flat().map(r => r[0]).sort((a, b) => a - b)).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000])
    expect((calls[1]!.pos as Date).getTime()).toBe(7000)
  })

  it('spans a time range in unit milliseconds', async () => {
    const t = (ms: number) => new Date(ms)
    const { pages, calls } = await walk<[number, string], Date>(t(0), {
      read: row => rowField(row, [0]), keys: timestampMillis, unique: true, descending: false, cap: 3, far: t(9000),
      span: 4, spanUnitMs: 1000,
      venue: (pos, edge) => candles(pos!.getTime(), (edge as Date).getTime(), 'start', 3, 1000),
    })
    expect(pages.flat().map(r => r[0])).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000])
    expect(calls.every(c => (c.edge as Date).getTime() - (c.pos as Date).getTime() <= 4000)).toBe(true)
  })

  it('is resumable from any page\'s state', async () => {
    const walker = seek<Candle, number>(0, {
      method: 'ordersPaged', field: '[0]', ...candleKey, descending: false, cap: 3, far: 9,
      fetch: async pos => candles(pos!, 9, 'start', 3),
    })
    const pages = []
    for await (const page of walker.pages()) pages.push(page)
    const resumed = await walker.resume(pages[2]!.state)
    expect(resumed.map(r => r[0])).toEqual([5, 6, 7, 8, 9])
  })
})

describe('seek over precise times', () => {
  it('compares and shifts a PreciseDate bound in exact nanoseconds', async () => {
    const start = new PreciseDate(1_000_000_500n)
    const far = new PreciseDate(1_025_000_500n)
    const { calls } = await walk<{ t: number }, Date>(start, {
      read: row => rowField(row, ['t']), keys: timestampNanos, unique: true, descending: false, cap: 100, far, span: 10, spanUnitMs: 1,
      venue: () => [],
    })
    expect(calls.map(c => [epochNanoseconds(c.pos as Date), epochNanoseconds(c.edge as Date)]))
      .toEqual([[1_000_000_500n, 1_010_000_500n], [1_010_000_500n, 1_020_000_500n], [1_020_000_500n, 1_025_000_500n]])
  })
})

describe('seek with an exclusive far bound', () => {
  type Trade = { id: number; time: number }
  const trades: Trade[] = [1, 2, 3, 4, 5].map(id => ({ id, time: id === 4 ? 9000 : id * 1000 }))
  const byId = { read: (row: Trade) => rowField(row, ['id']), keys: 'number' as const, unique: true, descending: false, cap: 2 }
  const until = (value: Date | undefined) => ({ read: (row: Trade) => rowField(row, ['time']), keys: timestampMillis, value })

  it('drops a row past the caller\'s value and ends on the page that held it', async () => {
    const { pages, calls } = await walk<Trade, number>(1, {
      ...byId, until: until(new Date(5000)),
      venue: pos => trades.filter(t => t.id >= pos!).slice(0, 2),
    })
    expect(pages.flat().map(t => t.id)).toEqual([1, 2, 3])
    expect(calls.map(c => c.pos)).toEqual([1, 2, 3])
  })

  it('walks on untouched when the caller gave no value', async () => {
    const { pages } = await walk<Trade, number>(1, {
      ...byId, until: until(undefined),
      venue: pos => trades.filter(t => t.id >= pos!).slice(0, 2),
    })
    expect(pages.flat().map(t => t.id)).toEqual([1, 2, 3, 4, 5])
  })

  it('drops every past row on the page, keeping an in-range one after it', async () => {
    // One page, [3, 4 (past), 5]: rows are not assumed to arrive in time order.
    const { pages, calls } = await walk<Trade, number>(3, {
      ...byId, cap: 3, until: until(new Date(5000)),
      venue: pos => trades.filter(t => t.id >= pos!).slice(0, 3),
    })
    expect(pages.flat().map(t => t.id)).toEqual([3, 5])
    expect(calls.map(c => c.pos)).toEqual([3])
  })
})
