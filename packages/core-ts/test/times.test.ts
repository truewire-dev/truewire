/**
 * Pins the converters' RFC 3339 handling and exact integer epoch arithmetic: venues send
 * `Z`-suffixed timestamps with anywhere from no fractional digits to nanoseconds, and epoch
 * values in every unit, some as numeral strings beyond `Number.MAX_SAFE_INTEGER`.
 */
import { describe, expect, it } from 'vitest'
import { LogicError } from '../src/errors.js'
import { DateConverter, DateIso, EpochConverter, IsoConverter } from '../src/times.js'

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

  it('rejects what is not an RFC 3339 date-time', () => {
    for (const bad of ['2024-05-30', '2024-05-30T12:34:56', '2024-13-01T00:00:00Z', 'yesterday', '1717072496']) {
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
