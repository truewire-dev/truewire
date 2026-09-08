/**
 * The generated codecs against the recordings themselves, without the mock: every
 * recorded `*.messages.json` stream capture decodes through its endpoint's message codec
 * (timestamps become `Date`s, the channel literal is checked), a REST `result` round-trips
 * through `parse` and `dump`, and a mismatch names its path.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { ValidationError, type Codec } from '@truewire/core'
import { Response as Ticker } from '../src/kraken/spot/market_data/ticker.js'
import { ServerTime } from '../src/kraken/spot/market_data/time.js'
import { BookMessage } from '../src/kraken/streams/market_data/book.js'
import { InstrumentMessage } from '../src/kraken/streams/market_data/instrument.js'
import { OhlcMessage } from '../src/kraken/streams/market_data/ohlc.js'
import { TickerMessage } from '../src/kraken/streams/market_data/ticker.js'
import { TradeMessage } from '../src/kraken/streams/market_data/trade.js'
import { Payload as BalancesMessage } from '../src/kraken/streams/private/balances.js'
import { ExecutionsMessage } from '../src/kraken/streams/private/executions.js'
import { projectRoot } from './setup.js'

const recorded = (endpoint: string, file: string): unknown =>
  JSON.parse(readFileSync(path.join(projectRoot, 'spec', 'endpoints', endpoint, 'examples', file), 'utf8'))

const result = (endpoint: string): unknown =>
  (recorded(endpoint, 'default.response.json') as { payload: { result: unknown } }).payload.result

/** One recorded stream capture: every message decodes through the channel's codec and survives a round trip. */
function decodes<T extends { channel: string }>(endpoint: string, channel: string, codec: Codec<T>): [string, () => void] {
  return [endpoint, () => {
    const messages = recorded(endpoint, 'default.messages.json') as unknown[]
    expect(messages.length).toBeGreaterThan(0)
    for (const wire of messages) {
      const message = codec.parse(wire)
      expect(message.channel).toBe(channel)
      // `dump` renders the typed value back to the wire; sub-millisecond digits of a
      // timestamp are the one thing a `Date` cannot keep, so compare after a re-parse.
      expect(codec.parse(codec.dump(message))).toEqual(message)
    }
  }]
}

describe('recorded stream messages decode through their message codec', () => {
  for (const [endpoint, check] of [
    decodes('streams/market_data/book', 'book', BookMessage),
    decodes('streams/market_data/instrument', 'instrument', InstrumentMessage),
    decodes('streams/market_data/ohlc', 'ohlc', OhlcMessage),
    decodes('streams/market_data/ticker', 'ticker', TickerMessage),
    decodes('streams/market_data/trade', 'trade', TradeMessage),
    decodes('streams/private/balances', 'balances', BalancesMessage),
    decodes('streams/private/executions', 'executions', ExecutionsMessage),
  ]) {
    it(endpoint, check)
  }

  it('ticker pushes carry Dates', () => {
    const [snapshot] = recorded('streams/market_data/ticker', 'default.messages.json') as unknown[]
    const message = TickerMessage.parse(snapshot)
    expect(message.type).toBe('snapshot')
    expect(message.data[0]!.timestamp).toBeInstanceOf(Date)
    expect(message.data[0]!.timestamp.toISOString()).toBe('2026-08-13T12:07:04.018Z')
  })
})

describe('REST results', () => {
  it('parse a recorded ticker and dump it back verbatim', () => {
    const payload = result('spot/market_data/ticker')
    const ticker = Ticker.parse(payload)
    expect(Object.keys(ticker)).toEqual(['XXBTZUSD'])
    expect(Ticker.dump(ticker)).toEqual(payload)
  })

  it('parse the server time', () => {
    const time = ServerTime.parse(result('spot/market_data/time'))
    expect(time.unixtime).toBeTypeOf('number')
    expect(time.rfc1123).toBeTypeOf('string')
  })

  it('name the path of a field that does not match', () => {
    const [snapshot] = recorded('streams/market_data/ticker', 'default.messages.json') as { data: object[] }[]
    const broken = { ...snapshot, data: [{ ...snapshot!.data[0], bid: 'high' }] }
    expect(() => TickerMessage.parse(broken)).toThrow(ValidationError)
    try {
      TickerMessage.parse(broken)
    } catch (e) {
      expect((e as ValidationError).path).toBe('/data/0/bid')
    }
  })
})
