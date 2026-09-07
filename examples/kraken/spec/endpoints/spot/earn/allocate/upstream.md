# Allocate Earn Funds

Allocates funds to a strategy. Requires the `Earn Funds` API key permission; `amount`
must always be given.

**Asynchronous.** A couple of preflight checks run synchronously, but the
(de)allocation itself is dispatched further and the client must poll
`spot.earn.allocate_status` for the actual outcome. Only one (de)allocation can be in
flight per user/strategy pair at a time; while it's in flight, `spot.earn.allocations`'
`pending` field for that strategy is set, and `spot.earn.allocate_status`'s `pending`
is `true`.

**Documented `Earnings`-class errors:**

- `EEarnings:Below min:(De)allocation operation amount less than minimum`
- `EEarnings:Busy:Another (de)allocation for the same strategy is in progress`
- `EEarnings:Busy` — service temporarily unavailable, retry in a few minutes
- `EEarnings:Permission denied:The user's tier is not high enough`
- `EGeneral:Invalid arguments:Invalid strategy ID`

## Example response

```json
{ "error": [], "result": true }
```

`result` is `true` on acceptance, `null` when an error occurred (error detail in the
`error` array).

[Allocate Earn Funds](https://docs.kraken.com/api-reference/earn/allocate-earn-funds)
