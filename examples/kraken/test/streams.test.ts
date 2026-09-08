/**
 * The WebSocket surface against `truewire mock`: channel subscriptions over each socket,
 * the trading methods, and the two whole-frame commands (`ping`, `batch_cancel`) whose
 * reply is not nested under `result`. The same walks as `test/test_streams.py`, through
 * the TypeScript client.
 *
 * A subscription is read, never unsubscribed, as in the Python test: the mock has no
 * recorded unsubscribe ack for these channels and synthesizes one without a `req_id`,
 * which a `req_id`-correlated connection cannot match. `unsubscribe()` and `await using`
 * are proved by `@truewire/core`'s own tests against a fake server.
 */
import { describe, expect, it } from 'vitest'
import type { Reply } from '../src/kraken/core/index.js'
import { withClient } from './client.js'

/** The first message of an async iterable, leaving the iterator where it is. */
async function first<T>(iterable: AsyncIterable<T>): Promise<T> {
  const next = await iterable[Symbol.asyncIterator]().next()
  if (next.done) throw new Error('the stream ended before its first message')
  return next.value
}

describe('channel subscriptions', () => {
  it('a public channel: reply and first push over the market-data socket', async () => {
    await withClient(async (client, transports) => {
      const stream = await client.streams.marketData.ticker({ symbol: ['BTC/USD'] })
      expect((stream.reply as Reply).method).toBe('subscribe')
      expect(transports.market_client.conn.subscriptions.has('ticker')).toBe(true)
      expect(transports.private_client.conn.isOpen).toBe(false)
      const message = await first(stream)
      expect(message.channel).toBe('ticker')
      expect(message.type).toBe('snapshot')
      expect(message.data[0]!.symbol).toBe('BTC/USD')
      expect(message.data[0]!.timestamp).toBeInstanceOf(Date)
    })
  })

  it('a private channel over the private socket', async () => {
    await withClient(async (client, transports) => {
      const message = await first(client.streams.private.balances())
      expect(message.channel).toBe('balances')
      expect(message.data.length).toBeGreaterThan(0)
      expect(transports.private_client.conn.subscriptions.has('balances')).toBe(true)
      expect(transports.market_client.conn.isOpen).toBe(false)
    })
  })

  it('validate: false hands the pushed frame over as it came', async () => {
    await withClient(async client => {
      const message = await first(client.streams.marketData.ticker({ symbol: ['BTC/USD'] }, { validate: false }))
      expect((message as { data: { timestamp: unknown }[] }).data[0]!.timestamp).toBeTypeOf('string')
    })
  })
})

describe('trading methods', () => {
  it('a reply nested under `result`', async () => {
    await withClient(async client => {
      const result = await client.tradingWs.addOrder({
        symbol: 'XBT/USDC', side: 'buy', order_type: 'limit', order_qty: 0.0001, limit_price: 10000.0,
      })
      expect(result.order_id).toBeTruthy()
    })
  })

  it('`ping` answers with the whole `pong` frame', async () => {
    await withClient(async client => {
      const reply = await client.streams.marketData.ping()
      expect(reply.method).toBe('pong')
      expect(reply.time_in).toBeInstanceOf(Date)
      expect(reply.time_out).toBeInstanceOf(Date)
    })
  })

  it('`batch_cancel` reports at the top level of the frame', async () => {
    await withClient(async client => {
      const reply = await client.tradingWs.batchCancel({ orders: ['OOWVMC-7HDFH-B7UPWM', 'O7L6T4-QEGI4-7M4PUY'] })
      expect(reply.orders_cancelled).toBe(2)
    })
  })
})
