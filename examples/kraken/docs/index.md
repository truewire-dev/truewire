# Kraken

Kraken is a cryptocurrency exchange. Its Spot API has three parts, and this client covers
all three:

| Surface | Transport | What it does |
| --- | --- | --- |
| `spot` | REST | market data, account, trading, funding and Earn |
| `streams` | WebSocket v2 | public market data channels, and private balances and executions |
| `trading_ws` | WebSocket v2 | placing, amending and cancelling orders over a socket |

Public calls need no key. Private calls sign each request with your key pair (see
[API keys](api-keys.md)).

## Why a validated client

Kraken's responses are easy to misread. Prices and volumes arrive as strings, tickers as
positional tuples, and REST errors as an `error` list inside an HTTP 200. The client
unwraps the envelope and raises on a non-empty `error`. It gives every field its real
type: a balance is a `Decimal`, a timestamp a `datetime`. It also checks each response
against its schema, so a change on Kraken's side fails at the call, not three functions
later. Pass `validate=False` to get the body as Kraken sent it.

## First call

```python
import asyncio

from kraken import Kraken


async def main() -> None:
  async with Kraken.new(public=True) as client:
    ticker = await client.spot.market_data.ticker(pair='XBTUSD')
    for pair, snapshot in ticker.items():
      last = snapshot.get('c')
      if last is not None:
        print(pair, 'last trade', last[0])


asyncio.run(main())
```

The same call in TypeScript, Rust and Go is in the quickstart (`docs.yml`).
