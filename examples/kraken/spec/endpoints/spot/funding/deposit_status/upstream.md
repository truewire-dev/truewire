# Get Status of Recent Deposits

Retrieve information about recent deposits, sorted by recency. Requires the `Funds permissions - Query` API key permission.

## Pagination

The `cursor` parameter is a boolean/string union: `false` (the default) returns a plain array of deposit records with no pagination. Setting `cursor: true` switches the response shape to `{"deposit": [...], "next_cursor": "..."}`; pass the returned `next_cursor` back as `cursor` to fetch the next page (page size follows `limit`, default 25).

## Example response (paginated off)

```json
{
  "error": [],
  "result": [
    {"method": "Bitcoin", "aclass": "currency", "asset": "XXBT", "refid": "FTQcuak-V6Za8qrWnhzTx67yYHz8Tg",
     "txid": "6544b41b607d8b2512baf801755a3a87b6890eacdb451be8a94059fb11f0a8d9",
     "info": "2Myd4eaAW96ojk38A2uDK4FbioCayvkEgVq", "amount": "0.78125000", "fee": "0.0000000000",
     "time": 1688992722, "status": "Success", "status-prop": "return"}
  ]
}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-status-of-recent-deposits)
