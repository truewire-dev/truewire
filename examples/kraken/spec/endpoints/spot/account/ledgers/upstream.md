# Get Ledgers Info

`POST /private/Ledgers` -- retrieves ledger entries (trades, deposits, withdrawals,
transfers, staking rewards, etc). 50 results are returned at a time, the most recent
first by default. Requires `Data - Query ledger entries`.

## Pagination

Same shape as `ClosedOrders`: `ofs` offsets a fixed 50-row page (no size parameter);
`count` in the response is the total number of matching entries and the pagination
terminator, omitted when `without_count: true` is passed for performance.

## Staking/Earn assets

`.B`/`.F` symbol extensions from the legacy Staking -> Earn migration are read-only in
the ledger too -- use the base asset (e.g. `USDT`) to transact.

## Example response

```json
{
  "error": [],
  "result": {
    "ledger": {
      "L4UESK-KG3EQ-UFO4T5": {
        "refid": "TJKLXF-PGMUI-4NTLXU", "time": 1688464484.1787, "type": "trade", "subtype": "",
        "aclass": "currency", "asset": "ZGBP", "amount": "-24.5000", "fee": "0.0490", "balance": "459567.9171"
      }
    },
    "count": 2
  }
}
```

Reference: [Get Ledgers Info](https://docs.kraken.com/api-reference/account-data/get-ledgers-info)
