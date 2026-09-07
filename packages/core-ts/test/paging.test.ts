/**
 * Pins `PaginatedResponse`'s resumable, retry-safe contract: every page is one pure
 * `next(state)` call, so a page can be retried, a walk resumed from any page's state, and a
 * per-call invoker (`via`) wrapped around each fetch without unrolling the loop by hand.
 */
import { describe, expect, it } from 'vitest'
import { PaginatedResponse, type Page } from '../src/paging.js'

/** A walk over integer states `1..pages.size`, recording every `next` call. */
function counting(pages: Record<number, string[]>) {
  const calls: number[] = []
  const next = async (state: number): Promise<[string[], number | null]> => {
    calls.push(state)
    return [pages[state]!, state + 1 in pages ? state + 1 : null]
  }
  return { paging: new PaginatedResponse(1, next), calls }
}

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = []
  for await (const item of iterable) out.push(item)
  return out
}

describe('PaginatedResponse', () => {
  it('await flattens every page', async () => {
    const { paging } = counting({ 1: ['a', 'b'], 2: [], 3: ['c'] })
    expect(await paging).toEqual(['a', 'b', 'c'])
  })

  it('iteration skips empty pages', async () => {
    const { paging } = counting({ 1: ['a', 'b'], 2: [], 3: ['c'] })
    expect(await collect(paging)).toEqual([['a', 'b'], ['c']])
  })

  it('pages() yields every page with its states, empty ones included', async () => {
    // A checkpoint needs every state transition, not only the ones that carried rows.
    const { paging } = counting({ 1: ['a'], 2: [], 3: ['c'] })
    const expected: Page<string, number>[] = [
      { rows: ['a'], state: 1, next: 2 },
      { rows: [], state: 2, next: 3 },
      { rows: ['c'], state: 3, next: null },
    ]
    expect(await collect(paging.pages())).toEqual(expected)
  })

  it('resume starts from a saved state', async () => {
    const { paging, calls } = counting({ 1: ['a'], 2: ['b'], 3: ['c'] })
    expect(await paging.resume(2)).toEqual(['b', 'c'])
    expect(calls).toEqual([2, 3])
  })

  it('via routes every fetch through the invoker', async () => {
    const { paging } = counting({ 1: ['a'], 2: ['b'] })
    const seen: string[] = []
    expect(await paging.via(async fn => { seen.push('fetch'); return fn() })).toEqual(['a', 'b'])
    expect(seen).toEqual(['fetch', 'fetch'])
  })

  it('via lets the invoker retry one page', async () => {
    // A transient failure on page two is retried at page two; page one is never fetched again.
    const attempts: number[] = []
    const next = async (state: number): Promise<[string[], number | null]> => {
      attempts.push(state)
      if (state === 2 && attempts.filter(s => s === 2).length === 1) throw new Error('transient')
      return [[String(state)], state < 3 ? state + 1 : null]
    }
    const retried = async (fn: () => Promise<[string[], number | null]>) => {
      try { return await fn() } catch { return fn() }
    }
    expect(await new PaginatedResponse(1, next).via(retried)).toEqual(['1', '2', '3'])
    expect(attempts).toEqual([1, 2, 2, 3])
  })

  it('via and resume leave the original untouched', async () => {
    const { paging, calls } = counting({ 1: ['a'], 2: ['b'] })
    const resumed = paging.resume(2)
    const wrapped = paging.via(fn => fn())
    expect(paging.init).toBe(1)
    expect(resumed.init).toBe(2)
    expect(wrapped.next).not.toBe(paging.next)
    expect(await paging).toEqual(['a', 'b'])
    expect(calls).toEqual([1, 2])
  })

  it('two iterations share no state', async () => {
    // `next` is pure in `state`, so nothing about one iteration leaks into another.
    const { paging, calls } = counting({ 1: ['a'], 2: ['b'] })
    const first = await collect(paging)
    const second = await collect(paging)
    expect(first).toEqual([['a'], ['b']])
    expect(second).toEqual(first)
    expect(calls).toEqual([1, 2, 1, 2])
  })

  it('is a thenable: works with Promise.all and then/catch', async () => {
    const { paging } = counting({ 1: ['a'] })
    expect(await Promise.all([paging, paging.resume(1)])).toEqual([['a'], ['a']])
    const failing = new PaginatedResponse(1, async () => { throw new Error('nope') })
    await expect(failing.then(x => x)).rejects.toThrow('nope')
    expect(await failing.then(undefined, () => 'caught')).toBe('caught')
  })
})
