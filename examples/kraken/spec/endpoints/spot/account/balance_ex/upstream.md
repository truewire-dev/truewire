# Get Extended Balance

`POST /private/BalanceEx` -- retrieves all extended account balances, including total
balance, available credit, used credit, and held amounts, per asset. Requires the
`Funds permissions - Query` API key permission.

Available balance for trading is calculated as:

```
available balance = balance + credit - credit_used - hold_trade
```

`credit`/`credit_used` only appear for accounts with an active credit line (see
`CreditLines`). `hold_trade` only reflects spot non-margin orders; margin positions are
not included.

## Example response

```json
{
  "error": [],
  "result": {
    "ZUSD": {"balance": "25435.21", "hold_trade": "8249.76"},
    "XXBT": {"balance": "1.2435", "hold_trade": "0.8423"}
  }
}
```

Reference: [Get Extended Balance](https://docs.kraken.com/api-reference/account-data/get-extended-balance)
