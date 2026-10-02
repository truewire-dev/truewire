/**
 * The `seek` walk (ADR 0013): pagination by a bound read off the rows of the previous page.
 *
 * The walk moves the bound the venue anchors truncation to (`start` walking forwards,
 * `end` walking backwards) to the extreme cursor key it has seen, and re-requests. Rows
 * sharing that key are carried into the next state and dropped when the venue serves them
 * again: by key when the cursor field is unique per row, by whole-row content otherwise
 * (a carried row missing from the next page raises). A request always spans from the
 * moving bound to the caller's far bound, or to the span edge when the venue refuses wide
 * ranges, so nothing is fetched outside the caller's own range.
 *
 * - A full page (`cap` known and reached) means more rows inside the range: move on. A
 *   full page whose rows all share one key cannot be advanced past and raises `LogicError`.
 * - With no cap known, a short page cannot be told from a full one, so the walk keeps
 *   moving while the extreme key makes progress.
 * - Otherwise the range is exhausted: the walk ends, or with a `span` advances to the next
 *   chunk until the edge reaches the far bound.
 * - With `until` (a far bound the venue refuses alongside the moving one, `exclusive.far`), a
 *   page holding a row past the caller's value ends the walk, and every such row is dropped.
 *
 * `next` is pure in its state `[pos, carried]`, so a page can be retried and a walk resumed
 * (`PaginatedResponse`'s contract). `truewire generate typescript` renders a `<method>Paged`
 * as one call to `seek`.
 */
import { LogicError } from './errors.js'
import { epochNanoseconds, fromEpochNanoseconds } from './times.js'
import { PaginatedResponse } from './paging.js'

/** A value a seek bound takes: an id or height (`number`), a string id, or a time (`Date`). */
export type SeekKey = number | bigint | string | Date

/** How a raw row field becomes a key comparable with a bound; see `SeekOptions.keys`. */
export type SeekKeys = 'number' | 'bigint' | 'string' | { parse(value: never): Date }

/** A `seek` walk's state: the moving bound's value, and the rows already yielded that share it. */
export type SeekState<Row, Key extends SeekKey> = [pos: Key | undefined, carried: Row[]]

export interface SeekOptions<Row, Key extends SeekKey> {
  /** The walking method's name, for error messages. */
  method: string
  /** The cursor field relative to one row (`[0]`, `properties.timestamp`; `''` for the row itself), for error messages. */
  field: string
  /** One row's raw cursor field. */
  read: (row: Row) => unknown
  /**
   * How a raw field becomes a key comparable with the bound: `'number'` (a numeral string
   * compares numerically), `'bigint'` (the same, for an `integer-string` bound, every digit kept), `'string'` (equality only; a page's last row in wire order
   * stands in for its extreme), or a time converter: the row field's own, so a raw epoch
   * or ISO value, as a `validate: false` row carries it, is parsed in the row's format
   * (epoch seconds under a millisecond bound, say). The bound's format is only the
   * request's concern.
   */
  keys: SeekKeys
  /** Whether the cursor field is unique per row: dedup by key, else by content. */
  unique: boolean
  /** Walking backwards: the moving bound is the range's end. */
  descending: boolean
  /** Rows a full page holds, when known. */
  cap: number | undefined
  /** The caller's far bound, when given. */
  far?: Key | undefined
  /** Widest range one request may cover, in key ticks (or `spanUnitMs` milliseconds per unit for a time). */
  span?: number | undefined
  /** Milliseconds per `span` unit, for a time bound. */
  spanUnitMs?: number
  /**
   * The caller's far bound on a parameter the venue refuses alongside the moving one
   * (`exclusive.far`), so the walk keeps it instead of sending it: a row whose `read` field
   * lies past `value` is dropped, and the walk ends on the page that held it.
   */
  until?: { read: (row: Row) => unknown; keys: SeekKeys; value: SeekKey | undefined } | undefined
  /** Fetch one page's rows, the moving bound at `pos` and, with a span, the far bound at `edge`. */
  fetch: (pos: Key | undefined, edge: Key | undefined) => Promise<Row[]>
}

/** Read a field off one row by path segments: `[0]` as `0`, `.id` as `'id'`; `undefined` when absent. */
export function rowField(row: unknown, path: readonly (string | number)[]): unknown {
  let value: unknown = row
  for (const segment of path) {
    if (value === null || value === undefined) return undefined
    if (typeof segment === 'number') {
      value = Array.isArray(value) ? value.at(segment) : undefined
    } else {
      value = typeof value === 'object' ? (value as Record<string, unknown>)[segment] : undefined
    }
  }
  return value
}

/** A raw field as a key; `undefined` when absent or not a key of that kind. */
function toKey(raw: unknown, keys: SeekKeys): SeekKey | undefined {
  if (raw === null || raw === undefined) return undefined
  if (keys === 'number') {
    const n = Number(raw)
    return Number.isNaN(n) ? undefined : n
  }
  if (keys === 'bigint') {
    if (typeof raw === 'bigint') return raw
    if ((typeof raw === 'number' && Number.isSafeInteger(raw)) || (typeof raw === 'string' && /^[+-]?\d+$/.test(raw))) return BigInt(raw)
    return undefined
  }
  if (keys === 'string') return String(raw)
  return raw instanceof Date ? raw : keys.parse(raw as never)
}

/** A key as a comparable value: a time as its exact epoch nanoseconds. */
function ordinal(key: SeekKey): number | bigint | string {
  return key instanceof Date ? epochNanoseconds(key) : key
}

function same(a: SeekKey | undefined, b: SeekKey | undefined): boolean {
  return a !== undefined && b !== undefined && ordinal(a) === ordinal(b)
}

/** Walk a `seek` pagination from the caller's own moving bound (`undefined`: the venue's default). */
export function seek<Row, Key extends SeekKey>(start: Key | undefined, options: SeekOptions<Row, Key>): PaginatedResponse<Row, SeekState<Row, Key>> {
  const { method, field, descending, cap, far, span, unique } = options
  const named = field === '' ? 'row' : `\`${field}\``
  if (span !== undefined && (start === undefined || far === undefined)) {
    throw new TypeError(`\`${method}\` walks a bounded range in spans: pass both bounds`)
  }

  const keyOf = (row: Row): Key | undefined => toKey(options.read(row), options.keys) as Key | undefined
  const until = options.until
  const past = (row: Row): boolean => {
    if (until?.value === undefined) return false
    const key = toKey(until.read(row), until.keys)
    if (key === undefined) return false
    return descending ? ordinal(key) < ordinal(until.value) : ordinal(key) > ordinal(until.value)
  }

  const shift = (pos: Key): Key => {
    if (typeof pos === 'bigint') {
      const moved = pos + (descending ? -BigInt(span!) : BigInt(span!))
      const bound = far as bigint
      return (descending ? (moved > bound ? moved : bound) : (moved < bound ? moved : bound)) as Key
    }
    if (pos instanceof Date) {
      // Exact nanoseconds, so a `PreciseDate` bound keeps its sub-millisecond digits.
      const width = BigInt(Math.round(span! * (options.spanUnitMs ?? 1) * 1_000_000))
      const moved = epochNanoseconds(pos) + (descending ? -width : width)
      const bound = epochNanoseconds(far as Date)
      return fromEpochNanoseconds(descending ? (moved > bound ? moved : bound) : (moved < bound ? moved : bound)) as Key
    }
    const width = span!
    const moved = (ordinal(pos) as number) + (descending ? -width : width)
    const bound = ordinal(far!) as number
    const edge = descending ? Math.max(moved, bound) : Math.min(moved, bound)
    return edge as Key
  }

  const next = async ([pos, carried]: SeekState<Row, Key>): Promise<[Row[], SeekState<Row, Key> | null]> => {
    const edge = span !== undefined ? shift(pos!) : undefined
    const rows = await options.fetch(pos, edge)
    const keys = rows.map(keyOf)

    let fresh: Row[]
    if (unique) {
      const carriedKeys = carried.map(keyOf)
      fresh = rows.filter((_, i) => !carriedKeys.some(key => same(key, keys[i])))
    } else {
      const remaining = carried.map(row => JSON.stringify(row))
      fresh = []
      for (const row of rows) {
        const index = remaining.indexOf(JSON.stringify(row))
        if (index >= 0) remaining.splice(index, 1)
        else fresh.push(row)
      }
      if (remaining.length > 0) {
        throw new LogicError(
          `\`${method}\` requested from ${String(pos)} and the venue no longer returned one or more rows it had ` +
          `already returned for that ${named}; row content was expected to stay available across requests, so ` +
          'the walk stopped instead of silently dropping or duplicating rows.',
        )
      }
    }

    if (rows.some(past)) return [fresh.filter(row => !past(row)), null]

    const values = keys.filter((key): key is Key => key !== undefined)
    let extreme: Key | undefined
    if (values.length > 0) {
      extreme = options.keys === 'string'
        ? values.at(-1)
        : values.reduce((a, b) => ((descending ? ordinal(b) < ordinal(a) : ordinal(b) > ordinal(a)) ? b : a))
    }
    const at = (value: Key): Row[] => rows.filter((_, i) => same(keys[i], value))

    if (cap !== undefined && rows.length >= cap) {
      if (extreme === undefined || same(extreme, pos)) {
        throw new LogicError(
          `\`${method}\` requested from ${String(pos)} and the venue returned a full page of ${rows.length} rows ` +
          `all sharing one ${named} value; the rest of that value is unreachable and advancing would drop it.`,
        )
      }
      return [fresh, [extreme, at(extreme)]]
    }
    if (cap === undefined && extreme !== undefined && !same(extreme, pos)) return [fresh, [extreme, at(extreme)]]
    if (edge === undefined) return [fresh, null]
    const done = descending ? ordinal(edge) <= ordinal(far!) : ordinal(edge) >= ordinal(far!)
    return done ? [fresh, null] : [fresh, [edge, at(edge)]]
  }

  return new PaginatedResponse<Row, SeekState<Row, Key>>([start, []], next)
}
