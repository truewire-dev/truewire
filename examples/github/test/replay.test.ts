/**
 * Generic replay coverage: every recorded HTTP example, through the real client.
 *
 * Each `spec/endpoints/**\/examples/<id>.request.json` is replayed against the mock by
 * calling the generated method the endpoint's function path names (`repos.list_commits`
 * is `client.repos.listCommits`), with the recorded request as the argument. Validation
 * is on, so the test proves the codec accepts the recorded response; a second call with
 * `validate: false` proves the raw reply has the same shape.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, inject, it } from 'vitest'
import { GitHub } from '../src/github/index.js'
import { Core } from '../src/github/core/index.js'
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
      const endpoint = JSON.parse(readFileSync(spec, 'utf8')) as { function?: string }
      const fn = endpoint.function ?? segments.join('.')
      const examples = path.join(dir, 'examples')
      if (existsSync(examples)) {
        for (const file of readdirSync(examples).sort()) {
          if (!file.endsWith('.request.json')) continue
          const id = file.slice(0, -'.request.json'.length)
          if (!existsSync(path.join(examples, `${id}.response.json`))) continue
          const recorded = JSON.parse(readFileSync(path.join(examples, file), 'utf8')) as { request?: Record<string, unknown>; parameters?: Record<string, unknown> }
          out.push({ function: fn, id, request: recorded.request ?? recorded.parameters ?? {} })
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

/** `list_commits` -> `listCommits`: the naming rule the generator applies to every segment. */
function camelCase(segment: string): string {
  const [head, ...rest] = segment.replace(/-/g, '_').split('_').filter(Boolean)
  return (head ?? '').replace(/^./, c => c.toLowerCase()) + rest.map(part => part[0]!.toUpperCase() + part.slice(1)).join('')
}

type Method = (request: unknown, options?: { validate?: boolean }) => Promise<unknown>

/** The bound generated method a dotted function path names on the client. */
function resolve(client: GitHub, fn: string): Method {
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

describe('recorded HTTP examples replay through the generated client', () => {
  const examples = recordedExamples()
  it('finds the recordings', () => {
    expect(examples.length).toBeGreaterThan(0)
  })
  for (const example of examples) {
    it(`${example.function} (${example.id})`, async () => {
      const client = new GitHub(new Core({ baseUrl: inject('httpBaseUrl') }))
      const method = resolve(client, example.function)
      const validated = await method(example.request)
      const raw = await method(example.request, { validate: false })
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
