# Cancel Order

Cancels one or more open orders in a single request. Orders can be identified by a
range of client or Kraken identifiers (`order_id`, `cl_ord_id`, `order_userref`) -- but
these cannot be combined within one request. The details of each individual cancelled
order are also streamed on the `executions` channel.

**Note:** the doc page states that "When cancelling multiple orders, there will be a
stream of individual order responses" -- worth verifying live whether that means
multiple `req_id`-correlated reply frames for one request.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/cancel_order>

## Example

Request:

```json
{
  "method": "cancel_order",
  "params": {
    "order_id": [
      "OM5CRX-N2HAL-GFGWE9",
      "OLUMT4-UTEGU-ZYM7E9"
    ],
    "token": "zGXT1dUQQjJjy5VmGXMegdDQngXXehNo5qbMBVolwEQ"
  },
  "req_id": 123456789
}
```

Response:

```json
{
  "method": "cancel_order",
  "req_id": 123456789,
  "result": {
    "order_id": "OLUMT4-UTEGU-ZYM7E9"
  },
  "success": true,
  "time_in": "2023-09-21T14:36:57.428972Z",
  "time_out": "2023-09-21T14:36:57.437952Z"
}
```
