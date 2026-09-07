# Cancel All

Cancels all open orders on the account, including untriggered orders and orders
resting in the book. The details of each individual cancelled order are also streamed
on the `executions` channel.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/cancel_all>

## Example

Request:

```json
{
  "method": "cancel_all",
  "params": {
    "token": "weeBxllys/7kHy/zHpkATSDIS42BvDgWS2b04ZSZHZ5"
  },
  "req_id": 1234567890
}
```

Response:

```json
{
  "method": "cancel_all",
  "req_id": 1234567890,
  "result": {
    "count": 1
  },
  "success": true,
  "time_in": "2023-09-26T13:09:48.463201Z",
  "time_out": "2023-09-26T13:09:48.471419Z"
}
```
