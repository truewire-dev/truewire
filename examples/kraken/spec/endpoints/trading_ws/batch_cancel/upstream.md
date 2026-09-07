# Batch Cancel

Cancels multiple orders (minimum 2, maximum 50 total unique identifiers) in a single
request, identified by a mix of client `order_userref`/`cl_ord_id` or Kraken `order_id`
identifiers.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/batch_cancel>

## Example

Request:

```json
{
  "method": "batch_cancel",
  "params": {
    "orders": [
      "1",
      "2",
      "ORDERX-IDXXX-XXXXX3"
    ],
    "token": "TxxxxxxxxxOxxxxxxxxxxKxxxxxxxExxxxxxxxN"
  },
  "req_id": 1234567890
}
```

Response:

```json
{
  "method": "batch_cancel",
  "req_id": 1234567890,
  "result": {
    "count": 3
  },
  "success": true,
  "time_in": "2022-06-13T08:09:10.123456Z",
  "time_out": "2022-06-13T08:09:10.7890123"
}
```
