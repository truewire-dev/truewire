/**
 * `describeReplay` over the `pets` fixture, whose every endpoint declares both transports:
 * each HTTP recording is replayed over HTTP and each WebSocket one (native or synthesized)
 * over the socket. The explicit tests below prove the generated `transport` option really
 * routes the call.
 */
import { describe, expect, it } from 'vitest'
import { describeReplay } from '../src/replay.js'
import { withClient } from './client.js'
import { fixtureRoot, packageDir } from './setup.js'

describeReplay({
  test: { describe, it },
  projectRoot: fixtureRoot,
  packageDir,
  withClient,
  importModule: url => import(/* @vite-ignore */ url),
})

describe('a seek walk (ADR 0013)', () => {
  it('moves `from` to the latest `at` of each full page and drops the re-served boundary row', async () => {
    await withClient(async (client, core) => {
      using http = core.http.recording()
      const walk = client.pets.adoptionsPaged({ from: 0, to: 9, limit: 3 })
      const pages: string[][] = []
      for await (const rows of walk) pages.push(rows.map(row => row.pet))
      expect(pages).toEqual([['Fido', 'Rex', 'Luna'], ['Max', 'Bella'], ['Coco']])
      const sent = await Promise.all(http.exchanges.map(async x => ((await x.request.json()) as { params: { from: number } }).params.from))
      expect(sent).toEqual([0, 2, 4])
      const [first, second] = await Promise.all([walk, walk.resume([2, []])])
      expect(first.map(row => row.at)).toEqual([0, 1, 2, 3, 4, 5])
      expect(second.map(row => row.at)).toEqual([2, 3, 4, 5])
    })
  })
})

describe('a dual-transport method', () => {
  it('goes over its first-declared transport by default', async () => {
    await withClient(async (client, core) => {
      using http = core.http.recording()
      expect(await client.pets.getPet({ petId: 42 })).toEqual({ id: 42, name: 'Fido' })
      expect(http.exchanges.length).toBe(1)
      expect(core.socket.isOpen).toBe(false)
    })
  })

  it('goes over the socket with `transport: \'ws\'`', async () => {
    await withClient(async (client, core) => {
      using http = core.http.recording()
      expect(await client.pets.getPet({ petId: 42 }, { transport: 'ws' })).toEqual({ id: 42, name: 'Fido' })
      expect(http.exchanges.length).toBe(0)
    })
  })

  it('a WebSocket-first endpoint defaults to the socket, and `validate: false` keeps the option', async () => {
    await withClient(async (client, core) => {
      using http = core.http.recording()
      const pets = await client.pets.listPets({ limit: 2 })
      expect(pets.map(pet => pet.name)).toEqual(['Fido', 'Rex'])
      const raw: unknown = await client.pets.listPets({ limit: 2 }, { transport: 'ws', validate: false })
      expect(raw).toEqual([{ id: 42, name: 'Fido' }, { id: 43, name: 'Rex' }])
      expect(http.exchanges.length).toBe(0)
    })
  })
})

describe('canonical', () => {
  it('renders a bigint the lossless parse kept as its digits, keys sorted at every depth', async () => {
    const { canonical } = await import('../src/replay.js')
    expect(canonical({ b: 18446744073709551617n, a: { d: [1n, 2], c: new Date(0) } }))
      .toBe('{"a":{"c":"1970-01-01T00:00:00.000Z","d":[1,2]},"b":18446744073709551617}')
    expect(canonical({ id: 18446744073709551617n })).toBe(canonical({ id: 18446744073709551617n }))
  })
})
