import { describe, expect, it } from 'vitest'
import { parseJsonText, stringifyJson } from '../src/json.js'
import { ValidationError } from '../src/errors.js'
import { dumpJson, parseJson, unknown } from '../src/validation.js'

describe('lossless JSON', () => {
  it('reads an integer beyond 2^53 as a bigint, every digit kept', () => {
    const text = '{"orderId": 9007199254740993, "nanos": 1786302600123456789, "neg": -18446744073709551615}'
    expect(parseJsonText(text)).toEqual({ orderId: 9007199254740993n, nanos: 1786302600123456789n, neg: -18446744073709551615n })
  })

  it('leaves safe integers, fractions and exponents as numbers, like JSON.parse', () => {
    const text = '[9007199254740991, 12345678901234567.5, 1e21, 0, -1, "12345678901234567890", true, null, {"a": []}]'
    expect(parseJsonText(text)).toEqual(JSON.parse(text))
    expect(parseJsonText('{"n": 42}')).toEqual({ n: 42 })
  })

  it('parses strings, escapes and nesting exactly as JSON.parse does', () => {
    const text = ' {"s": "a\\"b\\\\c\\u00e9\\n", "big": [1, [12345678901234567890]], "__proto__": 1} '
    const parsed = parseJsonText(text) as Record<string, unknown>
    expect(parsed.s).toBe('a"b\\cé\n')
    expect(parsed.big).toEqual([1, [12345678901234567890n]])
    expect(Object.keys(parsed)).toEqual(['s', 'big', '__proto__'])
    expect(Object.getPrototypeOf(parsed)).toBe(Object.prototype)
  })

  it('rejects what JSON.parse rejects', () => {
    for (const bad of ['[12345678901234567890,]', '{"a" 12345678901234567890}', '12345678901234567890 x', "{'a': 12345678901234567890}", '[01234567890123456789]']) {
      expect(() => parseJsonText(bad), bad).toThrow(SyntaxError)
    }
  })

  it('writes a bigint as bare digits and round-trips', () => {
    const value = { id: 18446744073709551615n, list: [1, 2n], skip: undefined, when: new Date(0) }
    const text = stringifyJson(value)
    expect(text).toBe('{"id":18446744073709551615,"list":[1,2],"when":"1970-01-01T00:00:00.000Z"}')
    expect(parseJsonText(text)).toEqual({ id: 18446744073709551615n, list: [1, 2], when: '1970-01-01T00:00:00.000Z' })
    expect(stringifyJson([undefined, () => 1])).toBe('[null,null]')
  })

  it('parseJson/dumpJson go through it', () => {
    expect(parseJson(unknown, '[123456789012345678901]')).toEqual([123456789012345678901n])
    expect(dumpJson(unknown, [123456789012345678901n])).toBe('[123456789012345678901]')
    expect(() => parseJson(unknown, '[123456789012345678901')).toThrow(ValidationError)
  })
})
