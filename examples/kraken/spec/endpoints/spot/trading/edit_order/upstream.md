# Edit Order

[Upstream docs](https://docs.kraken.com/api-reference/trading/edit-order)

Legacy cancel-replace: edits a live order's parameters by cancelling the original and
creating a new order with the adjusted parameters, returning a new `txid`. Superseded by
`AmendOrder`, which edits in place, preserves queue priority, and has fewer caveats.
Requires both `Orders and trades - Create & modify orders` and `Orders and trades -
Cancel & close orders`.

Caveats (per upstream, all resolved by `AmendOrder` instead):

- Triggered stop-loss/take-profit orders are not supported.
- Orders with a conditional close attached are not supported.
- An edit that would reduce volume below the already-executed volume is rejected.
- `cl_ord_id` is not supported.
- Existing executions stay associated with the original order, not the replacement.
- Queue position is not maintained (the replacement order goes to the back of the book).

Example response:

```json
{
  "error": [],
  "result": {
    "status": "ok",
    "txid": "OFVXHJ-KPQ3B-VS7ELA",
    "originaltxid": "OHYO67-6LP66-HMQ437",
    "volume": "0.00030000",
    "price": "19500.0",
    "price2": "32500.0",
    "orders_cancelled": 1,
    "descr": {"order": "buy 0.00030000 XXBTZGBP @ limit 19500.0"}
  }
}
```
