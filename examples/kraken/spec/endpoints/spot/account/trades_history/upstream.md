# Get Trades History

`POST /private/TradesHistory` -- retrieves trading history/fills, most recent first by
default. Requires `Orders and trades - Query closed orders & trades`.

Costs, fees, prices, and volumes are specified with the asset pair's precision
(`pair_decimals`/`lot_decimals`), not the individual assets' own precision.

## Pagination

`ofs` offsets into the result set; `limit` (1-100, default 50, clamped above 100) sets
the page size -- the one Account Data list endpoint with an explicit page-size
parameter (`ClosedOrders`/`Ledgers` fix it at 50). `count` in the response is the total
number of trades matching the filters and the pagination terminator;
`without_count: true` skips computing it for performance.

## Example response

```json
{
  "error": [],
  "result": {
    "trades": {
      "THVRQM-33VKH-UCI7BS": {
        "ordertxid": "OQCLML-BW3P3-BUCMWZ", "postxid": "TKH2SE-M7IF5-CFI7LT", "pair": "XXBTZUSD",
        "time": 1688667796.8802, "type": "buy", "ordertype": "limit", "price": "30010.00000",
        "cost": "600.20000", "fee": "0.00000", "vol": "0.02000000", "margin": "0.00000",
        "misc": "", "trade_id": 40274859, "maker": true
      }
    }
  }
}
```

Reference: [Get Trades History](https://docs.kraken.com/api-reference/account-data/get-trades-history)
