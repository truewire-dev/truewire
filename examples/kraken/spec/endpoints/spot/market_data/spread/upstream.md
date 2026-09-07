# Get Recent Spreads

[`GET /public/Spread`](https://docs.kraken.com/api-reference/market-data/get-recent-spreads)

Returns roughly the last 200 top-of-book bid/ask spreads for a pair. `pair` required;
`since` polls incrementally but explicitly does **not** hold the full historical
spread series -- there can be gaps. Public, unauthenticated.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBTZUSD": [
    [1688671834, "30292.10000", "30297.50000"]
  ],
  "last": 1688672106
}
```

Each row is `[time, bid, ask]`.
