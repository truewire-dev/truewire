# Query Ledgers

`POST /private/QueryLedgers` -- retrieves specific ledger entries, by ledger ID
(comma-delimited, 20 maximum). Requires `Data - Query ledger entries`.

Same entry shape as `Ledgers`, without the enclosing `ledger`/`count` wrapper -- `result`
maps ledger ID directly to entry.

## Example response

```json
{
  "error": [],
  "result": {
    "L4UESK-KG3EQ-UFO4T5": {
      "refid": "TJKLXF-PGMUI-4NTLXU", "time": 1688464484.1787, "type": "trade", "subtype": "",
      "aclass": "currency", "asset": "ZGBP", "amount": "-24.5000", "fee": "0.0490", "balance": "459567.9171"
    }
  }
}
```

Reference: [Query Ledgers](https://docs.kraken.com/api-reference/account-data/query-ledgers)
