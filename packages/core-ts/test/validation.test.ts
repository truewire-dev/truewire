/**
 * Pins the codec contract: every combinator parses wire JSON into the typed value and dumps
 * it back, undocumented keys survive both ways, unions try variants in order, and every
 * mismatch is a `ValidationError` naming the path.
 */
import { describe, expect, it } from 'vitest'
import { Decimal } from '../src/decimal.js'
import { ValidationError } from '../src/errors.js'
import { DateIso } from '../src/times.js'
import * as t from '../src/validation.js'
import { dumpJson, parseJson, type Codec } from '../src/validation.js'

interface Order {
  id: string
  amount: Decimal
  note?: string
}

// `tsc` proves the codec matches the interface: a drift in either is a compile error.
const Order: Codec<Order> = t.object({ id: t.string, amount: t.decimal, note: t.optional(t.string) })

function failure(fn: () => unknown): ValidationError {
  try { fn() } catch (e) { if (e instanceof ValidationError) return e; throw e }
  throw new Error('expected a ValidationError')
}

describe('parse', () => {
  it('a JSON document', () => {
    expect(parseJson(Order, '{"id": "ord_1", "amount": "10.5"}')).toEqual({ id: 'ord_1', amount: '10.5' })
  })

  it('a decoded value', () => {
    expect(Order.parse({ id: 'ord_1', amount: '10.5' })).toEqual({ id: 'ord_1', amount: '10.5' })
  })

  it('keeps an undocumented field instead of rejecting it', () => {
    expect(parseJson(Order, '{"id": "ord_1", "amount": "10.5", "extra": true}'))
      .toEqual({ id: 'ord_1', amount: '10.5', extra: true })
  })

  it('a missing required key is a ValidationError naming the key', () => {
    const err = failure(() => parseJson(Order, '{"id": "ord_1"}'))
    expect(err.path).toBe('/amount')
    expect(err.message).toBe('missing required key at /amount')
  })

  it('a missing optional key is fine, and stays absent', () => {
    expect('note' in Order.parse({ id: 'x', amount: '1' })).toBe(false)
    expect(Order.parse({ id: 'x', amount: '1', note: 'n' }).note).toBe('n')
  })

  it('a wrong type is a ValidationError naming the path and both types', () => {
    const err = failure(() => Order.parse({ id: 1, amount: '10.5' }))
    expect(err.path).toBe('/id')
    expect(err.message).toBe('expected string, got 1 at /id')
  })

  it('invalid JSON is a ValidationError chained from the SyntaxError', () => {
    const err = failure(() => parseJson(Order, '{not json'))
    expect(err.cause).toBeInstanceOf(SyntaxError)
  })

  it('a non-object root is reported at /', () => {
    expect(failure(() => Order.parse([])).message).toBe('expected object, got array at /')
    expect(failure(() => Order.parse(null)).message).toBe('expected object, got null at /')
  })

  it('escapes JSON pointer segments', () => {
    const codec = t.object({ 'a/b': t.string, 'c~d': t.string })
    expect(failure(() => codec.parse({ 'a/b': 1, 'c~d': 'x' })).path).toBe('/a~1b')
    expect(failure(() => codec.parse({ 'a/b': 'x', 'c~d': 1 })).path).toBe('/c~0d')
  })
})

describe('dump', () => {
  it('renders to a JSON document', () => {
    expect(dumpJson(Order, { id: 'ord_1', amount: Decimal.of('10.5') })).toBe('{"id":"ord_1","amount":"10.5"}')
  })

  it('keeps extra fields', () => {
    const order = { id: 'ord_1', amount: Decimal.of('10.5'), extra: true } as Order
    expect(dumpJson(Order, order)).toBe('{"id":"ord_1","amount":"10.5","extra":true}')
  })

  it('round trips', () => {
    const order = { id: 'ord_1', amount: Decimal.of('10.5') }
    expect(parseJson(Order, dumpJson(Order, order))).toEqual(order)
  })

  it('checks the typed value, so a request built wrong fails before the wire', () => {
    const err = failure(() => Order.dump({ id: 'x', amount: 'abc' as Decimal }))
    expect(err.message).toBe('expected decimal string, got "abc" at /amount')
  })

  it('omits an optional key set to undefined', () => {
    expect(Order.dump({ id: 'x', amount: Decimal.of('1'), note: undefined })).toEqual({ id: 'x', amount: '1' })
  })
})

describe('scalars', () => {
  it('string, number, integer, boolean, null, unknown', () => {
    expect(t.string.parse('a')).toBe('a')
    expect(t.number.parse(1.5)).toBe(1.5)
    expect(t.integer.parse(3)).toBe(3)
    expect(t.boolean.parse(false)).toBe(false)
    expect(t.null.parse(null)).toBeNull()
    expect(t.unknown.parse({ any: ['thing'] })).toEqual({ any: ['thing'] })
    expect(t.unknown.dump(undefined)).toBeUndefined()
    expect(failure(() => t.number.parse('1')).message).toBe('expected number, got "1" at /')
    expect(failure(() => t.number.parse(Number.POSITIVE_INFINITY)).message).toMatch(/expected number/)
    expect(failure(() => t.integer.parse(1.5)).message).toBe('expected integer, got 1.5 at /')
    expect(failure(() => t.boolean.parse('true')).message).toMatch(/expected boolean/)
    expect(failure(() => t.null.parse(undefined)).message).toBe('expected null, got undefined at /')
  })

  it('integer rejects values beyond MAX_SAFE_INTEGER', () => {
    expect(() => t.integer.parse(2 ** 53)).toThrow(ValidationError)
    expect(t.integer.parse(2 ** 53 - 1)).toBe(2 ** 53 - 1)
    expect(() => t.integer.dump(2 ** 53)).toThrow(ValidationError)
  })

  it('literal / enum', () => {
    const side = t.enum('buy', 'sell')
    expect(side.parse('buy')).toBe('buy')
    expect(side.dump('sell')).toBe('sell')
    expect(failure(() => side.parse('hold')).message).toBe('expected "buy" | "sell", got "hold" at /')
    expect(t.literal(1).parse(1)).toBe(1)
    expect(t.literal(null).parse(null)).toBeNull()
    expect(t.literal(true, 0).parse(0)).toBe(0)
  })
})

describe('wire formats', () => {
  it('decimal-string brands the digits the wire carried', () => {
    for (const s of ['10.50', '-0.1', '1e-7', '42', '.5', '+3.']) expect(t.decimal.parse(s)).toBe(s)
    expect(t.decimal.dump(Decimal.of('10.50'))).toBe('10.50')
    expect(failure(() => t.decimal.parse(10.5)).message).toBe('expected decimal string, got 10.5 at /')
    expect(() => t.decimal.parse('1,5')).toThrow(ValidationError)
    expect(() => t.decimal.parse('')).toThrow(ValidationError)
  })

  it('Decimal helpers', () => {
    expect(Decimal.compare(Decimal.of('1.50'), Decimal.of('1.5'))).toBe(0)
    expect(Decimal.compare(Decimal.of('-2'), Decimal.of('1'))).toBe(-1)
    expect(Decimal.compare(Decimal.of('10'), Decimal.of('9.99'))).toBe(1)
    expect(Decimal.compare(Decimal.of('0.001'), Decimal.of('1e-3'))).toBe(0)
    expect(Decimal.compare(Decimal.of('1.5e2'), Decimal.of('150'))).toBe(0)
    expect(Decimal.compare(Decimal.of('-0'), Decimal.of('0'))).toBe(0)
    expect(Decimal.compare(Decimal.of('0.3'), Decimal.of('0.25'))).toBe(1)
    expect(Decimal.toNumber(Decimal.of('10.5'))).toBe(10.5)
    expect(Decimal.of(42)).toBe('42')
    expect(() => Decimal.of('abc')).toThrow('Not a decimal string')
  })

  it('integer-string', () => {
    expect(t.integerString.parse('42')).toBe(42)
    expect(t.integerString.parse('-7')).toBe(-7)
    expect(t.integerString.dump(42)).toBe('42')
    expect(failure(() => t.integerString.parse('4.2')).message).toMatch(/^expected integer string, got "4.2": not an integer at \/$/)
    expect(failure(() => t.integerString.parse('9007199254740993')).message).toMatch(/beyond safe integer/)
    expect(failure(() => t.integerString.parse(42)).message).toBe('expected string, got 42 at /')
    expect(() => t.integerString.dump(1.5)).toThrow(ValidationError)
  })

  it('boolean-string', () => {
    expect(t.booleanString.parse('true')).toBe(true)
    expect(t.booleanString.parse('false')).toBe(false)
    expect(t.booleanString.dump(true)).toBe('true')
    expect(() => t.booleanString.parse('yes')).toThrow(ValidationError)
    expect(() => t.booleanString.parse(true)).toThrow(ValidationError)
  })
})

describe('timestamps', () => {
  const dt = new Date(Date.UTC(2024, 4, 30, 12, 34, 56, 123))

  it('epoch millis: the shape a generated client uses, parse and dump through JSON', () => {
    expect(t.epochMillis.parse(1717072496123)).toEqual(dt)
    expect(dumpJson(t.epochMillis, dt)).toBe('1717072496123')
  })

  it('epoch seconds, micros, nanos round trip exactly, from numbers and numeral strings', () => {
    expect(t.epochSeconds.parse(1717072496)).toEqual(new Date(Date.UTC(2024, 4, 30, 12, 34, 56)))
    expect(t.epochSeconds.dump(dt)).toBe(1717072496)
    expect(t.epochMicros.parse(1717072496123000)).toEqual(dt)
    expect(t.epochMicros.dump(dt)).toBe(1717072496123000)
    expect(t.epochNanos.parse('1717072496123456789')).toEqual(dt)
    expect(dumpJson(t.epochNanos, dt)).toBe('1717072496123000000')
    expect(t.epochNanos.parse(t.epochNanos.dump(dt))).toEqual(dt)
    expect(t.epochMillis.parse('1717072496123')).toEqual(dt)
  })

  it('rejects non-epoch values on both sides', () => {
    expect(failure(() => t.epochMillis.parse('2024-05-30')).message).toBe('expected epoch timestamp, got "2024-05-30" at /')
    expect(() => t.epochMillis.parse(1.5)).not.toThrow()
    expect(() => t.epochMillis.dump('1717072496123' as unknown as Date)).toThrow(ValidationError)
    expect(() => t.epochMillis.dump(new Date(Number.NaN))).toThrow(ValidationError)
  })

  it('date-time: RFC 3339 forms', () => {
    expect(t.dateTime.parse('2024-05-30T12:34:56.123456789Z')).toEqual(dt)
    expect(t.dateTime.parse('2024-05-30T12:34:56.12Z')).toEqual(new Date(Date.UTC(2024, 4, 30, 12, 34, 56, 120)))
    expect(t.dateTime.parse('2024-05-30T14:34:56.123+02:00')).toEqual(dt)
    expect(t.dateTime.dump(dt)).toBe('2024-05-30T12:34:56.123Z')
    expect(t.dateTime.dump(new Date(Date.UTC(2024, 4, 30, 12, 34, 56)))).toBe('2024-05-30T12:34:56Z')
    expect(t.dateTime.parse(t.dateTime.dump(dt))).toEqual(dt)
    expect(failure(() => t.dateTime.parse('2024-05-30')).message).toMatch(/^expected RFC 3339 date-time, got "2024-05-30"/)
    expect(() => t.dateTime.parse(1717072496123)).toThrow(ValidationError)
  })

  it('date: RFC 3339 full-date as a branded string', () => {
    expect(t.date.parse('2026-08-03')).toBe('2026-08-03')
    expect(t.date.dump(DateIso.of('2026-08-03'))).toBe('2026-08-03')
    expect(() => t.date.parse('2026-02-30')).toThrow(ValidationError)
    expect(() => t.date.parse('20260803')).toThrow(ValidationError)
    expect(() => t.date.dump('20260803' as DateIso)).toThrow(ValidationError)
  })

  it('a project can define its own wire format with t.wire', () => {
    const yesNo = t.wire(t.string, 'yes/no', s => { if (s !== 'yes' && s !== 'no') throw new Error('not yes/no'); return s === 'yes' }, b => (b ? 'yes' : 'no'), (v): v is boolean => typeof v === 'boolean')
    expect(yesNo.parse('yes')).toBe(true)
    expect(yesNo.dump(false)).toBe('no')
    expect(failure(() => yesNo.parse('maybe')).message).toBe('expected yes/no, got "maybe": not yes/no at /')
    expect(failure(() => yesNo.dump('yes' as unknown as boolean)).message).toBe('expected yes/no, got "yes" at /')
  })
})

describe('containers', () => {
  it('array parses each item and names the failing index', () => {
    const rows = t.array(Order)
    expect(parseJson(rows, '[{"id": "a", "amount": "1"}, {"id": "b", "amount": "2"}]').map(r => r.id)).toEqual(['a', 'b'])
    expect(failure(() => rows.parse([{ id: 'a', amount: '1' }, { id: 'b' }])).path).toBe('/1/amount')
    expect(failure(() => rows.parse({})).message).toBe('expected array, got object at /')
    expect(rows.dump([{ id: 'a', amount: Decimal.of('1') }])).toEqual([{ id: 'a', amount: '1' }])
  })

  it('tuple / prefixItems', () => {
    const level = t.tuple([t.decimal, t.decimal, t.epochMillis])
    const parsed = level.parse(['1.5', '2', 1717072496123])
    expect(parsed[2]).toBeInstanceOf(Date)
    expect(level.dump(parsed)).toEqual(['1.5', '2', 1717072496123])
    expect(failure(() => level.parse(['1.5', '2'])).message).toBe('expected 3 items, got 2 at /')
    expect(failure(() => level.parse(['1.5', '2', 1, 2])).message).toBe('expected 3 items, got 4 at /')
    expect(failure(() => level.parse(['1.5', 'x', 1])).path).toBe('/1')
    const withRest = t.tuple([t.string], t.integer)
    expect(withRest.parse(['a', 1, 2])).toEqual(['a', 1, 2])
    expect(failure(() => withRest.parse([])).message).toBe('expected at least 1 items, got 0 at /')
    expect(failure(() => withRest.parse(['a', 'b'])).path).toBe('/1')
  })

  it('record / additionalProperties', () => {
    const balances = t.record(t.decimal)
    expect(balances.parse({ BTC: '1.5', ETH: '0' })).toEqual({ BTC: '1.5', ETH: '0' })
    expect(failure(() => balances.parse({ BTC: 1.5 })).path).toBe('/BTC')
    expect(failure(() => balances.parse([])).message).toBe('expected object, got array at /')
    expect(balances.dump({ BTC: Decimal.of('1.5') })).toEqual({ BTC: '1.5' })
  })

  it('union / anyOf tries variants in order and reports every attempt', () => {
    const idOrOrder = t.union(t.string, Order)
    expect(idOrOrder.parse('ord_1')).toBe('ord_1')
    expect(idOrOrder.parse({ id: 'ord_1', amount: '1' })).toEqual({ id: 'ord_1', amount: '1' })
    const err = failure(() => idOrOrder.parse({ id: 'ord_1' }))
    expect(err.message).toBe('no variant matched object at /')
    expect(err.issues.map(i => i.path)).toEqual(['', '', '/amount'])
    expect(idOrOrder.dump({ id: 'x', amount: Decimal.of('1') })).toEqual({ id: 'x', amount: '1' })
    expect(idOrOrder.dump('x')).toBe('x')
    expect(() => idOrOrder.dump(42 as unknown as string)).toThrow(ValidationError)
  })

  it('nullable', () => {
    const maybe = t.nullable(t.epochMillis)
    expect(maybe.parse(null)).toBeNull()
    expect(maybe.parse(0)).toEqual(new Date(0))
    expect(maybe.dump(null)).toBeNull()
    expect(maybe.dump(new Date(0))).toBe(0)
  })

  it('lazy allows recursive shapes', () => {
    interface Node { name: string; children: Node[] }
    const Node: Codec<Node> = t.object({ name: t.string, children: t.array(t.lazy(() => Node)) })
    const tree = { name: 'root', children: [{ name: 'leaf', children: [] }] }
    expect(Node.parse(tree)).toEqual(tree)
    expect(Node.dump(tree)).toEqual(tree)
    expect(failure(() => Node.parse({ name: 'root', children: [{ name: 1, children: [] }] })).path).toBe('/children/0/name')
  })

  it('nested paths compose', () => {
    const book = t.object({ bids: t.array(t.tuple([t.decimal, t.decimal])), ts: t.epochMillis })
    const err = failure(() => book.parse({ bids: [['1', '2'], ['3', 4]], ts: 0 }))
    expect(err.path).toBe('/bids/1/1')
    expect(err.message).toBe('expected decimal string, got 4 at /bids/1/1')
  })
})

describe('types', () => {
  it('Infer and InferObject name the parsed type', () => {
    const codec = t.object({ a: t.string, b: t.optional(t.integer), c: t.union(t.literal('x'), t.null) })
    type Parsed = t.Infer<typeof codec>
    const value: Parsed = { a: 'a', c: null }
    const value2: Parsed = { a: 'a', b: 1, c: 'x' }
    // @ts-expect-error `a` is required
    const missing: Parsed = { c: 'x' }
    expect([value, value2, missing]).toHaveLength(3)
  })
})
