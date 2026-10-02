/**
 * Converters between a wire timestamp and a real `Date`, one per shape an API puts on the
 * wire: epoch seconds/millis/micros/nanos, RFC 3339 date-times, and plain calendar dates.
 *
 * Timestamps are `Date`s behind the aliases below, so a later move to `Temporal.Instant` is
 * one alias change. A `Date` holds milliseconds; a value with digits below the millisecond
 * (`epoch-micros`/`epoch-nanos`, a long RFC 3339 fraction) parses to a `PreciseDate`, a
 * `Date` that also keeps its sub-millisecond nanoseconds, and dumps back with every digit.
 * The arithmetic is exact integer (`BigInt`) arithmetic throughout. (Python keeps
 * microseconds and rounds nanoseconds; this keeps nanoseconds.)
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

/**
 * A `Date` that keeps the nanoseconds below its millisecond: `subMillisecondNanos` is
 * `0`–`999999`, added to `getTime()`'s milliseconds. The `Date` setters move only the
 * millisecond part.
 */
export class PreciseDate extends Date {
  readonly subMillisecondNanos: number

  /** The instant `epochNanoseconds` nanoseconds after the epoch. */
  constructor(epochNanoseconds: bigint) {
    super(Number(floorDiv(epochNanoseconds, 1_000_000n)))
    this.subMillisecondNanos = Number(epochNanoseconds - floorDiv(epochNanoseconds, 1_000_000n) * 1_000_000n)
  }

  /** Nanoseconds since the epoch, exact. */
  get epochNanoseconds(): bigint {
    return BigInt(this.getTime()) * 1_000_000n + BigInt(this.subMillisecondNanos)
  }

  /** `toISOString` with the fraction extended to microseconds or nanoseconds when the value needs them. */
  toPreciseISOString(): string {
    return isoWithNanos(this)
  }
}

/** Nanoseconds since the epoch of any `Date`: exact for a `PreciseDate`, whole milliseconds otherwise. */
export function epochNanoseconds(date: Date): bigint {
  const ms = date.getTime()
  if (Number.isNaN(ms)) throw new LogicError('Invalid Date')
  return date instanceof PreciseDate ? date.epochNanoseconds : BigInt(ms) * 1_000_000n
}

/** The instant `nanos` after the epoch: a plain `Date` when it is whole milliseconds, else a `PreciseDate`. */
export function fromEpochNanoseconds(nanos: bigint): Date {
  return nanos % 1_000_000n === 0n ? new Date(Number(nanos / 1_000_000n)) : new PreciseDate(nanos)
}

/**
 * `toISOString` with the shortest fraction of 0, 3, 6 or 9 digits that holds the value, as
 * the Rust and Go runtimes write it: `.733340` stays six digits, while redundant zero groups
 * (`.000`, `.500000`) are dropped and any offset is written as `Z`. The source text's own
 * width is not kept.
 */
function isoWithNanos(date: Date): string {
  const sub = date instanceof PreciseDate ? date.subMillisecondNanos : 0
  // `toISOString` always ends `.mmmZ`; the year before it can be four digits or a signed six.
  const iso = date.toISOString()
  const seconds = iso.slice(0, -5)
  const nanos = iso.slice(-4, -1) + String(sub).padStart(6, '0')
  const width = sub === 0 ? (nanos.startsWith('000') ? 0 : 3) : sub % 1000 === 0 ? 6 : 9
  return width === 0 ? `${seconds}Z` : `${seconds}.${nanos.slice(0, width)}Z`
}

/** Floor division on `BigInt`s (`/` truncates toward zero). */
function floorDiv(n: bigint, d: bigint): bigint {
  const q = n / d
  return n % d < 0n ? q - 1n : q
}

function toBigInt(value: number | string | bigint, what: string): bigint {
  if (typeof value === 'bigint') return value
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) throw new LogicError(`Not an epoch ${what}: ${value}`)
    return BigInt(value)
  }
  if (!/^[+-]?\d+$/.test(value.trim())) throw new LogicError(`Not an epoch ${what}: ${JSON.stringify(value)}`)
  return BigInt(value.trim())
}

const NUMERAL = /^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i

/**
 * A fractional `number` of `unit`s per second in nanoseconds: read through its shortest
 * decimal form (the digits the wire sent, `1688669448.4712`), exactly, then rounded half
 * away from zero to the nanosecond, as the Rust runtime rounds.
 */
function fractionalNanos(value: number, unit: bigint): bigint {
  const [, sign, whole, fraction = '', exponent = '0'] = NUMERAL.exec(String(value))!
  const shift = Number(exponent) - fraction.length
  let num = BigInt(whole + fraction) * 1_000_000_000n
  let den = unit
  if (shift >= 0) num *= 10n ** BigInt(shift)
  else den *= 10n ** BigInt(-shift)
  const nanos = (2n * num + den) / (2n * den)
  return sign === '-' ? -nanos : nanos
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
   * number (`"timestamp": "1786302600000"`); a string or `bigint` is read exactly, so a
   * nanosecond value beyond `Number.MAX_SAFE_INTEGER` keeps every digit (a `PreciseDate`).
   * A fractional `number` (kraken's `1688669448.4712` seconds) keeps its fraction, to the
   * nearest nanosecond, as the Python and Rust runtimes keep theirs.
   */
  parse(value: number | string | bigint): Date {
    if (typeof value === 'number' && Number.isFinite(value) && !Number.isInteger(value)) {
      return fromEpochNanoseconds(fractionalNanos(value, this.unit))
    }
    return fromEpochNanoseconds(floorDiv(toBigInt(value, 'timestamp') * 1_000_000_000n, this.unit))
  }

  /**
   * Convert a `Date` back into an epoch timestamp in this unit. Integer arithmetic
   * throughout; the result is exact whenever it is a safe integer, and for nanoseconds
   * the nearest double, which `JSON.stringify` still prints with the original digits.
   */
  dump(date: Date): number {
    return Number(this.dumpBigInt(date))
  }

  /** `dump`, exact at any magnitude and down to a `PreciseDate`'s nanoseconds. */
  dumpBigInt(date: Date): bigint {
    return floorDiv(epochNanoseconds(date) * this.unit, 1_000_000_000n)
  }

  /**
   * The count for a `number` schema, keeping a fraction of a unit: whole units exactly (a
   * `bigint` beyond `Number.MAX_SAFE_INTEGER`), anything between two units as the `number`
   * nearest the exact decimal, so a fraction `parse` read goes back as the digits it came as.
   */
  dumpNumber(date: Date): number | bigint {
    const scaled = epochNanoseconds(date) * this.unit
    const magnitude = scaled < 0n ? -scaled : scaled
    const rest = magnitude % 1_000_000_000n
    if (rest === 0n) {
      const whole = scaled / 1_000_000_000n
      return Number.isSafeInteger(Number(whole)) ? Number(whole) : whole
    }
    const sign = scaled < 0n ? '-' : ''
    return Number(`${sign}${magnitude / 1_000_000_000n}.${String(rest).padStart(9, '0')}`)
  }

  /** The current time, in this unit. */
  now(): number {
    return this.dump(new Date())
  }
}

const DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:([Zz])|([+-])(\d{2}):(\d{2}))?$/

/** Converter for RFC 3339 date-times, always UTC and `Z`-suffixed on the wire. */
export class IsoConverter implements TimeConverter<string> {
  /**
   * Parse a `Z`-suffixed or offset RFC 3339 date-time with a fraction of any length
   * (some APIs send milliseconds, others nanoseconds); digits down to nanoseconds are
   * kept (a `PreciseDate` when any fall below the millisecond), further ones dropped. A
   * date-time with no offset at all (Hyperliquid's `1970-01-01T00:00:00`) is read as UTC,
   * as Python's converter reads it.
   */
  parse(value: string): Date {
    const m = DATE_TIME.exec(value)
    if (!m) throw new LogicError(`Not an RFC 3339 date-time: ${JSON.stringify(value)}`)
    const [, y, mo, d, h, mi, s, frac, , sign, oh, om] = m
    const nanos = frac ? (frac + '00000000').slice(0, 9) : '000000000'
    const ms = Number(nanos.slice(0, 3))
    // `setUTCFullYear`, not `Date.UTC`, which reads years 0000-0099 as 1900-1999. The
    // calendar check reads the local fields, before the offset moves the instant into
    // another month (`2026-09-30T17:30:00-10:00` is October in UTC).
    const local = new Date(0)
    local.setUTCFullYear(Number(y), Number(mo) - 1, Number(d))
    local.setUTCHours(Number(h), Number(mi), Number(s), ms)
    if (
      local.getUTCMonth() !== Number(mo) - 1 || Number(d) > 31 || Number(h) > 23 || Number(mi) > 59 || Number(s) > 60 ||
      (sign && (Number(oh) > 23 || Number(om) > 59))
    ) {
      throw new LogicError(`Not an RFC 3339 date-time: ${JSON.stringify(value)}`)
    }
    let utc = local.getTime()
    if (sign) utc -= (sign === '-' ? -1 : 1) * (Number(oh) * 60 + Number(om)) * 60_000
    const date = new Date(utc)
    // The wire form is UTC with a four-digit year, so an offset may not carry it past either end.
    if (date.getUTCFullYear() < 0 || date.getUTCFullYear() > 9999) {
      throw new LogicError(`Not an RFC 3339 date-time in years 0000-9999 UTC: ${JSON.stringify(value)}`)
    }
    const sub = Number(nanos.slice(3))
    return sub === 0 ? date : new PreciseDate(BigInt(utc) * 1_000_000n + BigInt(sub))
  }

  /**
   * Render a `Date` as UTC, `Z`-suffixed, with a fraction only when it is non-zero: three,
   * six or nine digits, as many as the value needs (a `PreciseDate` takes six or nine), as
   * the Rust and Go runtimes render it. A `Date` keeps no record of the width it was read
   * in, so a wire fraction of another width (`.5`, `.1234`) comes back padded to the group.
   */
  dump(date: Date): string {
    if (Number.isNaN(date.getTime())) throw new LogicError('Invalid Date')
    return isoWithNanos(date)
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
