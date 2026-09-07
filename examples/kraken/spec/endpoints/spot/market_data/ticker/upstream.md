# Get Ticker Information

[`GET /public/Ticker`](https://docs.kraken.com/api-reference/market-data/get-ticker-information)

Returns a ticker snapshot per pair: best ask/bid with lot volumes, last trade, and
today's/24h volume, VWAP, trade count, low, high, and open. Public, unauthenticated.
Today's window resets at midnight UTC, not per-caller timezone. Omitting `pair` returns
every tradeable pair. All price/volume fields are strings, not numbers.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBTZUSD": {
    "a": ["30300.10000", "1", "1.000"],
    "b": ["30300.00000", "1", "1.000"],
    "c": ["30303.20000", "0.00067643"],
    "v": ["4083.67001100", "4412.73601799"],
    "p": ["30706.77771", "30689.13205"],
    "t": [34619, 38907],
    "l": ["29868.30000", "29868.30000"],
    "h": ["31631.00000", "31631.00000"],
    "o": "30502.80000"
  }
}
```
