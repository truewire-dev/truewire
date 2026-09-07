# Add Order Batch

[Upstream docs](https://docs.kraken.com/api-reference/trading/add-order-batch)

Sends a batch of 2 to 15 orders, all sharing a single trading pair (`pair` is
batch-level, not per-order). Requires both `Orders and trades - Create & modify orders`
and `Orders and trades - Cancel & close orders` permissions.

Two validation phases: the whole batch is validated before submission -- one invalid
order rejects the entire batch -- but once submitted to the matching engine, an
individual order failing a pre-match check (e.g. insufficient funds) is rejected on its
own and the rest of the batch is still processed. The response `orders` array preserves
request order, and each entry is either a success (`descr`/`txid`) or a failure
(`error`), reported per order.

Each order in `orders` accepts the same fields as `AddOrder` (including a nested `close`
conditional-order object), minus `pair`/`asset_class` (batch-level) and with a narrower
`timeinforce` enum (`GTC`/`IOC`/`GTD`, no `FOK`) than the single-order endpoint documents.

Example response:

```json
{
  "error": [],
  "result": {
    "orders": [
      {"txid": "O5OR23-ADFAD-Y2G61C",
       "descr": {"order": "buy 0.80300000 XBTUSD @ limit 28300.0",
                 "close": "close position @ stop loss 27000.0 -> limit 26000.0"}},
      {"txid": "9K6KFS-5H3PL-XBRC7A",
       "descr": {"order": "sell 0.10500000 XBTUSD @ limit 36000.0"}}
    ]
  }
}
```
