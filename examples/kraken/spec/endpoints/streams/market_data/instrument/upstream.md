# Instruments

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/instrument)

Streams reference data -- symbol identifiers, precisions, trading parameters and rules --
for all active assets and tradeable pairs. Unlike the other market-data channels this one
takes no `symbol` filter; it always covers the whole active universe (optionally including
xStocks via `include_tokenized_assets`). Also referenced by the general WS guide as the
recommended way to discover which pairs are subscribable via WebSockets v2 (the `pairs[].symbol`
field), since v2 renamed several assets (e.g. `XBT` -> `BTC`).

## Example

Subscribe:

```json
{"method": "subscribe", "params": {"channel": "instrument"}, "req_id": 79}
```

Snapshot push (truncated):

```json
{
  "channel": "instrument",
  "type": "snapshot",
  "data": {
    "assets": [
      {
        "id": "USD", "status": "enabled", "precision": 4, "precision_display": 2,
        "borrowable": true, "collateral_value": 1.0, "margin_rate": 0.015
      }
    ],
    "pairs": [
      {
        "symbol": "BTC/USD", "base": "BTC", "quote": "USD", "status": "online",
        "qty_precision": 8, "qty_increment": 1e-08, "price_precision": 1,
        "cost_precision": 5, "marginable": true, "has_index": true, "cost_min": 0.5,
        "margin_initial": 0.2, "position_limit_long": 250, "position_limit_short": 200,
        "tick_size": 0.1, "price_increment": 0.1, "qty_min": 0.0001
      }
    ]
  }
}
```
