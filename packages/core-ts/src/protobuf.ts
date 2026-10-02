/**
 * Protobuf-framed WebSocket pushes (ADR 0016): decode a binary frame against the
 * project's `spec/proto/*.proto` sources and read it as its canonical ProtoJSON value.
 *
 * The sources travel with the generated package (`proto.ts`, `PROTO_SOURCES`) and are
 * compiled at run time, so a core needs no build step:
 *
 * ```ts
 * import { PROTO_SOURCES } from '../proto.js'
 * const frames = ProtoFrames.compile(PROTO_SOURCES, 'PushDataV3ApiWrapper')
 *
 * parseMsg(msg: Data) {
 *   if (!isBinary(msg)) return null            // a JSON ack, handled elsewhere
 *   const frame = frames.decode(msg)
 *   return { channel: frame.string('channel')!, notification: frame }
 * }
 * // per endpoint: narrow to `meta.proto_field`
 * subscription.filter(f => f.has(meta.proto_field)).map(f => f.field(meta.proto_field))
 * ```
 *
 * Every value handed out is ProtoJSON (lowerCamelCase `json_name` keys, 64-bit integers as
 * strings, bytes as base64, enums by name, unset fields left out): the one rendering every
 * protobuf library agrees on, so the TypeScript, Go and Rust cores decode a recorded frame
 * to the same value. Fields are addressed by their `.proto` name (`public_aggre_deals`),
 * the name `meta.proto_field` carries.
 */
import protobuf from 'protobufjs'
import { LogicError, ValidationError } from './errors.js'
import type { Data } from './ws/socket.js'

/** `.proto` file name -> source text: what a generated `proto.ts` exports as `PROTO_SOURCES`. */
export type ProtoSources = Readonly<Record<string, string>>

/** A ProtoJSON object. */
export type ProtoJson = { [key: string]: unknown }

/** Whether a received frame is binary: an `ArrayBuffer` or a byte view. */
export function isBinary(data: Data | Uint8Array): data is ArrayBuffer | Uint8Array {
  return typeof data !== 'string'
}

/** The ProtoJSON name of a field: protoc's lowerCamelCase of its `.proto` name. */
export function jsonName(protoName: string): string {
  let out = ''
  let upper = false
  for (const ch of protoName) {
    if (ch === '_') { upper = true; continue }
    out += upper ? ch.toUpperCase() : ch
    upper = false
  }
  return out
}

/** One message type compiled from the project's sources: the frame envelope. */
export class ProtoFrames {
  private constructor(readonly root: protobuf.Root, readonly type: protobuf.Type) {}

  /**
   * Compile `sources` (every file of `spec/proto/`, parsed into one root, so imports
   * between them resolve by type name) and look up `message`, the envelope every binary
   * frame decodes as. A `LogicError` when the sources do not parse or name no such message.
   */
  static compile(sources: ProtoSources, message: string): ProtoFrames {
    const root = new protobuf.Root()
    for (const name of Object.keys(sources).sort()) {
      try {
        protobuf.parse(sources[name]!, root, { keepCase: true })
      } catch (e) {
        throw new LogicError(`${name}: does not parse as protobuf: ${(e as Error).message}`, { cause: e })
      }
    }
    try {
      root.resolveAll()
      return new ProtoFrames(root, root.lookupType(message))
    } catch (e) {
      throw new LogicError(`protobuf sources: ${(e as Error).message}`, { cause: e })
    }
  }

  /** Decode one frame; a `ValidationError` for a text frame or bytes that are not this message. */
  decode(data: Data | Uint8Array): Frame {
    if (!isBinary(data)) throw new ValidationError(`expected a binary ${this.type.name} frame, received a text frame`)
    const bytes = data instanceof Uint8Array ? data : new Uint8Array(data)
    try {
      return new Frame(this.type, this.type.decode(bytes) as unknown as Record<string, unknown>)
    } catch (e) {
      throw new ValidationError(`the frame does not decode as ${this.type.name}: ${(e as Error).message}`, { cause: e })
    }
  }
}

/** One decoded frame. */
export class Frame {
  constructor(readonly type: protobuf.Type, readonly message: Record<string, unknown>) {}

  #field(name: string): protobuf.Field {
    const field = this.type.fields[name]
    if (field === undefined) throw new LogicError(`${this.type.name} has no field "${name}"`)
    return field
  }

  /** Whether the field (by `.proto` name) is set: a oneof member chosen, a repeated field non-empty. */
  has(name: string): boolean {
    return present(this.#field(name), this.message)
  }

  /** The field's ProtoJSON value, `undefined` when it is not set. */
  field(name: string): unknown {
    const field = this.#field(name)
    return present(field, this.message) ? fieldJson(field, this.message[field.name]) : undefined
  }

  /** A string field's value, `undefined` when unset or not a string. */
  string(name: string): string | undefined {
    const value = this.message[this.#field(name).name]
    return typeof value === 'string' && this.has(name) ? value : undefined
  }

  /** The `.proto` name of the member set in `oneof`, `undefined` when none is. */
  oneofCase(oneof: string): string | undefined {
    const group = this.type.oneofs?.[oneof]
    if (group === undefined) throw new LogicError(`${this.type.name} has no oneof "${oneof}"`)
    return group.fieldsArray.find(field => present(field, this.message))?.name
  }

  /** The whole frame as ProtoJSON. */
  json(): ProtoJson {
    return messageJson(this.type, this.message)
  }
}

const LONG_TYPES = new Set(['int64', 'uint64', 'sint64', 'fixed64', 'sfixed64'])

function isDefault(value: unknown): boolean {
  if (value === '' || value === 0 || value === false || value == null) return true
  if (value instanceof Uint8Array) return value.length === 0
  if (typeof value === 'object' && typeof (value as { isZero?: unknown }).isZero === 'function') {
    return (value as { isZero(): boolean }).isZero()
  }
  return false
}

function present(field: protobuf.Field, message: Record<string, unknown>): boolean {
  const value = message[field.name]
  if (field.repeated) return Array.isArray(value) && value.length > 0
  if (field.map) return typeof value === 'object' && value !== null && Object.keys(value).length > 0
  if (!Object.prototype.hasOwnProperty.call(message, field.name) || value == null) return false
  // `hasPresence` is the field's `partOf` oneof (an object) for a oneof member: coerce it.
  return Boolean(field.hasPresence) || !isDefault(value)
}

function messageJson(type: protobuf.Type, message: Record<string, unknown>): ProtoJson {
  const out: ProtoJson = {}
  for (const field of type.fieldsArray) {
    if (!present(field, message)) continue
    const key = (field.options?.json_name as string | undefined) ?? jsonName(field.name)
    out[key] = fieldJson(field, message[field.name])
  }
  return out
}

function fieldJson(field: protobuf.Field, value: unknown): unknown {
  if (field.map) {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, scalarJson(field, v)]))
  }
  if (field.repeated) return (value as unknown[]).map(item => scalarJson(field, item))
  return scalarJson(field, value)
}

function scalarJson(field: protobuf.Field, value: unknown): unknown {
  const resolved = field.resolvedType
  if (resolved instanceof protobuf.Type) return messageJson(resolved, value as Record<string, unknown>)
  if (resolved instanceof protobuf.Enum) return resolved.valuesById[value as number] ?? value
  if (LONG_TYPES.has(field.type)) return String(value)
  if (field.type === 'bytes') return base64(value as Uint8Array)
  if ((field.type === 'double' || field.type === 'float') && typeof value === 'number' && !Number.isFinite(value)) {
    return Number.isNaN(value) ? 'NaN' : value > 0 ? 'Infinity' : '-Infinity'
  }
  return value
}

function base64(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}
