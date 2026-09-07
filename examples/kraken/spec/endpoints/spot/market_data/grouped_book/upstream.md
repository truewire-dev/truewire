# Get Grouped Order Book

[`GET /public/GroupedBook`](https://docs.kraken.com/api-reference/market-data/get-grouped-order-book)

Aggregates order book volume across a tick range for coarser, UI-friendly liquidity
summaries -- distinct from `/public/Depth`'s raw per-level book. `pair` required;
`depth` (10/25/100/250/1000, default 10) sets levels per side; `grouping` (1 through
1000, default 1) sets how many ticks fold into one grouped level. Bids round down to
the nearest grouped level, asks round up. Public, unauthenticated.

Example response (`result`, already unwrapped):

```json
{
  "pair": "BTC/USD",
  "grouping": 1000,
  "bids": [{"price": "90400.00000", "qty": "19.83057746"}],
  "asks": [{"price": "90500.00000", "qty": "38.96185061"}]
}
```
