# Trades

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade)

Streams a trade event whenever orders are matched in the book, for one or more subscribed
symbols. Multiple trades may be batched in one push, but that does not mean they resulted
from a single taker order. Requesting a snapshot returns the most recent 50 trades.

## Example

Subscribe:

```json
{"method": "subscribe", "params": {"channel": "trade", "symbol": ["MATIC/USD"], "snapshot": true}}
```

Update push:

```json
{
  "channel": "trade",
  "type": "update",
  "data": [
    {
      "symbol": "MATIC/USD",
      "side": "sell",
      "price": 0.5117,
      "qty": 40.0,
      "ord_type": "market",
      "trade_id": 4665906,
      "timestamp": "2023-09-25T07:49:37.708706Z"
    }
  ]
}
```
