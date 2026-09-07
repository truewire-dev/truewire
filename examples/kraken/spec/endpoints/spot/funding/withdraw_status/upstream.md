# Get Status of Recent Withdrawals

Retrieve information about recent withdrawals, sorted by recency. Requires the `Funds permissions - Withdraw` or `Data - Query ledger entries` API key permission.

## Pagination

Prose documents the same `cursor`/`limit` iteration scheme as [DepositStatus](./get-status-of-recent-deposits) ("use the cursor parameter to iterate through list of withdrawals... from newest to oldest"), but the response schema here never documents a `next_cursor` field the way DepositStatus's does -- `result` is always a bare array of withdrawal records. See this endpoint's `notes`.

## Example response

```json
{
  "error": [],
  "result": [
    {"method": "Bitcoin", "aclass": "currency", "asset": "XXBT", "refid": "FTQcuak-V6Za8qrWnhzTx67yYHz8Tg",
     "txid": "29323ce235cee8dae22503caba7....8ad3a506879a03b1e87992923d80428",
     "info": "bc1qm32pq....3ewt0j37s2g", "amount": "0.72485000", "fee": "0.00020000",
     "time": 1688014586, "status": "Pending", "key": "btc-wallet-1"}
  ]
}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-status-of-recent-withdrawals)
