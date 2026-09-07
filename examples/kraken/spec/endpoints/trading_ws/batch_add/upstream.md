# Batch Add

Sends a collection of orders (minimum 2, maximum 15) in a single request:

* Validation runs against the whole batch before submission to the engine. If any
  order fails validation, the whole batch is rejected.
* On submission to the engine, an order that fails a pre-match check (e.g.
  insufficient funding) is rejected individually and the rest of the batch proceeds.
* All orders in a batch are limited to a single pair.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/batch_add>

## Example

Request:

```json
{
  "method": "batch_add",
  "params": {
    "deadline": "2022-06-13T08:09:10.123456Z",
    "orders": [
      {
        "limit_price": 1010.10,
        "order_qty": 0.123456789,
        "order_type": "limit",
        "order_userref": 1,
        "side": "buy"
      },
      {
        "limit_price": 2020.20,
        "order_qty": 0.987654321,
        "order_type": "limit",
        "order_userref": 2,
        "side": "sell",
        "stp_type": "cancel_both"
      }
    ],
    "symbol": "BTC/USD",
    "token": "TxxxxxxxxxOxxxxxxxxxxKxxxxxxxExxxxxxxxN",
    "validate": false
  },
  "req_id": 1234567890
}
```

Response (`result` is an array, ordered the same as the request's `orders`):

```json
{
  "method": "batch_add",
  "req_id": 1234567890,
  "result": [
    { "order_id": "ORDERX-IDXXX-XXXXX1", "order_userref": 1 },
    { "order_id": "ORDERX-IDXXX-XXXXX2", "order_userref": 2 }
  ],
  "success": true,
  "time_in": "2022-06-13T08:09:10.123456Z",
  "time_out": "2022-06-13T08:09:10.7890123"
}
```
