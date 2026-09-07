# Amend Order

[Upstream docs](https://docs.kraken.com/api-reference/trading/amend-order)

Modifies a live order's parameters in place, without cancelling and replacing it, unlike
the legacy `EditOrder`. Requires `Orders and trades - Create & modify orders` or
`Orders and trades - Cancel & close orders`.

- Both the Kraken-assigned and client-assigned order identifiers stay the same after the
  amend.
- Queue priority in the order book is preserved where possible.
- If the amended quantity drops below the already-filled quantity, the remaining
  (unfilled) quantity is cancelled.

Identify the order with either `txid` or `cl_ord_id`. `limit_price`/`trigger_price`
support relative pricing (`+`/`-` prefix, optional `%` suffix) against a reference price.
`post_only` (for `limit_price` amends) rejects the amend if it can't be posted passively.

See the [amend transaction guide](https://docs.kraken.com/docs/guides/spot-amends) for
further detail on the semantics -- not independently verified in this pass.

Example response:

```json
{"error": [], "result": {"amend_id": "TEZA4R-DSDGT-IJBOJK"}}
```
