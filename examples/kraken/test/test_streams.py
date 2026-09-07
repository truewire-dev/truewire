"""Handwritten WebSocket coverage: subscriptions, WS trading RPCs, and the two
`raw_request`-based commands (`ping`, `batch_cancel`) whose reply isn't nested under
`result` -- none of this is reachable from the generic HTTP-only replay test.
"""

import pytest


@pytest.mark.asyncio
async def test_public_ticker_subscription(client):
  """A public channel subscription: reply + one push, over the market-data socket.

  Uses `StreamManager`'s "legacy" `await manager` form rather than `async with manager
  as stream:` -- the latter reliably hangs under pytest specifically (reproduced outside
  this client entirely, with a minimal `truewire_core.ws.Socket`-based client against
  `truewire.mock`'s WS server; plain `python` never reproduces it). Tracked as a gap in
  the final report rather than a kraken-specific bug to fix here.
  """
  async with client:
    stream = await client.streams.market_data.ticker(symbol=['BTC/USD'])
    message = await anext(aiter(stream))
    assert message['channel'] == 'ticker'
    assert message['data'][0]['symbol'] == 'BTC/USD'


@pytest.mark.asyncio
async def test_private_balances_subscription(client):
  """A private channel subscription: token-auth path, over the private socket."""
  async with client:
    stream = await client.streams.private.balances()
    message = await anext(aiter(stream))
    assert message['channel'] == 'balances'
    assert message['data']


@pytest.mark.asyncio
async def test_ws_add_order(client):
  """A WS trading RPC (`envelope` declared): reply nested under `result`, via `request`."""
  async with client:
    result = await client.trading_ws.add_order({
      'symbol': 'XBT/USDC',
      'side': 'buy',
      'order_type': 'limit',
      'order_qty': 0.0001,
      'limit_price': 10000.0,
    })
    assert result['order_id']


@pytest.mark.asyncio
async def test_ws_ping_whole_frame(client):
  """`ping` declares no `envelope` -- its reply is the whole frame, via `raw_request`,
  not `request`'s `.get('result')` (which would silently return `None` here).
  """
  async with client:
    reply = await client.streams.market_data.ping()
    assert reply['method'] == 'pong'
    assert reply['time_in']
    assert reply['time_out']


@pytest.mark.asyncio
async def test_ws_batch_cancel_whole_frame(client):
  """`batch_cancel` also declares no `envelope`: unlike every sibling trading command,
  its real reply nests `orders_cancelled` at the top level, not under `result`.
  """
  async with client:
    reply = await client.trading_ws.batch_cancel(
      orders=['OOWVMC-7HDFH-B7UPWM', 'O7L6T4-QEGI4-7M4PUY'],
    )
    assert reply['orders_cancelled'] == 2
