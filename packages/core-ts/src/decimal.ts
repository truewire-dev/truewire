/**
 * `decimal-string` wire values as a branded string.
 *
 * A `Decimal` is exactly the digits the API sent: no allocation, no rounding, and it
 * serializes back verbatim. Callers who want arithmetic hand it to their library of choice
 * (`new Big(price)`); this module only offers the operations that need no such library.
 */
import { LogicError } from './errors.js'

declare const decimalBrand: unique symbol

/** A decimal number kept as the exact string the wire carried, e.g. `'10.50'`. */
export type Decimal = string & { readonly [decimalBrand]: true }

const PATTERN = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/

/** Whether `value` is a string in decimal-string form. */
export function isDecimal(value: unknown): value is Decimal {
  return typeof value === 'string' && PATTERN.test(value)
}

/** Digits of `d` normalised for comparison: sign, integer digits, fraction digits. */
function split(d: string): [sign: 1 | -1, int: string, frac: string] {
  let sign: 1 | -1 = 1
  let s = d
  if (s[0] === '-') { sign = -1; s = s.slice(1) } else if (s[0] === '+') s = s.slice(1)
  let exp = 0
  const e = s.search(/[eE]/)
  if (e >= 0) { exp = Number(s.slice(e + 1)); s = s.slice(0, e) }
  const dot = s.indexOf('.')
  let int = dot < 0 ? s : s.slice(0, dot)
  let frac = dot < 0 ? '' : s.slice(dot + 1)
  if (exp > 0) { const move = frac.slice(0, exp); int += move + '0'.repeat(exp - move.length); frac = frac.slice(exp) }
  else if (exp < 0) { const n = -exp; const move = int.slice(-n); frac = '0'.repeat(n - move.length) + move + frac; int = int.slice(0, -n) }
  int = int.replace(/^0+/, '')
  frac = frac.replace(/0+$/, '')
  if (int === '' && frac === '') sign = 1
  return [sign, int, frac]
}

export const Decimal = {
  /** Brand a string as `Decimal`, throwing `LogicError` when it is not one. */
  of(value: string | number | bigint): Decimal {
    const s = typeof value === 'string' ? value : String(value)
    if (!isDecimal(s)) throw new LogicError(`Not a decimal string: ${JSON.stringify(value)}`)
    return s
  },
  /** Exact comparison: `-1`, `0` or `1`. `'1.50'` equals `'1.5'`. */
  compare(a: Decimal, b: Decimal): -1 | 0 | 1 {
    const [sa, ia, fa] = split(a)
    const [sb, ib, fb] = split(b)
    if (sa !== sb) return sa < sb ? -1 : 1
    let cmp = 0
    if (ia.length !== ib.length) cmp = ia.length < ib.length ? -1 : 1
    else if (ia !== ib) cmp = ia < ib ? -1 : 1
    else {
      const n = Math.max(fa.length, fb.length)
      const pa = fa.padEnd(n, '0'), pb = fb.padEnd(n, '0')
      if (pa !== pb) cmp = pa < pb ? -1 : 1
    }
    return (cmp * sa) as -1 | 0 | 1
  },
  /** The nearest `number`; loses digits beyond double precision. */
  toNumber(d: Decimal): number {
    return Number(d)
  },
}
