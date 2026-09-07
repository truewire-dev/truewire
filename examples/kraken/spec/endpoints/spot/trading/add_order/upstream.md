# Add Order

[Upstream docs](https://docs.kraken.com/api-reference/trading/add-order)

Places a new Spot order. Requires the `Orders and trades - Create & modify orders` API
key permission. See `AssetPairs` for tradable pairs and their price/quantity precisions,
order minimums, and available leverage.

`ordertype` chooses the execution model; `price`/`price2` mean different things depending
on it (limit price vs. trigger price vs. secondary limit price for stop-limit/take-profit-limit
variants). Both accept relative pricing (`+`/`-`/`#` prefix, optional `%` suffix) relative to
the last traded price; trailing-stop types must use the `+` prefix, with direction inferred
from the order's side.

A conditional close order can be attached (`close[ordertype]`/`close[price]`/`close[price2]`
on the wire) -- triggered by execution of the primary order in the same quantity and opposite
direction, but once triggered it becomes an **independent** order that may reduce or increase
net position, not strictly close it.

`validate: true` validates the order without ever reaching the matching engine -- the response
omits `txid` in that case. This is the only Spot REST "paper trading" mechanism; there is no
dedicated sandbox environment for self-serve accounts.

Example response:

```json
{
  "error": [],
  "result": {
    "descr": {"order": "buy 2.12340000 XBTUSD @ limit 25000.1 with 2:1 leverage",
              "close": "close position @ stop loss 22000.0 -> limit 21000.0"},
    "txid": ["OUF4EM-FRGI2-MQMWZD"]
  }
}
```
