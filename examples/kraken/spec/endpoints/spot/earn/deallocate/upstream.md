# Deallocate Earn Funds

Deallocates funds from a strategy. Requires the `Earn Funds` API key permission;
`amount` must always be given.

**Asynchronous**, same shape as `Allocate`: the client must poll
`spot.earn.deallocate_status` for the outcome. Only one (de)allocation can be in
flight per user/strategy pair; while in flight, `spot.earn.allocations`' `pending`
field for that strategy holds the (negative) amount being deallocated, and
`spot.earn.deallocate_status`'s `pending` is `true`.

**Documented `Earnings`-class errors:**

- `EEarnings:Below min:(De)allocation operation amount less than minimum allowed`
- `EEarnings:Busy:Another (de)allocation for the same strategy is in progress`
- `EGeneral:Invalid arguments:Invalid strategy ID`

## Example response

```json
{ "error": [], "result": true }
```

`result` is `true` on acceptance, `null` when an error occurred (error detail in the
`error` array).

[Deallocate Earn Funds](https://docs.kraken.com/api-reference/earn/deallocate-earn-funds)
