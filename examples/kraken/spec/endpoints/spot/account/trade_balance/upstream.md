# Get Trade Balance

`POST /private/TradeBalance` -- retrieves a summary of collateral balances, margin
position valuations, equity, and margin level. Requires the
`Orders and trades - Query open orders & trades` API key permission.

`asset` (default `ZUSD`) is the base asset the summary is expressed in. All response
values are decimal strings; `ml` (margin level) is nullable and only present while margin
positions are open.

## Example response

```json
{
  "error": [],
  "result": {
    "eb": "1101.3425", "tb": "392.2264", "m": "7.0354", "n": "-10.0232",
    "c": "21.1063", "v": "31.1297", "e": "382.2032", "mf": "375.1678", "ml": "5432.57"
  }
}
```

Reference: [Get Trade Balance](https://docs.kraken.com/api-reference/account-data/get-trade-balance)
