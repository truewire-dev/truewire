# List Earn Allocations

Lists all of the user's Earn allocations. Requires the `Query Funds` API key
permission. Returns every strategy the account has ever allocated to by default,
including zero-balance ones, so past earnings stay visible; `hide_zero_allocations`
drops zero-balance entries. Amounts can be expressed in a secondary currency via
`converted_asset` (defaults to USD). Paging is explicitly **not implemented** for this
endpoint — Kraken doesn't expect a single user's allocation list to be large.

Allocated funds move through up to four states: `bonding`, `allocated` (implicit — any
of `total` not otherwise accounted for), `exit_queue` (ETH only), `unbonding`.
Whether `bonding`/`unbonding` funds earn rewards depends on the strategy (see
`spot.earn.strategies`); ETH in `exit_queue` still earns rewards. For ETH, an
`exit_queue` entry's `expires` is when unbonding finishes, not when it leaves the
queue. Bonding/unbonding time estimates can be inaccurate for 1-2 minutes right after
a (de)allocation.

## Example response

```json
{
  "error": [],
  "result": {
    "converted_asset": "USD",
    "total_allocated": "49.2398",
    "total_rewarded": "0.0675",
    "items": [
      {
        "strategy_id": "ESDQCOL-WTZEU-NU55QF",
        "native_asset": "ETH",
        "amount_allocated": {
          "bonding": {
            "native": "0.0210000000",
            "converted": "39.0645",
            "allocation_count": 2,
            "allocations": [
              {
                "created_at": "2023-07-06T10:52:05Z",
                "expires": "2023-08-19T02:34:05.807Z",
                "native": "0.0010000000",
                "converted": "1.8602"
              },
              {
                "created_at": "2023-08-01T11:25:52Z",
                "expires": "2023-09-06T07:55:52.648Z",
                "native": "0.0200000000",
                "converted": "37.2043"
              }
            ]
          },
          "total": { "native": "0.0210000000", "converted": "39.0645" }
        },
        "total_rewarded": { "native": "0", "converted": "0.0000" }
      }
    ]
  }
}
```

Note: upstream's own worked example also shows a top-level `"next_cursor": "2"` inside
`result`, but that field is not declared anywhere in the response schema for this
endpoint (unlike `Strategies`, which genuinely has one) — most likely copy-pasted from
the sibling endpoint's example. Left out of this endpoint's schema and out of the JSON
above.

[List Earn Allocations](https://docs.kraken.com/api-reference/earn/list-earn-allocations)
