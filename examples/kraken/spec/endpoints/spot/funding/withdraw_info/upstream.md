# Get Withdrawal Information

Retrieve fee information about a potential withdrawal for a particular asset, key, and amount, without submitting it.

**API Key Permissions Required:** `Funds permissions - Query` and `Funds permissions - Withdraw`

## Example response

```json
{"error": [], "result": {"method": "Bitcoin", "limit": "332.00956139", "amount": "0.72485000", "fee": "0.00020000"}}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-withdrawal-information)
