# Executions

<https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/executions>

The `executions` channel streams order status and execution events for this account, over
the private connection (`wss://ws-auth.kraken.com/v2`, token auth required). It replaces WS
v1's separate `openOrders` and `ownTrades` channels with one unified stream: every push
carries a list of execution reports, and each report's `exec_type` says what happened
(`pending_new`, `new`, `trade`, `filled`, `iceberg_refill`, `canceled`, `expired`, `amended`,
`restated`, `status`) and determines which of the ~35 possible fields are actually present.

By default the snapshot right after subscribing contains all open orders plus the latest 50
fills (`snap_orders`/`snap_trades`, both boolean); `order_status: false` narrows the update
stream to only open/close transitions (`new`, `filled`, `canceled`, `expired`) rather than
every intermediate status change. `users: "all"` is master-account-only, same as `balances`.

Two deprecated params still work: `snapshot` (superseded by `snap_orders`/`snap_trades`) and
`snapshot_trades` (superseded by `snap_trades`).

## Documented example

A new order going live, then receiving a fill (two separate `update` pushes for the same
`order_id`):

```json
{
  "channel": "executions",
  "type": "update",
  "data": [
    {
      "timestamp": "2023-09-22T10:33:05.709982Z",
      "order_status": "new",
      "exec_type": "new",
      "order_userref": 3,
      "order_id": "OK4GJX-KSTLS-7DZZO5"
    }
  ],
  "sequence": 9
}
```

```json
{
  "channel": "executions",
  "type": "update",
  "data": [
    {
      "order_id": "OK4GJX-KSTLS-7DZZO5",
      "order_userref": 3,
      "exec_id": "TGBB7L-HT5LX-J3BZ4A",
      "exec_type": "trade",
      "trade_id": 62887576,
      "symbol": "BTC/USD",
      "side": "sell",
      "last_qty": 0.005,
      "last_price": 26599.9,
      "liquidity_ind": "t",
      "cost": 132.9995,
      "order_type": "limit",
      "timestamp": "2023-09-22T10:33:05.709993Z",
      "order_status": "partially_filled",
      "cum_qty": 0.005,
      "cum_cost": 132.9995,
      "avg_price": 26599.9,
      "fee_usd_equiv": 0.3458,
      "fees": [{"asset": "USD", "qty": 0.3458}]
    }
  ],
  "sequence": 10
}
```

Note: the doc's third ("Pending") example JSON includes a top-level `limit_price_type` field
that has no corresponding entry in the field documentation (only `contingent.limit_price_type`
is documented) — likely a doc inconsistency; not reflected in the spec's schema. See
`endpoint.json`'s `notes` for the full list of judgment calls made while modeling this shape.
