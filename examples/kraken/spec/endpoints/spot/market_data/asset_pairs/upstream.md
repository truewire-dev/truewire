# Get Tradable Asset Pairs

[`GET /public/AssetPairs`](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)

Per-pair trading metadata: decimal precision, leverage tiers, taker/maker fee
schedules, margin call/stop-out levels, order minimums, tick size, and lifecycle
`status`. Public, unauthenticated. `pair` filters to a comma-delimited list; `info`
selects which subset of fields to return (`info`/`leverage`/`fees`/`margin`).

`status` is documented as exactly five values: `online`, `cancel_only`, `post_only`,
`limit_only`, `reduce_only`. `lot` is present but deprecated.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBTZUSD": {
    "altname": "XBTUSD",
    "wsname": "XBT/USD",
    "aclass_base": "currency",
    "base": "XXBT",
    "aclass_quote": "currency",
    "quote": "ZUSD",
    "lot": "unit",
    "cost_decimals": 5,
    "pair_decimals": 1,
    "lot_decimals": 8,
    "lot_multiplier": 1,
    "leverage_buy": [2, 3, 4, 5],
    "leverage_sell": [2, 3, 4, 5],
    "fees": [[0, 0.26], [50000, 0.24]],
    "fees_maker": [[0, 0.16], [50000, 0.14]],
    "fee_volume_currency": "ZUSD",
    "margin_call": 80,
    "margin_stop": 40,
    "ordermin": "0.0001",
    "costmin": "0.5",
    "tick_size": "0.1",
    "status": "online",
    "long_position_limit": 250,
    "short_position_limit": 200
  }
}
```
