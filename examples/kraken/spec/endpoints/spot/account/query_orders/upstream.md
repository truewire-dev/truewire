# Query Orders Info

`POST /private/QueryOrders` -- retrieves information about specific orders, by
transaction ID (comma-delimited, up to 50). Requires
`Orders and trades - Query open orders & trades` or
`Orders and trades - Query closed orders & trades`, depending on the order's status.

Response shape is the same open/closed order union `OpenOrders`/`ClosedOrders` use,
keyed by the requested txids. `trades: true` includes each order's related trade IDs.

## Example response

```json
{
  "error": [],
  "result": {
    "OBCMZD-JIEE7-77TH3F": {
      "refid": null, "userref": 0, "status": "closed", "reason": null,
      "opentm": 1688665496.7808, "closetm": 1688665499.1922, "starttm": 0, "expiretm": 0,
      "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "stop-loss-limit", "price": "27500.0", "price2": "0", "leverage": "none", "order": "buy 1.25000000 XBTUSD @ limit 27500.0", "close": ""},
      "vol": "1.25000000", "vol_exec": "1.25000000", "cost": "27526.2", "fee": "26.2",
      "price": "27500.0", "stopprice": "0.00000", "limitprice": "0.00000", "misc": "", "oflags": "fciq",
      "trades": ["TZX2WP-XSEOP-FP7WYR"]
    }
  }
}
```

Reference: [Query Orders Info](https://docs.kraken.com/api-reference/account-data/query-orders-info)
