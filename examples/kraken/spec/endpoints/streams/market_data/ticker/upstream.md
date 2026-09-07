# Ticker

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ticker)

Streams level 1 market data -- top of book (best bid/offer) plus recent trade data -- for
a list of subscribed currency pairs. By default a new update is published on every trade;
setting `event_trigger: "bbo"` switches that to best-bid-offer changes instead. A snapshot
is sent right after subscribing unless `snapshot: false` is requested.

## Example

Subscribe:

```json
{"method": "subscribe", "params": {"channel": "ticker", "symbol": ["ALGO/USD"]}}
```

Snapshot push:

```json
{
  "channel": "ticker",
  "type": "snapshot",
  "data": [
    {
      "symbol": "ALGO/USD",
      "bid": 0.10025,
      "bid_qty": 740.0,
      "ask": 0.10036,
      "ask_qty": 1361.44813783,
      "last": 0.10035,
      "volume": 997038.98383185,
      "vwap": 0.10148,
      "low": 0.09979,
      "high": 0.10285,
      "change": -0.00017,
      "change_pct": -0.17,
      "timestamp": "2023-09-25T09:04:31.742648Z"
    }
  ]
}
```
