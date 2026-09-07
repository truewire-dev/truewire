# Request Withdrawal Cancellation

Cancel a recently requested withdrawal, if it has not already been successfully processed.

**API Key Permissions Required:** `Funds permissions - Withdraw`, unless the withdrawal is a `WalletTransfer` (see [Request Wallet Transfer](./request-wallet-transfer)), in which case no permissions are required.

## Example response

```json
{"error": [], "result": true}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/request-withdrawal-cancellation)
