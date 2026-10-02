/**
 * Example discovery: every endpoint under `spec/endpoints/` and the recordings beside it,
 * read with the same pairing rules as `truewire.spec.repo` (Python), so a TypeScript
 * replay covers exactly the examples `truewire mock` serves.
 *
 * - An endpoint is any directory holding `endpoint.json`; its function path is the
 *   authored `function`, else its directory path under `spec/endpoints/`, `/` read as `.`.
 * - HTTP (`transports` includes `http`): `<id>.request.json` paired with
 *   `<id>.response.json`. The call's value is the recording's `request`, else its legacy
 *   `parameters`.
 * - WebSocket (`transports` includes `ws`, or `kind: stream`): `<id>.parameters.json`
 *   paired with `<id>.reply.json`, `<id>.messages.json`/`<id>.message.json` or
 *   `<id>.messages.protobuf.json`. A parameters file with none of `description`,
 *   `parameters`, `payload` at its top level is a bare `parameters` object.
 * - An `rpc` endpoint declaring both transports gets a WebSocket example synthesized from
 *   every HTTP one whose id has no native WebSocket recording: the same `parameters`, a
 *   JSON-RPC frame naming `spec.path` as the method, and the HTTP reply's body as the reply.
 */
import { parseJsonText } from '@truewire/core'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

export type Transport = 'http' | 'ws'

/** An endpoint's declared `surface`, when the backend does not generate its method. */
export interface Surface {
  kind: 'handwritten' | 'absent'
  symbol?: string
  reason?: string
}

export interface EndpointRecord {
  /** The dotted function path. */
  function: string
  /** The endpoint's directory. */
  dir: string
  kind: 'rpc' | 'stream' | 'grpc'
  /** The declared transports; `['ws']` for a stream, `[]` for gRPC. */
  transports: Transport[]
  /** `spec.path`: the HTTP path template or the RPC method name. */
  path: string | undefined
  surface: Surface | undefined
  /** An `rpc` endpoint declaring both `http` and `ws`: its method takes a `transport` option. */
  dual: boolean
}

export interface HttpExample {
  transport: 'http'
  endpoint: EndpointRecord
  id: string
  description: string | undefined
  /** The recorded call, wire-named. */
  request: Record<string, unknown>
  status: number
  /** The recorded response body. */
  response: unknown
}

export interface WsExample {
  transport: 'ws'
  endpoint: EndpointRecord
  id: string
  description: string | undefined
  /** The recorded call's (or subscription's) parameters, wire-named. */
  parameters: Record<string, unknown>
  /** The recorded outgoing frame, when there is one. */
  payload: Record<string, unknown> | undefined
  /** The recorded reply frame, when there is one. */
  reply: unknown
  /** Recorded pushed messages, decoded. */
  messages: unknown[]
  /** Whether a protobuf frames sidecar was recorded. */
  binaryFrames: boolean
  /**
   * The sidecar's binary pushes, decoded from base64 in recorded order: what the mock sends
   * in place of `messages` (ADR 0016). Empty when none was recorded.
   */
  frames: Uint8Array[]
  /** Synthesized from an HTTP recording of a dual-transport `rpc` endpoint. */
  synthesized: boolean
}

export interface DiscoveryOptions {
  /** The spec directory relative to the project root; `spec` by default. */
  specDir?: string
}

function readJson(file: string): unknown {
  // Lossless, as the client's own core parses a reply: an unsafe integer stays exact (a `bigint`).
  return parseJsonText(readFileSync(file, 'utf8'))
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Every endpoint of the project, sorted by function path. */
export function endpointRecords(projectRoot: string, options: DiscoveryOptions = {}): EndpointRecord[] {
  const root = path.join(projectRoot, options.specDir ?? 'spec', 'endpoints')
  const out: EndpointRecord[] = []
  const walk = (dir: string, segments: string[]) => {
    const file = path.join(dir, 'endpoint.json')
    if (existsSync(file)) {
      const raw = readJson(file) as {
        function?: string
        surface?: Surface
        spec?: { kind?: string; transports?: Transport[]; path?: string }
      }
      const kind = (raw.spec?.kind ?? 'rpc') as EndpointRecord['kind']
      const transports = kind === 'stream' ? ['ws' as const] : kind === 'grpc' ? [] : (raw.spec?.transports ?? [])
      out.push({
        function: raw.function ?? segments.join('.'),
        dir,
        kind,
        transports,
        path: raw.spec?.path,
        surface: raw.surface,
        dual: kind === 'rpc' && transports.includes('http') && transports.includes('ws'),
      })
    }
    if (!existsSync(dir)) return
    const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))
    for (const entry of entries) {
      if (entry.isDirectory() && entry.name !== 'examples') walk(path.join(dir, entry.name), [...segments, entry.name])
    }
  }
  walk(root, [])
  return out.sort((a, b) => a.function.localeCompare(b.function))
}

/** `<id>` -> file for every `<id><suffix>` in the endpoint's `examples/`. */
function fileMap(dir: string, suffix: string): Map<string, string> {
  const examples = path.join(dir, 'examples')
  const out = new Map<string, string>()
  if (!existsSync(examples)) return out
  for (const name of readdirSync(examples).sort()) {
    if (name.endsWith(suffix)) out.set(name.slice(0, -suffix.length), path.join(examples, name))
  }
  return out
}

/** The complete HTTP examples of one endpoint. */
export function endpointHttpExamples(endpoint: EndpointRecord): HttpExample[] {
  if (!endpoint.transports.includes('http')) return []
  const requests = fileMap(endpoint.dir, '.request.json')
  const responses = fileMap(endpoint.dir, '.response.json')
  const out: HttpExample[] = []
  for (const [id, file] of [...requests].sort(([a], [b]) => a.localeCompare(b))) {
    const responseFile = responses.get(id)
    if (responseFile === undefined) continue
    const recorded = readJson(file) as { description?: string; request?: Record<string, unknown>; parameters?: Record<string, unknown> }
    const response = readJson(responseFile) as { status?: number; payload?: unknown }
    out.push({
      transport: 'http',
      endpoint,
      id,
      description: recorded.description,
      request: recorded.request ?? recorded.parameters ?? {},
      status: response.status ?? 200,
      response: response.payload,
    })
  }
  return out
}

/** The complete WebSocket examples of one endpoint, synthesized ones included. */
export function endpointWsExamples(endpoint: EndpointRecord): WsExample[] {
  if (!endpoint.transports.includes('ws')) return []
  const parameters = fileMap(endpoint.dir, '.parameters.json')
  const replies = fileMap(endpoint.dir, '.reply.json')
  const messages = new Map([...fileMap(endpoint.dir, '.messages.json'), ...fileMap(endpoint.dir, '.message.json')])
  const frames = fileMap(endpoint.dir, '.messages.protobuf.json')
  const out: WsExample[] = []
  for (const [id, file] of parameters) {
    if (!replies.has(id) && !messages.has(id) && !frames.has(id)) continue
    const raw = readJson(file)
    const wrapped = isObject(raw) && ('description' in raw || 'parameters' in raw || 'payload' in raw)
    const recorded = wrapped ? (raw as { description?: string; parameters?: Record<string, unknown>; payload?: Record<string, unknown> }) : { parameters: isObject(raw) ? raw : {} }
    const pushed = messages.has(id) ? readJson(messages.get(id)!) : []
    out.push({
      transport: 'ws',
      endpoint,
      id,
      description: recorded.description,
      parameters: recorded.parameters ?? {},
      payload: recorded.payload,
      reply: replies.has(id) ? readJson(replies.get(id)!) : undefined,
      messages: Array.isArray(pushed) ? pushed : [pushed],
      binaryFrames: frames.has(id),
      frames: frames.has(id) ? binaryFrames(frames.get(id)!) : [],
      synthesized: false,
    })
  }
  if (endpoint.dual) {
    const native = new Set(out.map(example => example.id))
    for (const http of endpointHttpExamples(endpoint)) {
      if (!native.has(http.id)) out.push(synthesizeWsExample(http))
    }
  }
  return out.sort((a, b) => a.id.localeCompare(b.id))
}

/**
 * The frames of a protobuf sidecar: a list of `{content_type, encoding, data}` entries, a
 * `{"frames": [...]}` envelope of them, or one bare entry, each base64-decoded.
 */
export function binaryFrames(file: string): Uint8Array[] {
  const raw = readJson(file)
  const entries = Array.isArray(raw) ? raw : isObject(raw) && Array.isArray(raw.frames) ? raw.frames : [raw]
  return entries.map((entry, index) => {
    const { encoding = 'base64', data } = entry as { encoding?: string; data?: unknown }
    if (encoding !== 'base64' || typeof data !== 'string') throw new Error(`${file}: frame ${index} is not a base64 \`data\` string`)
    return new Uint8Array(Buffer.from(data, 'base64'))
  })
}

/** A WebSocket example for a dual-transport `rpc` endpoint, from one of its HTTP recordings. */
export function synthesizeWsExample(http: HttpExample): WsExample {
  return {
    transport: 'ws',
    endpoint: http.endpoint,
    id: http.id,
    description: http.description,
    parameters: http.request,
    payload: { jsonrpc: '2.0', id: 0, method: http.endpoint.path, params: http.request },
    reply: http.response,
    messages: [],
    binaryFrames: false,
    frames: [],
    synthesized: true,
  }
}

/** Every complete HTTP example of the project, in function-path then id order. */
export function httpExamples(projectRoot: string, options: DiscoveryOptions = {}): HttpExample[] {
  return endpointRecords(projectRoot, options).flatMap(endpointHttpExamples)
}

/** Every complete WebSocket example of the project, in function-path then id order. */
export function wsExamples(projectRoot: string, options: DiscoveryOptions = {}): WsExample[] {
  return endpointRecords(projectRoot, options).flatMap(endpointWsExamples)
}
