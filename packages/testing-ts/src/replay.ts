/**
 * Replay every recorded example through the real generated client, as vitest suites: the
 * TypeScript counterpart of `truewire.testing`'s `build_http_replay_test`,
 * `build_ws_rpc_replay_test` and `build_ws_replay_test`.
 *
 * For each example the method its function path names is resolved on the client
 * (`trading_ws.add_order` is `client.tradingWs.addOrder`). A recording holds wire values
 * and a method takes typed ones, so the recorded call is parsed through the endpoint
 * module's `Request` codec (a stream's `Parameters`) first; a recording the typed value
 * cannot render back exactly (an RFC 3339 fraction of a width the runtime does not write,
 * `.5` or `.1234`) is skipped, since the mock would not recognise what the client sends.
 *
 * - HTTP: the call is made with validation on, then with `validate: false`, and the two
 *   values must have the same shape.
 * - WebSocket `rpc`: the call is made with validation on.
 * - WebSocket `stream`: the method is subscribed and its first pushed message read (with
 *   validation on); an example that recorded no push is skipped.
 *
 * A dual-transport endpoint's call passes `transport: 'http'` or `transport: 'ws'`, so
 * each half is proved over its own transport. An endpoint whose `surface` is declared
 * (hand-written or absent), or whose method the client does not have, is skipped with the
 * reason: `truewire surface` is the gate for a missing method, not this.
 *
 * The runner's `describe` and `it` are passed in rather than imported: a `file:` copy of
 * this package can carry its own copy of the runner, and suites registered on that copy
 * never run. Assertions use `node:assert`.
 */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import { httpExamples, wsExamples, type DiscoveryOptions, type HttpExample, type WsExample } from './examples.js'
import { endpointModulePath, resolveMethod, type Method } from './names.js'
import { stringifyJson } from '@truewire/core'

/** A test context's `skip`, as vitest's `it` hands it to the test body. */
export type Skip = (note?: string) => never

/** The pieces of a test runner the replay suites register with: vitest's `{ describe, it }`. */
export interface TestApi {
  describe(name: string, body: () => void): void
  it(name: string, body: (context: { skip: Skip }) => Promise<void> | void): void
}

export interface ReplayOptions<Client> extends DiscoveryOptions {
  /** The runner to register with: `{ describe, it }` from `vitest`. */
  test: TestApi
  /** The project root: the directory holding `truewire.toml`. */
  projectRoot: string
  /** The generated package's directory, `<[typescript].src>/<[typescript].package>`. */
  packageDir: string
  /**
   * Run `body` with a client pointed at the mock (validation on) and release it after:
   * close its sockets. Called once per example.
   */
  withClient: (body: (client: Client) => Promise<void>) => Promise<void>
  /** Keep only the examples this returns `true` for. */
  include?: (example: HttpExample | WsExample) => boolean
  /**
   * Load a generated endpoint module by its file URL, to read its `Request`/`Parameters`
   * codec. Under vitest pass `url => import(url)` from the test file itself: an `import()`
   * made from inside this package (under `node_modules`) is not transformed, and Node's
   * own TypeScript stripping rejects generated classes. The native `import()` by default.
   */
  importModule?: ModuleLoader
}

/** Loads a module by file URL. */
export type ModuleLoader = (url: string) => Promise<unknown>

const nativeImport: ModuleLoader = url => import(/* @vite-ignore */ url)

interface Codec {
  parse(value: unknown): unknown
  dump(value: unknown): unknown
}

/** A recorded call as the typed value a generated method takes. */
export interface TypedValue {
  value: unknown
  /** Dumping `value` renders the recording back exactly. */
  exact: boolean
  /** The endpoint module exports the codec; when it does not, the method may take no value. */
  declared: boolean
}

/** Parse a recording through the endpoint module's `Request` (or `Parameters`) codec. */
export async function typedValue(
  packageDir: string, fn: string, recorded: Record<string, unknown>, codec: 'Request' | 'Parameters' = 'Request',
  importModule: ModuleLoader = nativeImport,
): Promise<TypedValue> {
  const module = (await importModule(pathToFileURL(endpointModulePath(packageDir, fn)).href)) as Record<string, unknown>
  const found = module[codec] as Codec | undefined
  if (found === undefined || typeof found.parse !== 'function') return { value: recorded, exact: true, declared: false }
  const value = found.parse(recorded)
  return { value, exact: canonical(found.dump(value)) === canonical(recorded), declared: true }
}

/**
 * JSON with object keys sorted, so two renderings of one value compare equal. A `bigint`
 * (an integer the lossless parse kept exact) renders as its digits, like the number it was.
 */
export function canonical(value: unknown): string {
  const sorted = (item: unknown): unknown => {
    if (item === null || typeof item !== 'object') return item
    if (typeof (item as { toJSON?: unknown }).toJSON === 'function') return sorted((item as { toJSON(): unknown }).toJSON())
    if (Array.isArray(item)) return item.map(sorted)
    return Object.fromEntries(Object.entries(item as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, sorted(v)]))
  }
  return stringifyJson(sorted(value))
}

/** The arguments of a call: the typed value (unless the method takes none) and the options. */
function callArgs(typed: TypedValue, recorded: Record<string, unknown>, options: Record<string, unknown>): unknown[] {
  if (!typed.declared && Object.keys(recorded).length === 0) return [options]
  return [typed.value, options]
}

/** The method to call, or a skip with the reason there is none. */
function methodFor(client: unknown, example: HttpExample | WsExample, skip: Skip): Method {
  const { endpoint } = example
  if (endpoint.surface !== undefined) skip(`declared ${endpoint.surface.kind}: ${endpoint.surface.reason ?? ''}`)
  const method = resolveMethod(client, endpoint.function)
  if (method === undefined) skip(`${endpoint.function} has no generated method on the client`)
  return method!
}

function title(example: HttpExample | WsExample): string {
  return `${example.endpoint.function} (${example.id}${example.transport === 'ws' && example.synthesized ? ', from HTTP' : ''})`
}

function assertSameShape(validated: unknown, raw: unknown): void {
  assert.notEqual(validated, undefined, 'the validated call returned nothing')
  if (Array.isArray(raw)) {
    assert.ok(Array.isArray(validated), 'the raw reply is an array and the validated value is not')
    assert.equal((validated as unknown[]).length, raw.length)
  } else if (typeof raw === 'object' && raw !== null) {
    assert.deepEqual(Object.keys(validated as object).sort(), Object.keys(raw).sort())
  }
}

/** A suite's first test: a replay over zero recordings is a gap, never a pass. */
function findsRecordings(test: TestApi, count: number): void {
  test.it('finds the recordings', () => {
    assert.ok(count > 0, 'no recorded example was found under spec/endpoints')
  })
}

/** Register a suite replaying every recorded HTTP example. */
export function describeHttpReplay<Client>(options: ReplayOptions<Client>): void {
  const examples = httpExamples(options.projectRoot, options).filter(example => options.include?.(example) ?? true)
  const { describe, it } = options.test
  describe('recorded HTTP examples replay through the generated client', () => {
    findsRecordings(options.test, examples.length)
    for (const example of examples) {
      it(title(example), async ({ skip }) => {
        const typed = await typedValue(options.packageDir, example.endpoint.function, example.request, 'Request', options.importModule)
        if (!typed.exact) skip('the recording holds a value the typed request cannot render back exactly')
        const transport = example.endpoint.dual ? { transport: 'http' } : {}
        await options.withClient(async client => {
          const method = methodFor(client, example, skip)
          const validated = await method(...callArgs(typed, example.request, transport))
          const raw = await method(...callArgs(typed, example.request, { ...transport, validate: false }))
          assertSameShape(validated, raw)
        })
      })
    }
  })
}

/** Register a suite replaying every recorded WebSocket example: `rpc` calls and `stream` subscriptions. */
export function describeWsReplay<Client>(options: ReplayOptions<Client>): void {
  const examples = wsExamples(options.projectRoot, options).filter(example => options.include?.(example) ?? true)
  const { describe, it } = options.test
  describe('recorded WebSocket examples replay through the generated client', () => {
    findsRecordings(options.test, examples.length)
    for (const example of examples) {
      it(title(example), async ({ skip }) => {
        const stream = example.endpoint.kind === 'stream'
        if (stream && example.messages.length === 0) skip('no recorded push: nothing to read past the subscribe step')
        const typed = await typedValue(options.packageDir, example.endpoint.function, example.parameters, stream ? 'Parameters' : 'Request', options.importModule)
        if (!typed.exact) skip('the recording holds a value the typed request cannot render back exactly')
        const transport = example.endpoint.dual ? { transport: 'ws' } : {}
        await options.withClient(async client => {
          const method = methodFor(client, example, skip)
          const result = method(...callArgs(typed, example.parameters, transport))
          if (!stream) {
            await result
            return
          }
          const iterator = (result as AsyncIterable<unknown>)[Symbol.asyncIterator]()
          const first = await iterator.next()
          assert.equal(first.done, false, 'the stream ended before its first message')
          assert.notEqual(first.value, undefined)
        })
      })
    }
  })
}

/** `describeHttpReplay` and `describeWsReplay`, each only when the project records examples for it. */
export function describeReplay<Client>(options: ReplayOptions<Client>): void {
  if (httpExamples(options.projectRoot, options).length > 0) describeHttpReplay(options)
  if (wsExamples(options.projectRoot, options).length > 0) describeWsReplay(options)
}
