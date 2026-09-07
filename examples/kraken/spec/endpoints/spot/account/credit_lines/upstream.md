# Get Credit Lines

`POST /private/CreditLines` -- retrieves all credit line details for VIP accounts with
this functionality. Requires the `Funds permissions - Query` API key permission.

`asset_details` is a per-asset breakdown (`balance`, `hold_trade`, plus
`collateral_value`/`credit_limit`/`credit_used`/`available_credit` for eligible/credit-line
accounts); `limits_monitor` is a portfolio-wide USD summary. Only accessible to VIP
accounts with credit lines -- most fields are absent (or `result` itself is `null`) for
accounts without one.

## Example response

```json
{
  "error": [],
  "result": {
    "asset_details": {
      "USD": {"balance": "1000.5000", "credit_limit": "50000.0000", "credit_used": "12500.0000", "available_credit": "37500.0000"}
    },
    "limits_monitor": {
      "total_credit_usd": "100000.0000",
      "total_credit_used_usd": "25000.0000",
      "total_collateral_value_usd": "150000.0000",
      "equity_usd": "125000.0000",
      "ongoing_balance": "1.5000",
      "debt_to_equity": "0.2000"
    }
  }
}
```

Reference: [Get Credit Lines](https://docs.kraken.com/api-reference/account-data/get-credit-lines)
