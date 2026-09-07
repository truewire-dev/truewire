# List Earn Strategies

Lists Earn strategies with their parameters. Requires a valid API key but no specific
key permission. Returns only strategies available to the user based on geographic
region; when the account doesn't meet a strategy's tier restriction, `can_allocate` is
`false` and `allocation_restriction_info` names `tier` as the reason (Earn generally
requires Intermediate tier).

**`lock_type` variants:**

- `instant` — can be deallocated without an unbonding period ("flexible" in the UI). A
  special case of `bonded` with no bonding/unbonding period; not to be confused with
  `flex` below.
- `bonded` — explicit allocate/deallocate actions required; bonding/unbonding periods
  and an optional exit-queue period apply.
- `flex` — "Kraken rewards": earning on spot balances where eligible, turned on
  account-wide from the UI. Cannot be manually allocated to.

**Paging is not actually implemented yet** — the docs state the endpoint "always
returns all data in the first page," even though `cursor`/`limit` request fields and a
`next_cursor` response field are documented and accepted.

## Example response

```json
{
  "error": [],
  "result": {
    "next_cursor": "2",
    "items": [
      {
        "id": "ESRFUO3-Q62XD-WIOIL7",
        "asset": "DOT",
        "lock_type": { "type": "instant", "payout_frequency": 604800 },
        "apr_estimate": { "low": "8.0000", "high": "12.0000" },
        "user_min_allocation": "0.01",
        "allocation_fee": "0.0000",
        "deallocation_fee": "0.0000",
        "auto_compound": { "type": "enabled" },
        "yield_source": { "type": "staking" },
        "can_allocate": true,
        "can_deallocate": true,
        "allocation_restriction_info": []
      }
    ]
  }
}
```

[List Earn Strategies](https://docs.kraken.com/api-reference/earn/list-earn-strategies)
