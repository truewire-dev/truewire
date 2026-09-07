# Get Deposit Methods

Retrieve methods available for depositing a particular asset.

**API Key Permissions Required:** `Funds permissions - Query` and `Funds permissions - Deposit`

Each returned method carries an optional flat `fee` or, for some methods (e.g. Bitcoin Lightning), a `fee-percentage` instead. `limit` is either a decimal-string cap or the literal `false` when there is no current limit. `gen-address` says whether new addresses can be generated for that method (see [Get Deposit Addresses](./get-deposit-addresses)); `address-setup-fee` and `fee-percentage` are present only for methods that charge them.

## Example response

```json
{
  "error": [],
  "result": [
    {"method": "Bitcoin", "limit": false, "fee": "0.0000000000", "gen-address": true, "minimum": "0.00010000"},
    {"method": "Bitcoin Lightning", "limit": false, "fee": "0.00000000", "minimum": "0.00010000"}
  ]
}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-deposit-methods)
