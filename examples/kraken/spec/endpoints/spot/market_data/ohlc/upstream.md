# Get OHLC Data

[`GET /public/OHLC`](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)

Candles for a pair at one of nine fixed intervals (1, 5, 15, 30, 60, 240, 1440, 10080,
21600 minutes). Public, unauthenticated, `pair` required.

Two gotchas called out explicitly by the docs:

- **The last candle is always the current, not-yet-committed one** — present regardless
  of `since`, and still forming.
- **Only the 720 most recent candles are ever retrievable.** `since` is for incremental
  polling (skip candles already seen), not for paging further back in history; older
  data cannot be retrieved no matter what `since` is set to. That's why this endpoint
  gets no `pagination` block in the spec.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBTZUSD": [
    [1688671200, "30306.1", "30306.2", "30305.7", "30305.7", "30306.1", "3.39243896", 23]
  ],
  "last": 1688672160
}
```

Row shape: `[time, open, high, low, close, vwap, volume, count]`.
