# Get Withdrawal Addresses

Retrieve a list of withdrawal addresses available for the user.

**API Key Permissions Required:** `Funds permissions - Query` and `Funds permissions - Withdraw`

Filters are all optional (`asset`, `aclass`, `method`, `key`, `verified`); with none set, returns every withdrawal address configured on the account.

## Example response

```json
{
  "error": [],
  "result": [
    {"address": "bc1qxdsh4sdd29h6ldehz0se5c61asq8cgwyjf2y3z", "asset": "XBT", "method": "Bitcoin",
     "key": "btc-wallet-1", "verified": true}
  ]
}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/get-withdrawal-addresses)
