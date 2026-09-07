/**
 * Codecs: the runtime half of a generated type.
 *
 * A generated package emits a plain `interface` per wire shape and, beside it, a codec
 * built from the combinators here (`const Issue: Codec<Issue> = t.object({...})`), so
 * `tsc` proves the codec matches the interface. A codec `parse`s a decoded wire JSON value
 * into the typed value (decimal strings branded, epoch timestamps turned into `Date`s,
 * shapes checked) and `dump`s the typed value back to the wire form. Objects keep
 * undocumented keys, unions try their variants in order, and every failure is a
 * `ValidationError` naming the offending path as a JSON pointer.
 *
 * There is no schema interpretation and no `eval`: a codec is an object with two
 * functions, and this module is the whole validator.
 */
import { Decimal, isDecimal } from './decimal.js'
import { ValidationError, type Issue } from './errors.js'
import {
  DateIso, dateIso, timestampIso, timestampMicros, timestampMillis, timestampNanos, timestampSeconds,
  type TimestampIso, type TimestampMicros, type TimestampMillis, type TimestampNanos, type TimestampSeconds,
} from './times.js'

export interface Codec<T> {
  /** Parse a decoded wire value at `path` (a JSON pointer, `''` at the root) into a `T`. */
  parse(value: unknown, path?: string): T
  /** Render a `T` back to its wire value, checking it is a `T` on the way. */
  readonly dump: (value: T, path?: string) => unknown
}

/** The typed value a codec produces. */
export type Infer<C> = C extends Codec<infer T> ? T : never

/** A codec marked optional inside `object`: its key may be absent. */
export interface OptionalCodec<T> extends Codec<T> {
  readonly optional: true
}

// `any` on purpose: `Codec<T>` is invariant in `T` (its `dump` takes a `T`), so a bound of
// `Codec<unknown>` would reject every concrete codec.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyCodec = Codec<any>
type Shape = Record<string, AnyCodec>
type OptionalKeys<S extends Shape> = { [K in keyof S]: S[K] extends { readonly optional: true } ? K : never }[keyof S]
type RequiredKeys<S extends Shape> = Exclude<keyof S, OptionalKeys<S>>
type Simplify<T> = { [K in keyof T]: T[K] } & {}
/** The object type an `object(shape)` codec parses to. */
export type InferObject<S extends Shape> = Simplify<
  { [K in RequiredKeys<S>]: Infer<S[K]> } & { [K in OptionalKeys<S>]?: Infer<S[K]> }
>

/** A failed check at `path`. */
export function fail(path: string, message: string, issues?: readonly Issue[]): never {
  throw new ValidationError(`${message} at ${path || '/'}`, { issues: issues ?? [{ path, message }] })
}

function describe(value: unknown): string {
  if (value === null) return 'null'
  if (Array.isArray(value)) return 'array'
  if (typeof value === 'string') return JSON.stringify(value.length > 40 ? value.slice(0, 40) + '…' : value)
  if (typeof value === 'object') return value instanceof Date ? 'Date' : 'object'
  return typeof value === 'number' || typeof value === 'boolean' ? String(value) : typeof value
}

function expected(path: string, what: string, value: unknown): never {
  return fail(path, `expected ${what}, got ${describe(value)}`)
}

/** A codec whose `parse` and `dump` are the same check, for values that are themselves JSON. */
function scalar<T>(what: string, is: (value: unknown) => value is T): Codec<T> {
  const check = (value: unknown, path = ''): T => (is(value) ? value : expected(path, what, value))
  return { parse: check, dump: check }
}

/**
 * A codec over a wire codec: `parse` reads the wire value with `wire` then applies
 * `decode`; `dump` applies `encode` then checks the result with `wire`. Any exception
 * `decode`/`encode` throw becomes a `ValidationError` at the path. This is how the
 * timestamp codecs are built, and how a project adds a wire format of its own.
 */
export function wire<W, T>(
  wire: Codec<W>, what: string, decode: (value: W) => T, encode: (value: T) => W, isTyped: (value: unknown) => value is T,
): Codec<T> {
  return {
    parse(value, path = '') {
      const w = wire.parse(value, path)
      try { return decode(w) } catch (e) { return fail(path, `expected ${what}, got ${describe(value)}: ${(e as Error).message}`) }
    },
    dump(value, path = '') {
      if (!isTyped(value)) expected(path, what, value)
      let w: W
      try { w = encode(value) } catch (e) { return fail(path, `cannot dump ${what}: ${(e as Error).message}`) }
      return wire.dump(w, path)
    },
  }
}

const isString = (v: unknown): v is string => typeof v === 'string'
const isNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const isBoolean = (v: unknown): v is boolean => typeof v === 'boolean'
const isNull = (v: unknown): v is null => v === null
const isDate = (v: unknown): v is Date => v instanceof Date && !Number.isNaN(v.getTime())
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

/** Any JSON value, passed through untouched. */
export const unknown: Codec<unknown> = { parse: v => v, dump: v => v }
export const string: Codec<string> = scalar('string', isString)
/** A finite JSON number. */
export const number: Codec<number> = scalar('number', isNumber)
/** A JSON integer within `Number.MAX_SAFE_INTEGER`. */
export const integer: Codec<number> = scalar('integer', (v): v is number => Number.isSafeInteger(v))
export const boolean: Codec<boolean> = scalar('boolean', isBoolean)
const nul: Codec<null> = scalar('null', isNull)
export { nul as null }

/** `decimal-string`: the digits the wire carried, branded `Decimal`. */
export const decimal: Codec<Decimal> = scalar('decimal string', isDecimal)
/** `integer-string`: `"42"` on the wire, `42` in the client. */
export const integerString: Codec<number> = wire(
  string, 'integer string',
  s => { if (!/^[+-]?\d+$/.test(s)) throw new Error('not an integer'); const n = Number(s); if (!Number.isSafeInteger(n)) throw new Error('beyond safe integer range'); return n },
  n => String(n), (v): v is number => Number.isSafeInteger(v),
)
/** `boolean-string`: `"true"`/`"false"` on the wire, a `boolean` in the client. */
export const booleanString: Codec<boolean> = wire(
  string, 'boolean string',
  s => { if (s === 'true') return true; if (s === 'false') return false; throw new Error('not "true" or "false"') },
  b => String(b), isBoolean,
)

const epochWire: Codec<number | string> = scalar('epoch timestamp', (v): v is number | string =>
  (typeof v === 'number' && Number.isFinite(v)) || (typeof v === 'string' && /^[+-]?\d+$/.test(v)))
const epoch = (what: string, conv: typeof timestampMillis): Codec<Date> =>
  wire(epochWire, what, v => conv.parse(v), d => conv.dump(d), isDate)

export const epochSeconds: Codec<TimestampSeconds> = epoch('epoch seconds', timestampSeconds)
export const epochMillis: Codec<TimestampMillis> = epoch('epoch milliseconds', timestampMillis)
export const epochMicros: Codec<TimestampMicros> = epoch('epoch microseconds', timestampMicros)
export const epochNanos: Codec<TimestampNanos> = epoch('epoch nanoseconds', timestampNanos)
/** `date-time`: an RFC 3339 string on the wire, a `Date` in the client. */
export const dateTime: Codec<TimestampIso> = wire(string, 'RFC 3339 date-time', s => timestampIso.parse(s), d => timestampIso.dump(d), isDate)
/** `date`: an RFC 3339 full-date, kept as a branded `DateIso` string. */
export const date: Codec<DateIso> = wire(string, 'RFC 3339 date', s => dateIso.parse(s), d => dateIso.dump(d), DateIso.is)

/** Exactly one of `values` (a `const` enum on the wire). */
export function literal<const V extends readonly (string | number | boolean | null)[]>(...values: V): Codec<V[number]> {
  const what = values.map(v => JSON.stringify(v)).join(' | ')
  return scalar(what, (v): v is V[number] => values.includes(v as V[number]))
}
export { literal as enum }

/** Mark a key of `object` as optional. */
export function optional<T>(codec: Codec<T>): OptionalCodec<T> {
  return { ...codec, optional: true }
}

/** `T | null`. */
export function nullable<T>(codec: Codec<T>): Codec<T | null> {
  return union(nul, codec)
}

/**
 * An object with the keys of `shape`, each parsed by its codec. Keys not in `shape` are
 * kept as they came (both ways), so an undocumented field never breaks a client.
 */
export function object<S extends Shape>(shape: S): Codec<InferObject<S>> {
  const keys = Object.keys(shape)
  const run = (value: unknown, path: string, way: 'parse' | 'dump'): InferObject<S> => {
    if (!isObject(value)) expected(path, 'object', value)
    const out: Record<string, unknown> = { ...value }
    for (const key of keys) {
      const codec = shape[key]! as AnyCodec
      const at = `${path}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`
      if (value[key] === undefined) {
        if (!(codec as OptionalCodec<unknown>).optional) fail(at, 'missing required key')
        delete out[key]
      } else {
        out[key] = codec[way](value[key], at)
      }
    }
    return out as InferObject<S>
  }
  return { parse: (v, p = '') => run(v, p, 'parse'), dump: (v, p = '') => run(v, p, 'dump') }
}

/** A map with arbitrary string keys (`additionalProperties`), every value parsed by `value`. */
export function record<T>(value: Codec<T>): Codec<Record<string, T>> {
  const run = (v: unknown, path: string, way: 'parse' | 'dump'): Record<string, T> => {
    if (!isObject(v)) expected(path, 'object', v)
    const out: Record<string, T> = {}
    const codec = value as AnyCodec
    for (const key of Object.keys(v)) out[key] = codec[way](v[key], `${path}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`) as T
    return out
  }
  return { parse: (v, p = '') => run(v, p, 'parse'), dump: (v, p = '') => run(v, p, 'dump') }
}

export function array<T>(item: Codec<T>): Codec<T[]> {
  const run = (v: unknown, path: string, way: 'parse' | 'dump'): T[] => {
    if (!Array.isArray(v)) expected(path, 'array', v)
    const codec = item as AnyCodec
    return v.map((x, i) => codec[way](x, `${path}/${i}`) as T)
  }
  return { parse: (v, p = '') => run(v, p, 'parse'), dump: (v, p = '') => run(v, p, 'dump') }
}

type InferTuple<C extends readonly AnyCodec[]> = { [K in keyof C]: Infer<C[K]> }

/**
 * A fixed-shape array (`prefixItems`): `items[i]` parses position `i`. With `rest`, further
 * positions are parsed by it; without, the length must match exactly.
 */
export function tuple<const C extends readonly AnyCodec[]>(items: C): Codec<InferTuple<C>>
export function tuple<const C extends readonly AnyCodec[], R>(items: C, rest: Codec<R>): Codec<[...InferTuple<C>, ...R[]]>
export function tuple(items: readonly AnyCodec[], rest?: AnyCodec): Codec<unknown[]> {
  const run = (v: unknown, path: string, way: 'parse' | 'dump'): unknown[] => {
    if (!Array.isArray(v)) expected(path, 'array', v)
    if (v.length < items.length || (!rest && v.length > items.length)) {
      fail(path, `expected ${rest ? 'at least ' : ''}${items.length} items, got ${v.length}`)
    }
    return v.map((x, i) => (items[i] ?? rest!)[way](x, `${path}/${i}`))
  }
  return { parse: (v, p = '') => run(v, p, 'parse'), dump: (v, p = '') => run(v, p, 'dump') }
}

/** `anyOf`: the first variant that accepts the value wins, in the order given. */
export function union<const C extends readonly AnyCodec[]>(...variants: C): Codec<Infer<C[number]>> {
  const run = (v: unknown, path: string, way: 'parse' | 'dump'): Infer<C[number]> => {
    const issues: Issue[] = []
    for (const variant of variants) {
      try { return variant[way](v, path) as Infer<C[number]> } catch (e) {
        if (!(e instanceof ValidationError)) throw e
        issues.push(...e.issues)
      }
    }
    return fail(path, `no variant matched ${describe(v)}`, [{ path, message: 'no variant matched' }, ...issues])
  }
  return { parse: (v, p = '') => run(v, p, 'parse'), dump: (v, p = '') => run(v, p, 'dump') }
}

/** A codec resolved on first use, for recursive shapes. */
export function lazy<T>(get: () => Codec<T>): Codec<T> {
  let codec: Codec<T> | undefined
  return {
    parse: (v, p) => (codec ??= get()).parse(v, p),
    dump: (v, p) => (codec ??= get()).dump(v, p),
  }
}

/** Parse a raw JSON document; a syntax error is a `ValidationError` too, with the `SyntaxError` as `cause`. */
export function parseJson<T>(codec: Codec<T>, text: string): T {
  let value: unknown
  try { value = JSON.parse(text) } catch (e) {
    throw new ValidationError(`invalid JSON: ${(e as Error).message}`, { cause: e })
  }
  return codec.parse(value)
}

/** Dump a typed value to a JSON document. */
export function dumpJson<T>(codec: Codec<T>, value: T): string {
  return JSON.stringify(codec.dump(value))
}
