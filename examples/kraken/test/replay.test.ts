/**
 * Generic replay coverage: every recorded HTTP example, through the real client.
 *
 * Each `spec/endpoints/**\/examples/<id>.request.json` is replayed against the mock by
 * calling the generated method the endpoint's function path names (`spot.account.balance`
 * is `client.spot.account.balance`), with the recorded request as the argument. A
 * recording holds wire values, so it goes through the endpoint's `Request` codec first
 * (`from_ts` becomes the `Date` the method takes); a recording the typed value cannot
 * render back exactly (a sub-millisecond timestamp, which a `Date` keeps to the
 * millisecond) is skipped, since the mock would not recognise what the client sends.
 * Private endpoints are signed with the
 * fake key pair; the mock ignores the redacted `nonce` and matches the rest of the form
 * body. Validation is on, so the test proves the codec accepts the recorded response; a
 * second call with `validate: false` proves the raw reply has the same shape. An
 * endpoint whose `surface` is hand-written (`retrieve_export`, a binary body recorded as
 * metadata) has no envelope to unwrap and is left out, as it is from the Python package.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { Codec } from '@truewire/core'
import type { Kraken } from '../src/kraken/index.js'
import { mockClient } from './client.js'
import { projectRoot } from './setup.js'

interface Example {
  function: string
  id: string
  request: Record<string, unknown>
}

/** Every recorded HTTP example under `spec/endpoints/`, with the function path of its endpoint. */
function recordedExamples(): Example[] {
  const out: Example[] = []
  const endpoints = path.join(projectRoot, 'spec', 'endpoints')
  const walk = (dir: string, segments: string[]) => {
    const spec = path.join(dir, 'endpoint.json')
    if (existsSync(spec)) {
      const endpoint = JSON.parse(readFileSync(spec, 'utf8')) as { function?: string; surface?: { kind?: string } }
      if (endpoint.surface?.kind === 'handwritten') return
      const fn = endpoint.function ?? segments.join('.')
      const examples = path.join(dir, 'examples')
      if (existsSync(examples)) {
        for (const file of readdirSync(examples).sort()) {
          if (!file.endsWith('.request.json')) continue
          const id = file.slice(0, -'.request.json'.length)
          if (!existsSync(path.join(examples, `${id}.response.json`))) continue
          const recorded = JSON.parse(readFileSync(path.join(examples, file), 'utf8')) as { request?: Record<string, unknown> }
          out.push({ function: fn, id, request: recorded.request ?? {} })
        }
      }
      return
    }
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (entry.isDirectory() && entry.name !== 'examples') walk(path.join(dir, entry.name), [...segments, entry.name])
    }
  }
  walk(endpoints, [])
  return out
}

/** `add_order` -> `addOrder`: the naming rule the generator applies to every segment. */
function camelCase(segment: string): string {
  const [head, ...rest] = segment.replace(/-/g, '_').split('_').filter(Boolean)
  return (head ?? '').replace(/^./, c => c.toLowerCase()) + rest.map(part => part[0]!.toUpperCase() + part.slice(1)).join('')
}

type Method = (request: unknown, options?: { validate?: boolean }) => Promise<unknown>

/** The bound generated method a dotted function path names on the client. */
function resolve(client: Kraken, fn: string): Method {
  const segments = fn.split('.')
  let target: unknown = client
  for (const segment of segments.slice(0, -1)) {
    target = (target as Record<string, unknown>)[camelCase(segment)]
    if (target === undefined) throw new Error(`${fn}: no router ${camelCase(segment)} on the client`)
  }
  const name = camelCase(segments.at(-1)!)
  const method = (target as Record<string, unknown>)[name]
  if (typeof method !== 'function') throw new Error(`${fn}: no method ${name} on the client`)
  return (method as Method).bind(target)
}

/**
 * The recorded request as the typed value the method takes, parsed through the endpoint
 * module's `Request` codec when it declares one, and whether dumping it renders the
 * recording back exactly.
 */
async function typedRequest(fn: string, recorded: Record<string, unknown>): Promise<{ request: unknown; exact: boolean }> {
  const file = pathToFileURL(path.join(projectRoot, 'src', 'kraken', ...fn.split('.')) + '.ts').href
  const module = (await import(/* @vite-ignore */ file)) as { Request?: Codec<unknown> }
  if (module.Request === undefined) return { request: recorded, exact: true }
  const request = module.Request.parse(recorded)
  return { request, exact: canonical(module.Request.dump(request)) === canonical(recorded) }
}

/** JSON with object keys sorted, so two renderings of one value compare equal. */
function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    typeof item === 'object' && item !== null && !Array.isArray(item)
      ? Object.fromEntries(Object.entries(item as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)))
      : item,
  )
}

describe('recorded HTTP examples replay through the generated client', () => {
  const examples = recordedExamples()
  it('finds the recordings', () => {
    expect(examples.length).toBeGreaterThan(0)
  })
  for (const example of examples) {
    it(`${example.function} (${example.id})`, async ({ skip }) => {
      const method = resolve(mockClient(), example.function)
      const { request, exact } = await typedRequest(example.function, example.request)
      if (!exact) skip('the recording holds a value the typed request cannot render back exactly')
      const validated = await method(request)
      const raw = await method(request, { validate: false })
      expect(validated).toBeDefined()
      if (Array.isArray(raw)) {
        expect(Array.isArray(validated)).toBe(true)
        expect((validated as unknown[]).length).toBe(raw.length)
      } else if (typeof raw === 'object' && raw !== null) {
        expect(Object.keys(validated as object).sort()).toEqual(Object.keys(raw).sort())
      }
    })
  }
})
