# Get Order Book

[`GET /public/Depth`](https://docs.kraken.com/api-reference/market-data/get-order-book)

The public L2 order book: aggregated price levels, not individual orders (compare
`/private/Level3`, which is per-order and requires auth). `pair` required; `count`
(1-500, default 100) caps levels per side. Public, unauthenticated.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBTZUSD": {
    "asks": [["30384.10000", "2.059", 1688671659]],
    "bids": [["30297.00000", "1.115", 1688671636]]
  }
}
```

Each level is `[price, volume, timestamp]`.
