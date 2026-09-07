# Get Withdrawal Methods

Retrieve a list of withdrawal methods available for the user.

**API Key Permissions Required:** `Funds permissions - Query` and `Funds permissions - Withdraw`

Each method carries a flat `fee` or `fee_percentage`, a `minimum`, and optionally a set of `limits` -- rate limit rules keyed by time window (in seconds), each reporting `maximum`/`remaining`/`used`.

## Example response

```json
{
  "error": [],
  "result": [
    {"asset": "XXBT", "method": "Bitcoin", "method_id": "12fca2ad-edae-4d8c-acbb-4a424c1fbdeb",
     "network": "Bitcoin", "network_id": "ee9d686d-aeb6-4e61-9d83-448e3a7511f3", "minimum": "0.0004",
     "fee": {"aclass": "currency", "asset": "XXBT", "fee": "0.00001500"}}
  ]
}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-withdrawal-methods)
