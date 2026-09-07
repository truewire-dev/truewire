# Candles (OHLC)

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ohlc)

Streams Open/High/Low/Close candle data for the requested `interval` (in minutes: one of
1, 5, 15, 30, 60, 240, 1440, 10080, 21600), for one or more subscribed symbols. Updates are
generated on trade events. `timestamp` on each candle is deprecated in favor of
`interval_begin`, which marks the start of the interval unambiguously.

## Example

Subscribe:

```json
{"method": "subscribe", "params": {"channel": "ohlc", "symbol": ["ALGO/USD", "MATIC/USD"], "interval": 5}}
```

Update push:

```json
{
  "channel": "ohlc",
  "type": "update",
  "timestamp": "2023-10-04T16:26:30.524394914Z",
  "data": [
    {
      "symbol": "MATIC/USD",
      "open": 0.5624,
      "high": 0.5628,
      "low": 0.5622,
      "close": 0.5627,
      "trades": 12,
      "volume": 30927.68066226,
      "vwap": 0.5626,
      "interval_begin": "2023-10-04T16:25:00.000000000Z",
      "interval": 5,
      "timestamp": "2023-10-04T16:30:00.000000Z"
    }
  ]
}
```
