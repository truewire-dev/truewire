# Query Trades Info

`POST /private/QueryTrades` -- retrieves information about specific trades/fills, by
transaction ID (comma-delimited, up to 20 maximum). Requires
`Orders and trades - Query closed orders & trades`.

Same trade shape as `TradesHistory`, without the enclosing `count`/`trades` wrapper --
`result` maps txid directly to trade.

## Example response

```json
{
  "error": [],
  "result": {
    "THVRQM-33VKH-UCI7BS": {
      "ordertxid": "OQCLML-BW3P3-BUCMWZ", "postxid": "TKH2SE-M7IF5-CFI7LT", "pair": "XXBTZUSD",
      "time": 1688667796.8802, "type": "buy", "ordertype": "limit", "price": "30010.00000",
      "cost": "600.20000", "fee": "0.00000", "vol": "0.02000000", "margin": "0.00000",
      "misc": "", "trade_id": 93748276, "maker": true
    }
  }
}
```

Reference: [Query Trades Info](https://docs.kraken.com/api-reference/account-data/query-trades-info)
