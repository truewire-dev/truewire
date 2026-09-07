# Get Deposit Addresses

Retrieve (or generate a new) deposit address for a particular asset and method.

**API Key Permissions Required:** `Funds permissions - Query`

`amount` is only required when `method=Bitcoin Lightning`. `new=true` requests a fresh address; default is `false` (return existing addresses). Some assets return additional destination info alongside the address -- `tag` for XRP/STX/XLM/EOS, and a `memo` key was seen in Kraken's own documented example though it is not declared in the schema.

## Example response

```json
{
  "error": [],
  "result": [
    {"address": "2N9fRkx5JTWXWHmXzZtvhQsufvoYRMq9ExV", "expiretm": "0", "new": true},
    {"address": "rLHzPsX3oXdzU2qP17kHCH2G4csZv1rAJh", "expiretm": "0", "new": true, "tag": "1361101127"},
    {"address": "krakenkraken", "expiretm": "0", "memo": "4150096490"}
  ]
}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-deposit-addresses)
