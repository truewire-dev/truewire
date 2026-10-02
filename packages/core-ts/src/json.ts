/**
 * Lossless JSON text: `JSON.parse` reads every number as a double, so an integer beyond
 * `Number.MAX_SAFE_INTEGER` (an int64 order id, a nanosecond epoch) silently loses digits
 * before any codec sees it. `parseJsonText` reads such an integer literal as a `bigint`
 * instead, and `stringifyJson` writes a `bigint` back as bare digits, so the value
 * round-trips exactly, the way Python's `json` keeps an `int`.
 *
 * Every other value is exactly what `JSON.parse`/`JSON.stringify` produce: a safe integer
 * or a fraction is a `number`.
 */

/** An integer literal long enough that it may not be a safe integer. */
const LONG_INTEGER = /\d{16,}/

/** Parse JSON text, reading an unsafe integer literal as a `bigint`. Throws `SyntaxError` like `JSON.parse`. */
export function parseJsonText(text: string): unknown {
  if (!LONG_INTEGER.test(text)) return JSON.parse(text)
  return new Parser(text).document()
}

/** `JSON.stringify` for a value that may hold `bigint`s, written as bare JSON integers. */
export function stringifyJson(value: unknown): string {
  const out = write(value, new Set())
  if (out === undefined) throw new TypeError('value is not representable as JSON')
  return out
}

function write(value: unknown, seen: Set<object>): string | undefined {
  if (typeof value === 'bigint') return value.toString()
  if (value === null || typeof value !== 'object') {
    return typeof value === 'function' || typeof value === 'symbol' ? undefined : JSON.stringify(value)
  }
  if (typeof (value as { toJSON?: unknown }).toJSON === 'function') {
    return write((value as { toJSON(): unknown }).toJSON(), seen)
  }
  if (seen.has(value)) throw new TypeError('Converting circular structure to JSON')
  seen.add(value)
  try {
    if (Array.isArray(value)) return `[${value.map(item => write(item, seen) ?? 'null').join(',')}]`
    const parts: string[] = []
    for (const [key, item] of Object.entries(value)) {
      const rendered = write(item, seen)
      if (rendered !== undefined) parts.push(`${JSON.stringify(key)}:${rendered}`)
    }
    return `{${parts.join(',')}}`
  } finally {
    seen.delete(value)
  }
}

const NUMBER = /-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/y
const STRING = /"(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"/y

class Parser {
  private i = 0
  constructor(private readonly text: string) {}

  document(): unknown {
    const value = this.value()
    this.space()
    if (this.i !== this.text.length) this.error()
    return value
  }

  private error(): never {
    throw new SyntaxError(`Unexpected token in JSON at position ${this.i}`)
  }

  private space() {
    while (this.i < this.text.length && ' \t\n\r'.includes(this.text[this.i]!)) this.i++
  }

  private value(): unknown {
    this.space()
    const c = this.text[this.i]
    if (c === '{') return this.object()
    if (c === '[') return this.array()
    if (c === '"') return this.string()
    if (c === '-' || (c !== undefined && c >= '0' && c <= '9')) return this.number()
    for (const [word, v] of [['true', true], ['false', false], ['null', null]] as const) {
      if (this.text.startsWith(word, this.i)) { this.i += word.length; return v }
    }
    return this.error()
  }

  private object(): Record<string, unknown> {
    const out: Record<string, unknown> = {}
    this.i++
    this.space()
    if (this.text[this.i] === '}') { this.i++; return out }
    for (;;) {
      this.space()
      if (this.text[this.i] !== '"') this.error()
      const key = this.string()
      this.space()
      if (this.text[this.i] !== ':') this.error()
      this.i++
      Object.defineProperty(out, key, { value: this.value(), enumerable: true, writable: true, configurable: true })
      this.space()
      const c = this.text[this.i++]
      if (c === '}') return out
      if (c !== ',') { this.i--; this.error() }
    }
  }

  private array(): unknown[] {
    const out: unknown[] = []
    this.i++
    this.space()
    if (this.text[this.i] === ']') { this.i++; return out }
    for (;;) {
      out.push(this.value())
      this.space()
      const c = this.text[this.i++]
      if (c === ']') return out
      if (c !== ',') { this.i--; this.error() }
    }
  }

  private string(): string {
    STRING.lastIndex = this.i
    const m = STRING.exec(this.text)
    if (m === null) this.error()
    this.i += m[0].length
    return JSON.parse(m[0]) as string
  }

  private number(): number | bigint {
    NUMBER.lastIndex = this.i
    const m = NUMBER.exec(this.text)
    if (m === null) this.error()
    this.i += m[0].length
    const n = Number(m[0])
    if (m[1] === undefined && m[2] === undefined && !Number.isSafeInteger(n)) return BigInt(m[0])
    return n
  }
}
