# Get Deallocation Status

Returns the status of the most recently dispatched deallocation request for a
strategy. Requires either the `Earn Funds` or `Query Funds` API key permission. Only
one (de)allocation can be in flight per user/strategy pair, so this always resolves to
that one operation. `pending: true` means still in progress, `false` means completed;
if the underlying request failed, the error it failed with is returned here as if it
belonged to this request.

**Documented `Earnings`-class errors:**

- `EEarnings:Insufficient funds:Insufficient funds to complete the (de)allocation request`
- `EEarnings:Below min:(De)allocation operation amount less than minimum`

## Example response

```json
{ "error": [], "result": { "pending": false } }
```

[Get Deallocation Status](https://docs.kraken.com/api-reference/earn/get-deallocation-status)
