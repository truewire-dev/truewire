"""Real, representative usage of `kraken`'s public surface, type-checked by
pyright (`clients/kraken/pyrightconfig.json`) as this client's guardrail against a
future change silently degrading a public return type to `Any`
(`docs/production_standards.md` S17). Never executed -- only type-checked.

The one call that is `Any` on purpose is `validate=False`: it returns the body as the
wire sent it, and its overload says so (`reveal_type(..., expected_text='Any')` below).
"""

from decimal import Decimal

from typing_extensions import reveal_type

from kraken import Kraken
from kraken.spot.market_data.ticker import AssetTicker
from kraken.spot.market_data.time import ServerTime
from kraken.spot.market_data.depth import OrderBook
from kraken.spot.trading.add_order import AddOrderLimit, AddOrderMarket, OrderAdded
from kraken.trading_ws.add_order import WsAddOrderLimit, WsAddOrderMarket, AddOrderResult
from kraken.trading_ws.batch_add import WsBatchOrderMarket


async def constructors() -> None:
  """`Kraken.new()`'s two credential modes, and its three composed surfaces."""
  from kraken.spot import Spot
  from kraken.streams import Streams
  from kraken.trading_ws import TradingWs

  public_client = Kraken.new(public=True)
  authed_client = Kraken.new(
    api_key='fake-api-key', private_key='fake-private-key', validate=True
  )

  async with public_client as client:
    spot: Spot = client.spot
    streams: Streams = client.streams
    trading_ws: TradingWs = client.trading_ws
    print(spot, streams, trading_ws)

  async with authed_client as client:
    authed_spot: Spot = client.spot
    print(authed_spot)


async def market_data() -> None:
  """Public Spot REST -- no credentials required, `validate` overridable per call."""
  async with Kraken.new(public=True) as client:
    ticker: dict[str, AssetTicker] = await client.spot.market_data.ticker(pair='XBTUSD')
    for symbol, snapshot in ticker.items():
      last_trade = snapshot.get('c')
      if last_trade is not None:
        print(symbol, last_trade[0])

    book: dict[str, OrderBook] = await client.spot.market_data.depth(
      pair='XBTUSD', count=10
    )
    asks = book['XBTUSD'].get('asks') or []
    for price, volume, timestamp in asks:
      print(price, volume, timestamp)

    server_time: ServerTime = await client.spot.market_data.time()
    print(server_time.get('unixtime'))

    raw_time = await client.spot.market_data.time(validate=False)
    reveal_type(raw_time, expected_text='Any')
    reveal_type(
      client.spot.account.trades_history_paged(validate=False),
      expected_text='AsyncIterator[Any]',
    )


async def account_data() -> None:
  """Private Spot REST -- signed, real credentials required."""
  async with Kraken.new(
    api_key='fake-api-key', private_key='fake-private-key'
  ) as client:
    balances: dict[str, Decimal] = await client.spot.account.balance()
    print(balances.get('ZUSD'))

    trade_balance = await client.spot.account.trade_balance(asset='ZUSD')
    print(trade_balance)


async def rest_add_order_variants() -> None:
  """`spot.trading.add_order` takes a single discriminated-union `order` parameter --
  exercised here across two different `ordertype` branches to prove the union
  type-checks correctly for each, and that its wire `validate` (dry-run) field and the
  method's own `validate` (response-validation) kwarg are two distinct, non-colliding
  arguments.
  """
  async with Kraken.new(
    api_key='fake-api-key', private_key='fake-private-key'
  ) as client:
    market_order: AddOrderMarket = {
      'pair': 'XBTUSD',
      'type': 'buy',
      'ordertype': 'market',
      'volume': '0.001',
      'validate': True,
      # ^ Kraken's own dry-run flag: a real `AddOrderMarket` field, distinct from the
      # method's own `validate=` kwarg below.
    }
    market_result = await client.spot.trading.add_order(market_order, validate=False)
    reveal_type(market_result, expected_text='Any')
    print(market_result.get('txid'))

    limit_order: AddOrderLimit = {
      'pair': 'XBTUSD',
      'type': 'sell',
      'ordertype': 'limit',
      'volume': '0.001',
      'price': '65000',
    }
    limit_result: OrderAdded = await client.spot.trading.add_order(limit_order)
    print(limit_result.get('descr'))

    await client.spot.trading.cancel_order(txid='OABCDE-12345-67890X')


async def ws_add_order_variants() -> None:
  """`trading_ws.add_order`/`batch_add`'s WS twins: the same `order` union parameter,
  plus `effective_time`/`expire_time` (`TimestampIso`, i.e. real `datetime` values) on
  the variant `TypedDict`s -- proving those fields type-check as `datetime`, not a bare
  wire string.
  """
  from datetime import datetime, timezone

  async with Kraken.new(
    api_key='fake-api-key', private_key='fake-private-key'
  ) as client:
    ws_market_order: WsAddOrderMarket = {
      'order_type': 'market',
      'side': 'buy',
      'order_qty': 0.001,
      'symbol': 'BTC/USD',
      'validate': True,
      # ^ Kraken's dry-run flag again -- still a plain dict field here, still distinct
      # from `add_order`'s own `validate=` kwarg below.
    }
    dry_run_result = await client.trading_ws.add_order(ws_market_order, validate=False)
    reveal_type(dry_run_result, expected_text='Any')
    print(dry_run_result.get('order_id'))

    ws_limit_order: WsAddOrderLimit = {
      'order_type': 'limit',
      'side': 'sell',
      'order_qty': 0.001,
      'symbol': 'BTC/USD',
      'limit_price': 65000.0,
      'expire_time': datetime.now(timezone.utc),
    }
    live_result: AddOrderResult = await client.trading_ws.add_order(ws_limit_order)
    print(live_result.get('cl_ord_id'))

    batch_order: WsBatchOrderMarket = {
      'order_type': 'market',
      'side': 'buy',
      'order_qty': 0.001,
    }
    await client.trading_ws.batch_add(
      symbol='BTC/USD', orders=[batch_order], validate_=True, validate=False
    )

    await client.trading_ws.cancel_order(order_id=['OABCDE-12345-67890X'])


async def streams() -> None:
  """Public and private WebSocket channel subscriptions -- `StreamManager`, usable both
  as `async with ... as stream:` and as a bare `await`ed `Stream`.
  """
  async with Kraken.new(public=True) as client:
    async with client.streams.market_data.ticker(symbol=['BTC/USD']) as ticker_stream:
      async for message in ticker_stream:
        print(message)
        break

  async with Kraken.new(
    api_key='fake-api-key', private_key='fake-private-key'
  ) as client:
    balances_manager = client.streams.private.balances(snapshot=True)
    balances_stream = await balances_manager
    async for update in balances_stream:
      print(update)
      break
    await balances_stream.unsubscribe()

    pong = await client.streams.market_data.ping()
    print(pong)
