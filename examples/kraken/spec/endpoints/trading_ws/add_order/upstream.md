# Add Order

Sends a single, new order into the exchange over the private WebSocket connection
(`wss://ws-auth.kraken.com/v2`). Supports the full range of order types, Time-In-Force
settings, and order flags: triggered order types (`stop-loss`, `take-profit`,
`trailing-stop`, ...) configure their trigger via the `triggers` object; One-Triggers-
Other (OTO) orders attach a secondary close order via `conditional`, generated on each
fill of the primary order at the same executed size and the opposite side.

`validate: true` validates the order without ever reaching the matching engine -- no
`order_id` is returned in that case, only (optionally) `warnings`.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/add_order>

## Example (Limit order)

Request:

```json
{
  "method": "add_order",
  "params": {
    "order_type": "limit",
    "side": "buy",
    "limit_price": 26500.4,
    "order_userref": 100054,
    "order_qty": 1.2,
    "symbol": "BTC/USD",
    "token": "G38a1tGFzqGiUCmnegBcm8d4nfP3tytiNQz6tkCBYXY"
  },
  "req_id": 123456789
}
```

Response:

```json
{
  "method": "add_order",
  "req_id": 123456789,
  "result": {
    "order_id": "AA5JGQ-SBMRC-SCJ7J7",
    "order_userref": 100054
  },
  "success": true,
  "time_in": "2023-09-21T14:15:07.197274Z",
  "time_out": "2023-09-21T14:15:07.205301Z"
}
```

A failed request carries `"success": false` and an `error` string
(`"EOrder:Insufficient funds"`, etc.) instead of `result`.
