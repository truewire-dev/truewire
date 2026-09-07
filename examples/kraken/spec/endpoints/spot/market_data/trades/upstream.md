# Get Recent Trades

[`GET /public/Trades`](https://docs.kraken.com/api-reference/market-data/get-recent-trades)

Returns up to 1000 recent trades for a pair, oldest first. `pair` required; `since`
(an opaque numeric-string cursor, `result.last`'s value) polls forward incrementally;
`count` (1-1000, default 1000) caps the batch. Public, unauthenticated.

Each trade row is `[price, volume, time, buy/sell, market/limit, miscellaneous,
trade_id]`. The side/order-type codes are documented explicitly: `b`/`s` for
buy/sell, `m`/`l` for market/limit.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBTZUSD": [
    ["30243.40000", "0.34507674", 1688669597.8277369, "b", "m", "", 61044952]
  ],
  "last": "1688671969993150842"
}
```
