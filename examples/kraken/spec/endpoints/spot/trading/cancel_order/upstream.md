# Cancel Order

[Upstream docs](https://docs.kraken.com/api-reference/trading/cancel-order)

Cancels a particular open order, or a set of open orders sharing a `userref`, identified
by `txid`, `userref`, or `cl_ord_id`. Requires `Orders and trades - Create & modify
orders` or `Orders and trades - Cancel & close orders`.

`count` in the response is the number of orders cancelled (more than one when a shared
`userref` was targeted); `pending` is true if cancellation could not be confirmed
synchronously and is still in flight.

Example response:

```json
{"error": [], "result": {"count": 1}}
```
