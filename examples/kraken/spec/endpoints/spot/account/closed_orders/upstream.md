# Get Closed Orders

`POST /private/ClosedOrders` -- retrieves orders that have been closed (filled or
cancelled). 50 results are returned at a time, the most recent first by default. Requires
the `Orders and trades - Query closed orders & trades` API key permission.

## Pagination

`ofs` offsets into the result set (no page-size parameter -- pages are a fixed 50 rows).
`count` in the response is the total number of orders matching the filters, and doubles
as the pagination terminator; passing `without_count: true` skips computing it ("much
faster for users with many closed orders") at the cost of losing that signal.

If an order's tx ID is given for `start`/`end`, the order's opening time (`opentm`) is
used for the comparison. `closetime` (`open`/`close`/`both`, default `both`) selects
which timestamp `start`/`end` filter against.

## Example response

```json
{
  "error": [],
  "result": {
    "closed": {
      "O37652-RJWRT-IMO74O": {
        "refid": null, "userref": 1, "status": "canceled", "reason": "User requested",
        "opentm": 1688148493.7708, "closetm": 1688148610.0482, "starttm": 0, "expiretm": 0,
        "descr": {"pair": "XBTGBP", "type": "buy", "ordertype": "stop-loss-limit", "price": "23667.0", "price2": "0", "leverage": "none", "order": "buy 0.00100000 XBTGBP @ limit 23667.0", "close": ""},
        "vol": "0.00100000", "vol_exec": "0.00000000", "cost": "0.00000", "fee": "0.00000",
        "price": "0.00000", "stopprice": "0.00000", "limitprice": "0.00000", "misc": "", "oflags": "fciq"
      }
    },
    "count": 2
  }
}
```

Reference: [Get Closed Orders](https://docs.kraken.com/api-reference/account-data/get-closed-orders)
