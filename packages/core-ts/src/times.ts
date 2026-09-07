/**
 * Converters between a wire timestamp and a real `Date`, one per shape an API puts on the
 * wire: epoch seconds/millis/micros/nanos, RFC 3339 date-times, and plain calendar dates.
 *
 * Timestamps are `Date`s behind the aliases below, so a later move to `Temporal.Instant` is
 * one alias change. A `Date` holds milliseconds: `epoch-micros`/`epoch-nanos` and
 * sub-millisecond RFC 3339 fractions lose their extra digits on parse, but the arithmetic
 * is exact integer (`BigInt`) arithmetic throughout, so the digits that survive are the
 * right ones and a millisecond-precision value round-trips through every unit exactly.
 */
import { LogicError } from './errors.js'

/** An `epoch-seconds` field. */
export type TimestampSeconds = Date
/** An `epoch-millis` field. */
export type TimestampMillis = Date
/** An `epoch-micros` field. */
export type TimestampMicros = Date
/** An `epoch-nanos` field. */
export type TimestampNanos = Date
/** A `date-time` (RFC 3339) field. */
export type TimestampIso = Date

declare const dateIsoBrand: unique symbol

/** A `date` (RFC 3339 full-date) field: `'YYYY-MM-DD'`, since `Date` has no date-only value. */
export type DateIso = string & { readonly [dateIsoBrand]: true }

/** Parse `value` in wire form into a `Date`, or `dump` a `Date` back to wire form. */
export interface TimeConverter<W> {
  parse(value: W): Date
  dump(date: Date): W
  now(): W
}

/** Floor division on `BigInt`s (`/` truncates toward zero). */
function floorDiv(n: bigint, d: bigint): bigint {
  const q = n / d
  return n % d < 0n ? q - 1n : q
}

function toBigInt(value: number | string | bigint, what: string): bigint {
  if (typeof value === 'bigint') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new LogicError(`Not an epoch ${what}: ${value}`)
    return BigInt(Math.trunc(value))
  }
  if (!/^[+-]?\d+$/.test(value.trim())) throw new LogicError(`Not an epoch ${what}: ${JSON.stringify(value)}`)
  return BigInt(value.trim())
}

/** Converter for epoch timestamps in a specific unit. */
export class EpochConverter implements TimeConverter<number> {
  /** Units per second: `1000n` for milliseconds, `1n` for seconds, ... */
  readonly unit: bigint

  constructor(unit: bigint | number) {
    this.unit = BigInt(unit)
  }

  static seconds(): EpochConverter { return new EpochConverter(1n) }
  static milliseconds(): EpochConverter { return new EpochConverter(1_000n) }
  static microseconds(): EpochConverter { return new EpochConverter(1_000_000n) }
  static nanoseconds(): EpochConverter { return new EpochConverter(1_000_000_000n) }

  /**
   * Parse an epoch timestamp. Some APIs serialize it as a numeral string rather than a bare
   * number (`"timestamp": "1786302600000"`); a string is read exactly, so a nanosecond
   * value beyond `Number.MAX_SAFE_INTEGER` keeps its millisecond digits intact.
   */
  parse(value: number | string | bigint): Date {
    const millis = floorDiv(toBigInt(value, 'timestamp') * 1_000n, this.unit)
    return new Date(Number(millis))
  }

  /**
   * Convert a `Date` back into an epoch timestamp in this unit. Integer arithmetic
   * throughout; the result is exact whenever it is a safe integer, and for nanoseconds
   * the nearest double, which `JSON.stringify` still prints with the original digits.
   */
  dump(date: Date): number {
    return Number(this.dumpBigInt(date))
  }

  /** `dump`, exact at any magnitude. */
  dumpBigInt(date: Date): bigint {
    const ms = date.getTime()
    if (Number.isNaN(ms)) throw new LogicError('Invalid Date')
    return floorDiv(BigInt(ms) * this.unit, 1_000n)
  }

  /** The current time, in this unit. */
  now(): number {
    return this.dump(new Date())
  }
}

const DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:([Zz])|([+-])(\d{2}):(\d{2}))$/

/** Converter for RFC 3339 date-times, always UTC and `Z`-suffixed on the wire. */
export class IsoConverter implements TimeConverter<string> {
  /**
   * Parse a `Z`-suffixed or offset RFC 3339 date-time with a fraction of any length
   * (some APIs send milliseconds, others nanoseconds); digits beyond milliseconds are
   * dropped, digits short of them padded.
   */
  parse(value: string): Date {
    const m = DATE_TIME.exec(value)
    if (!m) throw new LogicError(`Not an RFC 3339 date-time: ${JSON.stringify(value)}`)
    const [, y, mo, d, h, mi, s, frac, , sign, oh, om] = m
    const ms = frac ? Number((frac + '00').slice(0, 3)) : 0
    let utc = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s), ms)
    if (sign) utc -= (sign === '-' ? -1 : 1) * (Number(oh) * 60 + Number(om)) * 60_000
    const date = new Date(utc)
    if (date.getUTCMonth() !== Number(mo) - 1 || Number(d) > 31 || Number(h) > 23 || Number(mi) > 59 || Number(s) > 60) {
      throw new LogicError(`Not an RFC 3339 date-time: ${JSON.stringify(value)}`)
    }
    return date
  }

  /** Render a `Date` as UTC, `Z`-suffixed, with milliseconds only when they are non-zero. */
  dump(date: Date): string {
    if (Number.isNaN(date.getTime())) throw new LogicError('Invalid Date')
    return date.toISOString().replace('.000Z', 'Z')
  }

  now(): string {
    return this.dump(new Date())
  }
}

const DIRECTIVES: Record<string, [width: number, key: 'y' | 'm' | 'd']> = { Y: [4, 'y'], m: [2, 'm'], d: [2, 'd'] }

/**
 * Converter for a plain calendar date, with no time component: wire string in `pattern`
 * to a `DateIso` (`'YYYY-MM-DD'`) and back. Not a `TimeConverter`: a calendar date has no
 * instant to round-trip through a `Date`.
 */
export class DateConverter {
  /**
   * `strftime`-style directives for the wire string: `%Y`, `%m`, `%d`, anything else
   * literal. `'%Y%m%d'` reads a compact `YYYYMMDD` date with no separators. Defaults to
   * RFC 3339's `%Y-%m-%d`.
   */
  readonly pattern: string
  private readonly regex: RegExp
  private readonly order: ('y' | 'm' | 'd')[] = []

  constructor(options: { pattern?: string } = {}) {
    this.pattern = options.pattern ?? '%Y-%m-%d'
    let source = ''
    for (let i = 0; i < this.pattern.length; i++) {
      const c = this.pattern[i]!
      const spec = c === '%' ? DIRECTIVES[this.pattern[i + 1] ?? ''] : undefined
      if (spec) { source += `(\\d{${spec[0]}})`; this.order.push(spec[1]); i++ }
      else source += c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    }
    this.regex = new RegExp(`^${source}$`)
  }

  /** Parse a wire date (e.g. `'2026-08-03'` for the default pattern) into a `DateIso`. */
  parse(value: string): DateIso {
    const m = this.regex.exec(value)
    if (!m) throw new LogicError(`Not a date in ${this.pattern} form: ${JSON.stringify(value)}`)
    const parts: Record<'y' | 'm' | 'd', string> = { y: '1970', m: '01', d: '01' }
    this.order.forEach((key, i) => { parts[key] = m[i + 1]! })
    return DateIso.of(`${parts.y}-${parts.m}-${parts.d}`)
  }

  /** Render a `DateIso` back to the wire pattern. */
  dump(date: DateIso): string {
    const [y, m, d] = DateIso.of(date).split('-') as [string, string, string]
    return this.pattern.replace(/%[Ymd]/g, s => (s === '%Y' ? y : s === '%m' ? m : d)).replace(/%%/g, '%')
  }

  /** Today's date, in UTC. */
  now(): DateIso {
    return DateIso.fromDate(new Date())
  }
}

const DATE = /^(\d{4})-(\d{2})-(\d{2})$/

export const DateIso = {
  /** Whether `value` is a valid `'YYYY-MM-DD'` calendar date. */
  is(value: unknown): value is DateIso {
    if (typeof value !== 'string') return false
    const m = DATE.exec(value)
    if (!m) return false
    const date = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
    return date.getUTCMonth() === Number(m[2]) - 1 && date.getUTCDate() === Number(m[3])
  },
  /** Brand a `'YYYY-MM-DD'` string, throwing `LogicError` when it is not a calendar date. */
  of(value: string): DateIso {
    if (!DateIso.is(value)) throw new LogicError(`Not a calendar date: ${JSON.stringify(value)}`)
    return value
  },
  /** The UTC calendar date of an instant. */
  fromDate(date: Date): DateIso {
    return date.toISOString().slice(0, 10) as DateIso
  },
  /** Midnight UTC on that date. */
  toDate(date: DateIso): Date {
    return new Date(`${date}T00:00:00Z`)
  },
}

export const timestampSeconds: EpochConverter = EpochConverter.seconds()
export const timestampMillis: EpochConverter = EpochConverter.milliseconds()
export const timestampMicros: EpochConverter = EpochConverter.microseconds()
export const timestampNanos: EpochConverter = EpochConverter.nanoseconds()
export const timestampIso: IsoConverter = new IsoConverter()
export const dateIso: DateConverter = new DateConverter()
