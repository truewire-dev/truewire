# Get API Key Info

`POST /private/GetApiKeyInfo` -- retrieves information about the API key used to make
the request: name, permissions, restrictions, and usage timestamps. Requires no
specific API key permission.

`otp` is only needed if the key has 2FA configured. `permissions` lists the granted
permission codes (see the 13-value table in the endpoint description); `ipAllowlist`
restricts which source IPs may use the key (empty means unrestricted).

## Example response

```json
{
  "error": [],
  "result": {
    "apiKeyName": "my-api-key",
    "apiKey": "4/SDrDBcOOPnm3nPlNfEMMJDeRcIVqPz+QhRxIodyZbI9po/aVRiHsgX",
    "nonce": "1772627060997",
    "nonceWindow": 0,
    "permissions": ["query-funds", "withdraw-funds", "query-open-trades", "modify-trades"],
    "iban": "AA88 N84G WOAK NMOI",
    "validUntil": "0", "queryFrom": "0", "queryTo": "0",
    "createdTime": "1772542900", "modifiedTime": "1772543095",
    "ipAllowlist": [], "lastUsed": "1772627061"
  }
}
```

Reference: [Get API Key Info](https://docs.kraken.com/api-reference/account-data/get-api-key-info)
