# Cancel Order Batch

[Upstream docs](https://docs.kraken.com/api-reference/trading/cancel-order-batch)

Cancels multiple open orders in one call, identified by `txid`, `userref`, or
`cl_ord_id` -- up to 50 unique identifiers/references total, split across the `orders`
and `cl_ord_ids` arrays. Requires `Orders and trades - Create & modify orders` or
`Orders and trades - Cancel & close orders`.

Upstream's formally declared schema wraps each identifier in an object (`orders: [{txid:
...}]`, `cl_ord_ids: [{cl_ord_id: ...}]`), but both its own inline `example` and the live
doc page's worked example instead show a flat array of bare strings:

```json
{"nonce": 1695828490, "orders": ["OP5V2Y-RYKVL-ET3V3B", "OP5V2Y-7YKVL-ET3V3B"]}
```

This is a real discrepancy in upstream's own documentation -- worth confirming
empirically which shape the endpoint actually accepts before this endpoint's first
example capture.

Example response:

```json
{"error": [], "result": {"count": 2}}
```
