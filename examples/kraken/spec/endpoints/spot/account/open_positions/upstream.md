# Get Open Positions

`POST /private/OpenPositions` -- retrieves open margin positions. Requires
`Orders and trades - Query open orders & trades`.

`docalcs: true` adds `value`/`net` (current valuation and unrealised P&L); without it
only the opening details are returned. `consolidation: "market"` consolidates positions
by trading pair.

## Example response

```json
{
  "error": [],
  "result": {
    "TF5GVO-T7ZZ2-6NBKBI": {
      "ordertxid": "OLWNFG-LLH4R-D6SFFP", "posstatus": "open", "pair": "XXBTZUSD",
      "time": 1605280097.8294, "type": "buy", "ordertype": "limit",
      "cost": "104610.52842", "fee": "289.06565", "vol": "8.82412861", "vol_closed": "0.20200000",
      "margin": "20922.10568", "value": "258797.5", "net": "+154186.9728",
      "terms": "0.0100% per 4 hours", "rollovertm": "1616672637", "misc": "", "oflags": ""
    }
  }
}
```

Reference: [Get Open Positions](https://docs.kraken.com/api-reference/account-data/get-open-positions)
