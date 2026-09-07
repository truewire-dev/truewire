# Amend Order

Modifies the parameters of a live order in-place, without the cancel-and-recreate cycle
`edit_order` uses:

* Order identifiers (Kraken's and/or the client's) stay the same.
* Queue priority in the order book is maintained where possible.
* If the amendment reduces the order quantity below the already-filled quantity, the
  remaining (unfilled) quantity is cancelled.

Identify the order via either `order_id` or `cl_ord_id`. See the
[amend transaction guide](https://docs.kraken.com/exchange/guides/general/amends) for
more detail.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/amend_order>

## Example

Request:

```json
{
  "method": "amend_order",
  "params": {
    "cl_ord_id": "2c6be801-1f53-4f79-a0bb-4ea1c95dfae9",
    "limit_price": 490795,
    "order_qty": 1.2,
    "token": "PM5Qm0MDrS54l657aQAtb7AhrwN30e2LBg1nUYOd6vU"
  }
}
```

Response:

```json
{
  "method": "amend_order",
  "result": {
    "amend_id": "TTW6PD-RC36L-ZZSWNU",
    "cl_ord_id": "2c6be801-1f53-4f79-a0bb-4ea1c95dfae9"
  },
  "success": true,
  "time_in": "2024-07-26T13:39:04.922699Z",
  "time_out": "2024-07-26T13:39:04.924912Z"
}
```
