# Withdraw Funds

Make a withdrawal request.

**API Key Permissions Required:** `Funds permissions - Withdraw`

`address` is optional but validated against `key` if supplied -- a mismatch returns `Invalid withdrawal address`. `max_fee` fails the withdrawal with `EFunding:Max fee exceeded` if the processed fee would exceed it. Returns a `refid` that identifies the withdrawal for [WithdrawStatus](./get-status-of-recent-withdrawals) and [WithdrawCancel](./request-withdrawal-cancellation).

## Example response

```json
{"error": [], "result": {"refid": "FTQcuak-V6Za8qrWnhzTx67yYHz8Tg"}}
```

[Upstream docs](https://docs.kraken.com/api-reference/funding/withdraw-funds)
