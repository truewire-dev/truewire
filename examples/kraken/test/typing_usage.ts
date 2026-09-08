/**
 * Real, representative usage of the generated client, type-checked by `yarn typecheck`
 * and never run: the guardrail against a public return type silently degrading, and the
 * proof that `{ validate: false }` is typed as what it returns, on streams as on calls.
 *
 * `expectTypeOf` is vitest's compile-time assertion; a mismatch fails `tsc`.
 */
import type { Subscription } from '@truewire/core'
import { expectTypeOf } from 'vitest'
import { Core } from '../src/kraken/core/index.js'
import { Kraken, type KrakenCore } from '../src/kraken/index.js'
import type { ServerTime } from '../src/kraken/spot/market_data/time.js'
import type { TickerMessage } from '../src/kraken/streams/market_data/ticker.js'
import type { Payload as BalancesMessage } from '../src/kraken/streams/private/balances.js'
import type { AddOrderResult } from '../src/kraken/trading_ws/add_order.js'

/** The hand-written core is what the generated root takes, by shape. */
export function construction(): Kraken {
  const core: KrakenCore = new Core({ credentials: { apiKey: 'key', privateKey: 'c2VjcmV0' } })
  return new Kraken(core)
}

/** Subscriptions are typed by their channel's message; `validate: false` makes them `unknown`. */
export async function streams(client: Kraken): Promise<void> {
  expectTypeOf(client.streams.marketData.ticker({ symbol: ['BTC/USD'] })).toEqualTypeOf<Subscription<TickerMessage>>()
  expectTypeOf(client.streams.private.balances()).toEqualTypeOf<Subscription<BalancesMessage>>()
  expectTypeOf(client.streams.marketData.ticker({ symbol: ['BTC/USD'] }, { validate: false })).toEqualTypeOf<Subscription<unknown>>()
  for await (const message of client.streams.marketData.ticker({ symbol: ['BTC/USD'] })) {
    expectTypeOf(message.data[0]!.timestamp).toEqualTypeOf<Date>()
  }
}

/** Calls over both transports return their declared payload. */
export async function calls(client: Kraken): Promise<void> {
  expectTypeOf(await client.spot.marketData.time()).toEqualTypeOf<ServerTime>()
  expectTypeOf(await client.tradingWs.addOrder({ symbol: 'BTC/USD', side: 'buy', order_type: 'market', order_qty: 1 })).toEqualTypeOf<AddOrderResult>()
  expectTypeOf(await client.tradingWs.addOrder({ symbol: 'BTC/USD', side: 'buy', order_type: 'market', order_qty: 1 }, { validate: false })).toBeUnknown()
}
