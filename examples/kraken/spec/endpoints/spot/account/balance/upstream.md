# Get Account Balance

`POST /private/Balance` -- retrieves all cash balances for the account, net of pending
withdrawals. Requires the `Funds permissions - Query` API key permission.

## Staking/Earn asset migration

Assets migrated from the legacy Staking system to the new Earn system may appear with
symbol extensions:

- `.B` -- new yield-bearing products (similar to `.S`/`.M`)
- `.F` -- balances earning automatically in Kraken Rewards
- `.S` -- legacy staked balances
- `.M` -- legacy opt-in rewards balances
- `.T` -- tokenized assets

These are read-only for transacting -- use the base asset (e.g. `USDT`) to trade both
`USDT` and `USDT.F` balances.

## Example response

```json
{
  "error": [],
  "result": {
    "ZUSD": "171288.6158",
    "ZEUR": "504861.8946",
    "XXBT": "1011.1908877900",
    "XETH": "818.5500000000",
    "USDT": "500000.00000000",
    "ETH2.S": "198.3970800000",
    "USD.M": "1213029.2780"
  }
}
```

Reference: [Get Account Balance](https://docs.kraken.com/api-reference/account-data/get-account-balance)
