/**
 * Pins the converters' RFC 3339 handling and exact integer epoch arithmetic: venues send
 * `Z`-suffixed timestamps with anywhere from no fractional digits to nanoseconds, and epoch
 * values in every unit, some as numeral strings beyond `Number.MAX_SAFE_INTEGER`.
 */
import { describe, expect, it } from 'vitest'
import { LogicError } from '../src/errors.js'
import { DateConverter, DateIso, EpochConverter, IsoConverter, PreciseDate, epochNanoseconds, fromEpochNanoseconds, timestampNanos, timestampMicros, timestampMillis, timestampIso } from '../src/times.js'

const utc = (y: number, mo: number, d: number, h = 0, mi = 0, s = 0, ms = 0) => new Date(Date.UTC(y, mo - 1, d, h, mi, s, ms))

describe('IsoConverter.parse', () => {
  it('Z-suffixed nine-digit fraction: nanosecond precision, more than a Date holds', () => {
    expect(new IsoConverter().parse('2024-05-30T12:34:56.123456789Z')).toEqual(utc(2024, 5, 30, 12, 34, 56, 123))
  })

  it('Z-suffixed two-digit fraction is padded, not just truncated', () => {
    expect(new IsoConverter().parse('2024-05-30T12:34:56.12Z')).toEqual(utc(2024, 5, 30, 12, 34, 56, 120))
  })

  it('Z-suffixed, no fraction', () => {
    expect(new IsoConverter().parse('2024-05-30T12:34:56Z')).toEqual(utc(2024, 5, 30, 12, 34, 56))
  })

  it('an explicit offset is the same instant', () => {
    expect(new IsoConverter().parse('2024-05-30T14:34:56+02:00')).toEqual(utc(2024, 5, 30, 12, 34, 56))
    expect(new IsoConverter().parse('2024-05-30T10:04:56.5-02:30')).toEqual(utc(2024, 5, 30, 12, 34, 56, 500))
  })

  it('an offset that moves the instant into another month is still valid (weather.gov, TRU-489)', () => {
    expect(new IsoConverter().parse('2026-09-30T17:30:00-10:00')).toEqual(utc(2026, 10, 1, 3, 30))
    expect(new IsoConverter().parse('2026-10-01T02:00:00+05:00')).toEqual(utc(2026, 9, 30, 21))
    expect(epochNanoseconds(new IsoConverter().parse('2026-12-31T23:00:00.000000001-01:00'))).toBe(1_798_761_600_000_000_001n)
  })

  it('years 0000-0099 are those years, not 1900-1999 (TRU-492)', () => {
    const c = new IsoConverter()
    expect(c.dump(c.parse('0099-12-15T23:00:00Z'))).toBe('0099-12-15T23:00:00Z')
    expect(c.dump(c.parse('0000-02-29T12:00:00.000000001+01:00'))).toBe('0000-02-29T11:00:00.000000001Z')
    expect(() => c.parse('0100-02-29T00:00:00Z')).toThrow(LogicError)
  })

  it('an offset may not carry the UTC instant outside years 0000-9999 (TRU-492)', () => {
    const c = new IsoConverter()
    expect(c.dump(c.parse('9999-12-31T23:59:59.999999999Z'))).toBe('9999-12-31T23:59:59.999999999Z')
    expect(c.dump(c.parse('0000-01-01T00:00:00-01:00'))).toBe('0000-01-01T01:00:00Z')
    for (const bad of ['9999-12-31T23:00:00-01:00', '9999-12-31T23:00:00.5-01:00', '9999-12-31T23:00:00.000000001-01:00', '0000-01-01T00:00:00+00:01', '0000-01-01T00:59:59.999999999+01:00']) {
      expect(() => c.parse(bad)).toThrow(LogicError)
    }
  })

  it('offset hours run 00-23 and minutes 00-59 (TRU-492)', () => {
    expect(new IsoConverter().parse('2026-10-15T00:00:00+23:59')).toEqual(utc(2026, 10, 14, 0, 1))
    for (const bad of ['2026-10-15T00:00:00+24:00', '2026-10-15T00:00:00-00:60', '2026-10-15T00:00:00+99:99']) {
      expect(() => new IsoConverter().parse(bad)).toThrow(LogicError)
    }
  })

  it('no offset at all is UTC, as Python reads it (Hyperliquid sends `1970-01-01T00:00:00`)', () => {
    expect(new IsoConverter().parse('2024-05-30T12:34:56')).toEqual(utc(2024, 5, 30, 12, 34, 56))
    expect(new IsoConverter().parse('1970-01-01T00:00:00')).toEqual(new Date(0))
    expect(epochNanoseconds(new IsoConverter().parse('2026-02-20T19:01:52.647760965'))).toBe(1_771_614_112_647_760_965n)
  })

  it('rejects what is not an RFC 3339 date-time', () => {
    for (const bad of ['2024-05-30', '2024-05-30T12:34', '2024-05-30T12:34:56+0200', '2024-13-01T00:00:00Z', '2026-02-30T00:00:00Z', '2026-09-31T12:00:00-10:00', '2026-10-00T02:00:00+05:00', 'yesterday', '1717072496']) {
      expect(() => new IsoConverter().parse(bad)).toThrow(LogicError)
    }
  })
})

describe('IsoConverter.dump', () => {
  it('renders UTC, Z-suffixed, without a fraction when there are no milliseconds', () => {
    expect(new IsoConverter().dump(utc(2024, 5, 30, 12, 34, 56))).toBe('2024-05-30T12:34:56Z')
  })

  it('keeps milliseconds when there are some', () => {
    expect(new IsoConverter().dump(utc(2024, 5, 30, 12, 34, 56, 961))).toBe('2024-05-30T12:34:56.961Z')
  })

  it('writes the fraction in groups of three, as the Rust and Go runtimes do', () => {
    const conv = new IsoConverter()
    for (const wire of [
      '2026-09-07T00:12:02Z', // github
      '2026-08-13T12:07:05.733340Z', // kraken, microseconds with a trailing zero
      '2026-08-13T12:07:05.000010Z',
      '2026-08-13T12:07:05.100000001Z',
      '2026-08-13T12:07:05.120Z',
      '2026-08-13T12:07:05.647760960Z',
    ]) {
      expect(conv.dump(conv.parse(wire))).toBe(wire)
    }
    // Only as many groups as the value needs: Rust's `AutoSi` and Go write these the same.
    expect(conv.dump(conv.parse('2026-08-13T12:07:05.000Z'))).toBe('2026-08-13T12:07:05Z')
    expect(conv.dump(conv.parse('2026-08-13T12:07:05.100000Z'))).toBe('2026-08-13T12:07:05.100Z')
    expect(conv.dump(conv.parse('2026-08-13T12:07:05.7333400Z'))).toBe('2026-08-13T12:07:05.733340Z')
    expect(conv.dump(new PreciseDate(1_000_000n))).toBe('1970-01-01T00:00:00.001Z')
    expect(conv.dump(new PreciseDate(-1n))).toBe('1969-12-31T23:59:59.999999999Z')
  })

  it('finds the fraction after an expanded year of either sign', () => {
    const conv = new IsoConverter()
    // `toISOString` writes years outside 0..9999 as a signed six-digit year.
    expect(conv.dump(new Date('+010000-01-01T00:00:00.000Z'))).toBe('+010000-01-01T00:00:00Z')
    expect(conv.dump(new Date('+010000-01-01T00:00:00.120Z'))).toBe('+010000-01-01T00:00:00.120Z')
    expect(conv.dump(new Date('-000001-01-01T00:00:00.123Z'))).toBe('-000001-01-01T00:00:00.123Z')
    expect(conv.dump(new Date('-000001-01-01T00:00:00.000Z'))).toBe('-000001-01-01T00:00:00Z')
    const nanos = (iso: string, sub: bigint) => new PreciseDate(BigInt(Date.parse(iso)) * 1_000_000n + sub)
    expect(conv.dump(nanos('+010000-01-01T00:00:00.733Z', 340_000n))).toBe('+010000-01-01T00:00:00.733340Z')
    expect(conv.dump(nanos('-000001-01-01T00:00:00.123Z', 456_789n))).toBe('-000001-01-01T00:00:00.123456789Z')
    expect(nanos('-000001-12-31T23:59:59.999Z', 1n).toPreciseISOString()).toBe('-000001-12-31T23:59:59.999000001Z')
  })

  it('round trips the bit2me shape', () => {
    const conv = new IsoConverter()
    expect(conv.dump(conv.parse('2024-05-07T14:08:30.961Z'))).toBe('2024-05-07T14:08:30.961Z')
  })

  it('now() is in wire form', () => {
    const conv = new IsoConverter()
    expect(Math.abs(conv.parse(conv.now()).getTime() - Date.now())).toBeLessThan(5000)
  })
})

describe('EpochConverter round trips', () => {
  const dt = utc(2024, 5, 30, 12, 34, 56, 123)

  it('milliseconds', () => {
    const conv = EpochConverter.milliseconds()
    expect(conv.dump(dt)).toBe(1717072496123)
    expect(conv.parse(1717072496123)).toEqual(dt)
  })

  it('seconds: sub-second precision is lost on dump, by construction of the unit', () => {
    const conv = EpochConverter.seconds()
    expect(conv.dump(dt)).toBe(1717072496)
    expect(conv.parse(1717072496)).toEqual(utc(2024, 5, 30, 12, 34, 56))
  })

  it('microseconds', () => {
    const conv = EpochConverter.microseconds()
    expect(conv.dump(dt)).toBe(1717072496123000)
    expect(conv.parse(1717072496123000)).toEqual(dt)
    expect(conv.parse(conv.dump(dt))).toEqual(dt)
  })

  it('nanoseconds: exact at nanosecond scale through BigInt arithmetic', () => {
    const conv = EpochConverter.nanoseconds()
    expect(conv.dumpBigInt(dt)).toBe(1717072496123000000n)
    expect(JSON.stringify(conv.dump(dt))).toBe('1717072496123000000')
    expect(conv.parse(1717072496123000000n)).toEqual(dt)
    expect(conv.parse('1717072496123456789')).toEqual(dt)
    expect(conv.parse(conv.dump(dt))).toEqual(dt)
  })

  it('accepts a numeral string, as some APIs send the epoch', () => {
    expect(EpochConverter.milliseconds().parse('1717072496123')).toEqual(dt)
  })

  it('floors negative (pre-1970) values like Python does', () => {
    const conv = EpochConverter.microseconds()
    expect(conv.parse(-1)).toEqual(new Date(-1))
    expect(conv.dump(new Date(-1))).toBe(-1000)
  })

  it('a fractional number keeps its fraction, to the nanosecond, rounded half away from zero', () => {
    // toEqual on two Dates compares milliseconds only; compare the exact nanoseconds.
    const seconds = EpochConverter.seconds()
    expect(epochNanoseconds(seconds.parse(1688669448.4712))).toBe(1688669448471200000n)
    expect(epochNanoseconds(seconds.parse(-0.5))).toBe(-500000000n)
    expect(epochNanoseconds(seconds.parse(1.5e-9))).toBe(2n)
    expect(epochNanoseconds(seconds.parse(-1.5e-9))).toBe(-2n)
    expect(epochNanoseconds(EpochConverter.nanoseconds().parse(0.5))).toBe(1n)
    expect(epochNanoseconds(EpochConverter.nanoseconds().parse(-0.5))).toBe(-1n)
    // The double nearest 1717072496123456.7 prints as 1717072496123456.8.
    expect(epochNanoseconds(EpochConverter.microseconds().parse(1717072496123456.7))).toBe(1717072496123456800n)
  })

  it('dumpNumber writes a fraction back as the digits it came as, a whole count as an integer', () => {
    const seconds = EpochConverter.seconds()
    expect(seconds.dumpNumber(seconds.parse(1688669448.4712))).toBe(1688669448.4712)
    expect(seconds.dumpNumber(new Date(-500))).toBe(-0.5)
    expect(seconds.dumpNumber(new Date(-1500))).toBe(-1.5)
    expect(seconds.dumpNumber(new Date(1717072496000))).toBe(1717072496)
    expect(EpochConverter.nanoseconds().dumpNumber(new PreciseDate(1717072496123456789n))).toBe(1717072496123456789n)
    expect(EpochConverter.milliseconds().dumpNumber(new PreciseDate(1717072496123250000n))).toBe(1717072496123.25)
    expect(() => seconds.dumpNumber(new Date(Number.NaN))).toThrow(LogicError)
  })

  it('rejects what is not an epoch', () => {
    expect(() => EpochConverter.seconds().parse('12.5')).toThrow(LogicError)
    expect(() => EpochConverter.seconds().parse(Number.NaN)).toThrow(LogicError)
    expect(() => EpochConverter.seconds().dump(new Date(Number.NaN))).toThrow(LogicError)
  })

  it('now() is in the unit', () => {
    const conv = EpochConverter.milliseconds()
    const now = conv.now()
    expect(Number.isInteger(now)).toBe(true)
    expect(Math.abs(conv.parse(now).getTime() - Date.now())).toBeLessThan(5000)
  })
})

describe('DateConverter', () => {
  it('parses a plain calendar date', () => {
    expect(new DateConverter().parse('2026-08-03')).toBe('2026-08-03')
  })

  it('dumps', () => {
    expect(new DateConverter().dump(DateIso.of('2026-08-03'))).toBe('2026-08-03')
  })

  it('round trips', () => {
    const conv = new DateConverter()
    expect(conv.parse(conv.dump(DateIso.of('2026-08-03')))).toBe('2026-08-03')
  })

  it('a compact pattern round trips', () => {
    // bitget's broker-commission endpoints send `YYYYMMDD` with no separators.
    const conv = new DateConverter({ pattern: '%Y%m%d' })
    expect(conv.parse('20260101')).toBe('2026-01-01')
    expect(conv.dump(DateIso.of('2026-01-01'))).toBe('20260101')
    expect(conv.parse(conv.dump(DateIso.of('2026-01-01')))).toBe('2026-01-01')
  })

  it('the default pattern is RFC 3339', () => {
    expect(new DateConverter().pattern).toBe('%Y-%m-%d')
  })

  it('rejects what does not match the pattern or the calendar', () => {
    expect(() => new DateConverter().parse('20260101')).toThrow(LogicError)
    expect(() => new DateConverter().parse('2026-02-30')).toThrow(LogicError)
    expect(() => DateIso.of('2026-1-1')).toThrow(LogicError)
  })

  it('DateIso converts to and from an instant', () => {
    expect(DateIso.toDate(DateIso.of('2026-08-03'))).toEqual(utc(2026, 8, 3))
    expect(DateIso.fromDate(utc(2026, 8, 3, 23, 59))).toBe('2026-08-03')
    expect(DateIso.is(new DateConverter().now())).toBe(true)
  })
})

describe('sub-millisecond precision', () => {
  it('keeps every nanosecond digit of an epoch-nanos value, up to 2^64', () => {
    const wire = 1786302600123456789n
    const date = timestampNanos.parse(wire)
    expect(date).toBeInstanceOf(PreciseDate)
    expect((date as PreciseDate).subMillisecondNanos).toBe(456789)
    expect(date.getTime()).toBe(1786302600123)
    expect(timestampNanos.dumpBigInt(date)).toBe(wire)
    expect(timestampNanos.dumpBigInt(timestampNanos.parse(String(wire)))).toBe(wire)
    expect(epochNanoseconds(date)).toBe(wire)
  })

  it('keeps microseconds exactly, as Python does', () => {
    const date = timestampMicros.parse(1786302600123456)
    expect(timestampMicros.dumpBigInt(date)).toBe(1786302600123456n)
    expect(timestampMicros.dump(date)).toBe(1786302600123456)
    expect(timestampMillis.dump(date)).toBe(1786302600123)
    expect(timestampNanos.dumpBigInt(date)).toBe(1786302600123456000n)
  })

  it('still parses a whole-millisecond value to a plain Date', () => {
    const date = timestampNanos.parse(1786302600123000000n)
    expect(date).not.toBeInstanceOf(PreciseDate)
    expect(date).toEqual(new Date(1786302600123))
  })

  it('handles instants before the epoch', () => {
    const date = timestampNanos.parse(-1n)
    expect(date.getTime()).toBe(-1)
    expect((date as PreciseDate).subMillisecondNanos).toBe(999999)
    expect(timestampNanos.dumpBigInt(date)).toBe(-1n)
    expect(fromEpochNanoseconds(-1_000_000n)).toEqual(new Date(-1))
  })

  it('reads and writes an RFC 3339 fraction down to nanoseconds', () => {
    const date = timestampIso.parse('2026-08-03T12:30:00.123456789Z')
    expect(epochNanoseconds(date)).toBe(BigInt(Date.UTC(2026, 7, 3, 12, 30, 0, 123)) * 1_000_000n + 456789n)
    expect(timestampIso.dump(date)).toBe('2026-08-03T12:30:00.123456789Z')
    expect(timestampIso.dump(timestampIso.parse('2026-08-03T12:30:00.1234Z'))).toBe('2026-08-03T12:30:00.123400Z')
    expect(timestampIso.dump(timestampIso.parse('2026-08-03T12:30:00.5Z'))).toBe('2026-08-03T12:30:00.500Z')
    expect(timestampIso.dump(timestampIso.parse('2026-08-03T12:30:00Z'))).toBe('2026-08-03T12:30:00Z')
    expect((timestampIso.parse('2026-08-03T14:30:00.000001+02:00') as PreciseDate).toPreciseISOString()).toBe('2026-08-03T12:30:00.000001Z')
  })
})
