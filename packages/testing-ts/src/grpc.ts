/**
 * gRPC for `@truewire/testing` (ADR 0017): an in-process gRPC server answering every
 * recorded gRPC example, and a suite replaying each through the generated client.
 *
 * Recordings are the proto JSON forms of the messages (`<id>.request.json`,
 * `<id>.response.json`). The server learns each method from the generated endpoint module's
 * `method` export (the protobuf-es descriptor), so no descriptor is read twice: a call whose
 * request equals a recorded one (compared as messages) gets that example's response, any
 * other call `NOT_FOUND`.
 *
 * Imported from `@truewire/testing/grpc` only: it needs `@bufbuild/protobuf`,
 * `@connectrpc/connect` and `@connectrpc/connect-node`.
 */
import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import http2 from 'node:http2'
import type { AddressInfo } from 'node:net'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { create, equals, fromJson, toJson, type DescMessage, type DescMethodUnary, type JsonValue, type MessageShape, type Registry } from '@bufbuild/protobuf'
import { base64Decode } from '@bufbuild/protobuf/wire'
import { AnySchema } from '@bufbuild/protobuf/wkt'
import { Code, ConnectError } from '@connectrpc/connect'
import { connectNodeAdapter } from '@connectrpc/connect-node'
import { endpointRecords, type DiscoveryOptions, type EndpointRecord } from './examples.js'
import { endpointModulePath, resolveMethod } from './names.js'
import type { ModuleLoader, TestApi } from './replay.js'

export interface GrpcExample {
  transport: 'grpc'
  endpoint: EndpointRecord
  id: string
  /** The recorded request message, proto JSON. */
  request: JsonValue
  /** The recorded response message, proto JSON. */
  response: JsonValue
}

/** Every recorded gRPC example of the project, sorted by function path then id. */
export function grpcExamples(projectRoot: string, options: DiscoveryOptions = {}): GrpcExample[] {
  const out: GrpcExample[] = []
  for (const endpoint of endpointRecords(projectRoot, options)) {
    if (endpoint.kind !== 'grpc' || endpoint.surface?.kind === 'absent') continue
    const dir = path.join(endpoint.dir, 'examples')
    if (!existsSync(dir)) continue
    for (const name of readdirSync(dir).sort()) {
      if (!name.endsWith('.request.json')) continue
      const id = name.slice(0, -'.request.json'.length)
      const response = path.join(dir, `${id}.response.json`)
      if (!existsSync(response)) continue
      out.push({
        transport: 'grpc', endpoint, id,
        request: JSON.parse(readFileSync(path.join(dir, name), 'utf8')) as JsonValue,
        response: JSON.parse(readFileSync(response, 'utf8')) as JsonValue,
      })
    }
  }
  return out
}

type AnyMethod = DescMethodUnary<DescMessage, DescMessage>

const nativeImport: ModuleLoader = url => import(/* @vite-ignore */ url)

/** The `method` descriptor a generated gRPC endpoint module exports. */
export async function grpcMethod(packageDir: string, fn: string, importModule: ModuleLoader = nativeImport): Promise<AnyMethod> {
  const module = (await importModule(pathToFileURL(endpointModulePath(packageDir, fn)).href)) as { method?: AnyMethod }
  if (module.method === undefined) throw new Error(`${fn}: the generated module exports no gRPC \`method\``)
  return module.method
}

/** A JSON object, not an array. */
function isObject(value: unknown): value is Record<string, JsonValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** A `google.protobuf.Any` recorded as its fields, `{type_url, value}` with `value` base64: the form a Python (betterproto) capture writes. */
function isLegacyAny(value: unknown): value is { type_url: string; value?: string } {
  return isObject(value) && typeof value.type_url === 'string' && !('@type' in value)
}

type Patch = (target: Record<string, unknown>) => void

/**
 * `json` without its legacy-form `Any`s, and the patch that puts each back into the decoded
 * message as the binary `Any` it records: that form carries the packed bytes, so it needs no
 * registry, unlike proto JSON's `{"@type": ...}`.
 */
function stripLegacyAny(desc: DescMessage, json: JsonValue): [JsonValue, Patch] {
  if (!isObject(json)) return [json, () => {}]
  const out: Record<string, JsonValue> = { ...json }
  const patches: Patch[] = []
  const any = (value: { type_url: string; value?: string }) => create(AnySchema, { typeUrl: value.type_url, value: base64Decode(value.value ?? '') })
  for (const field of desc.fields) {
    const key = field.jsonName in out ? field.jsonName : field.name in out ? field.name : undefined
    if (key === undefined) continue
    const value = out[key]
    if (field.fieldKind === 'message') {
      const get = (target: Record<string, unknown>): unknown => {
        if (field.oneof === undefined) return target[field.localName]
        const slot = target[field.oneof.localName] as { case?: string; value?: unknown } | undefined
        return slot?.case === field.localName ? slot.value : undefined
      }
      if (field.message.typeName === AnySchema.typeName && isLegacyAny(value)) {
        delete out[key]
        const packed = any(value)
        patches.push(target => {
          if (field.oneof === undefined) target[field.localName] = packed
          else target[field.oneof.localName] = { case: field.localName, value: packed }
        })
        continue
      }
      const [inner, patch] = stripLegacyAny(field.message, value)
      out[key] = inner
      patches.push(target => { const sub = get(target); if (sub) patch(sub as Record<string, unknown>) })
    } else if (field.fieldKind === 'list' && field.listKind === 'message' && Array.isArray(value)) {
      const isAny = field.message.typeName === AnySchema.typeName
      out[key] = value.map((item, index) => {
        if (isAny && isLegacyAny(item)) {
          const packed = any(item)
          patches.push(target => { (target[field.localName] as unknown[])[index] = packed })
          return {}
        }
        const [inner, patch] = stripLegacyAny(field.message, item)
        patches.push(target => patch((target[field.localName] as Record<string, unknown>[])[index]!))
        return inner
      })
    } else if (field.fieldKind === 'map' && field.mapKind === 'message' && isObject(value)) {
      const isAny = field.message.typeName === AnySchema.typeName
      out[key] = Object.fromEntries(Object.entries(value).map(([entry, item]) => {
        if (isAny && isLegacyAny(item)) {
          const packed = any(item)
          patches.push(target => { (target[field.localName] as Record<string, unknown>)[entry] = packed })
          return [entry, {}]
        }
        const [inner, patch] = stripLegacyAny(field.message, item)
        patches.push(target => patch((target[field.localName] as Record<string, Record<string, unknown>>)[entry]!))
        return [entry, inner]
      }))
    }
  }
  return [out, target => { for (const patch of patches) patch(target) }]
}

/**
 * A recorded message decoded: proto JSON (unknown fields ignored; `@type` `Any`s resolved
 * through `registry`), plus `google.protobuf.Any`s recorded in their field form
 * `{type_url, value}`, which decode to the binary `Any` they hold without a registry.
 */
export function recordedMessage<Desc extends DescMessage>(schema: Desc, json: JsonValue, registry?: Registry): MessageShape<Desc> {
  const [clean, patch] = stripLegacyAny(schema, json)
  const message = fromJson(schema, clean, { ignoreUnknownFields: true, registry })
  patch(message as unknown as Record<string, unknown>)
  return message
}

/** A message as proto JSON for a failure message; its binary size when it holds an `Any` no registry resolves. */
function describeMessage(schema: DescMessage, message: MessageShape<DescMessage>, registry?: Registry): string {
  try {
    return JSON.stringify(toJson(schema, message, { useProtoFieldName: true, registry }))
  } catch {
    return `<${schema.typeName}: not representable as proto JSON without a registry>`
  }
}

export interface GrpcMockOptions extends DiscoveryOptions {
  projectRoot: string
  /** The generated package's directory, `<[typescript].src>/<[typescript].package>`. */
  packageDir: string
  importModule?: ModuleLoader
  /** Resolves `@type` `Any`s in the recordings (proto JSON); the field form `{type_url, value}` needs none. */
  registry?: Registry
}

export interface GrpcMock {
  /** `http://127.0.0.1:<port>`, a plaintext HTTP/2 server. */
  baseUrl: string
  /** How many calls of a function path the mock answered. */
  answered(fn: string): number
  close(): Promise<void>
}

/** Start the in-process gRPC server over every recorded gRPC example. */
export async function startGrpcMock(options: GrpcMockOptions): Promise<GrpcMock> {
  const examples = grpcExamples(options.projectRoot, options)
  const byFunction = new Map<string, GrpcExample[]>()
  for (const example of examples) byFunction.set(example.endpoint.function, [...(byFunction.get(example.endpoint.function) ?? []), example])
  const methods = new Map<string, AnyMethod>()
  for (const fn of byFunction.keys()) {
    if (byFunction.get(fn)![0]!.endpoint.surface?.kind === 'handwritten') continue
    methods.set(fn, await grpcMethod(options.packageDir, fn, options.importModule))
  }
  const counts = new Map<string, number>()
  const handler = connectNodeAdapter({
    grpc: true, grpcWeb: false, connect: false,
    routes: router => {
      for (const [fn, method] of methods) {
        router.rpc(method, async request => {
          for (const example of byFunction.get(fn) ?? []) {
            if (!equals(method.input, request, recordedMessage(method.input, example.request, options.registry))) continue
            counts.set(fn, (counts.get(fn) ?? 0) + 1)
            return recordedMessage(method.output, example.response, options.registry)
          }
          throw new ConnectError(`@truewire/testing: ${fn}: no recorded example matches ${describeMessage(method.input, request, options.registry)}`, Code.NotFound)
        })
      }
    },
  })
  const server = http2.createServer(handler)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  return {
    baseUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    answered: fn => counts.get(fn) ?? 0,
    close: () => new Promise<void>(resolve => server.close(() => resolve())),
  }
}

export interface GrpcReplayOptions<Client> extends DiscoveryOptions {
  test: TestApi
  projectRoot: string
  packageDir: string
  /** Run `body` with a client whose gRPC core points at a `startGrpcMock` server. */
  withClient: (body: (client: Client) => Promise<void>) => Promise<void>
  importModule?: ModuleLoader
  /** Resolves `@type` `Any`s in the recordings, as for `startGrpcMock`. */
  registry?: Registry
}

/** Register a suite replaying every recorded gRPC example: the response must equal the recording. */
export function describeGrpcReplay<Client>(options: GrpcReplayOptions<Client>): void {
  const examples = grpcExamples(options.projectRoot, options)
  const { describe, it } = options.test
  describe('recorded gRPC examples replay through the generated client', () => {
    it('finds the recordings', () => {
      assert.ok(examples.length > 0, 'no recorded gRPC example was found under spec/endpoints')
    })
    for (const example of examples) {
      it(`${example.endpoint.function} (${example.id})`, async ({ skip }) => {
        const { endpoint } = example
        if (endpoint.surface !== undefined) skip(`declared ${endpoint.surface.kind}: ${endpoint.surface.reason ?? ''}`)
        const method = await grpcMethod(options.packageDir, endpoint.function, options.importModule)
        await options.withClient(async client => {
          const call = resolveMethod(client, endpoint.function)
          if (call === undefined) skip(`${endpoint.function} has no generated method on the client`)
          const got = (await call!(recordedMessage(method.input, example.request, options.registry))) as MessageShape<DescMessage>
          const want = recordedMessage(method.output, example.response, options.registry)
          assert.ok(
            equals(method.output, got, want),
            `${endpoint.function} (${example.id}): the response differs from the recording:\n  got  ${describeMessage(method.output, got, options.registry)}\n  want ${describeMessage(method.output, want, options.registry)}`,
          )
        })
      })
    }
  })
}
