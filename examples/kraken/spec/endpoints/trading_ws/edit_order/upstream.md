# Edit Order (legacy cancel-replace)

Edits the parameters of a live order by cancelling it and creating a new order with the
adjusted parameters; a new `order_id` is returned in the response.

The newer [`amend_order`](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/amend_order)
resolves this endpoint's caveats and has additional performance gains. Caveats for
`edit_order`:

* Triggered stop-loss or take-profit orders are not supported.
* Orders with conditional close terms attached are not supported.
* Orders where the executed volume is greater than the newly supplied volume are rejected.
* `cl_ord_id` is not supported.
* Existing executions stay associated with the original order, not copied to the edited one.
* Queue position is not maintained.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/edit_order>

## Example

Request:

```json
{
  "method": "edit_order",
  "params": {
    "order_id": "ORDERX-IDXXX-XXXXX1",
    "order_qty": 0.2123456789,
    "symbol": "BTC/USD",
    "token": "TxxxxxxxxxOxxxxxxxxxxKxxxxxxxExxxxxxxxN"
  },
  "req_id": 1234567890
}
```

Response:

```json
{
  "method": "edit_order",
  "req_id": 1234567890,
  "result": {
    "order_id": "ORDERX-IDXXX-XXXXX2",
    "original_order_id": "ORDERX-IDXXX-XXXXX1"
  },
  "success": true,
  "time_in": "2022-07-15T12:56:09.876488Z",
  "time_out": "2022-07-15T12:56:09.923422Z"
}
```
