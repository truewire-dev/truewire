# Request Wallet Transfer

Transfer from a Kraken Spot wallet to a Kraken Futures wallet.

**API Key Permissions Required:** `Funds permissions - Query`

Note: a transfer in the other direction (Futures -> Spot) must be requested via the Kraken Futures API's withdrawal-to-Spot-wallet endpoint instead -- this operation only reaches one direction.

## Example response

```json
{"error": [], "result": {"refid": "FTQcuak-V6Za8qrWnhzTx67yYHz8Tg"}}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/request-wallet-transfer)
