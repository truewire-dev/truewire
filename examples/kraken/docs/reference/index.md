# Reference

75 endpoints in eight routers. Each signature is generated from the endpoint's schema, so
the types in your editor are the reference: every field carries its description.

| Router | Endpoints | Transport | Key |
| --- | --- | --- | --- |
| `spot.market_data` | 12: ticker, depth, OHLC, trades, spread, asset pairs, assets, system status, time, … | REST | none |
| `spot.account` | 21: balances, orders, trades, ledgers, positions, exports, subaccounts | REST | yes |
| `spot.trading` | 9: add, amend, edit and cancel orders, singly and in batches; the socket token | REST | yes |
| `spot.funding` | 10: deposit and withdrawal methods, addresses and status; withdraw; wallet transfer | REST | yes |
| `spot.earn` | 6: strategies, allocations, allocate and deallocate | REST | yes |
| `streams.market_data` | 7: book, instrument, OHLC, ticker, trade, status, ping | WebSocket | none |
| `streams.private` | 2: balances, executions | WebSocket | yes |
| `trading_ws` | 8: add, amend, edit and cancel orders, singly and in batches | WebSocket | yes |

Every call takes `validate` (default `True`): `False` returns the body as Kraken sent it,
typed `Any`. In TypeScript, Rust and Go the names follow each language's convention:
`spot.marketData.ticker`, `spot.market_data.ticker` and `Spot.MarketData.Ticker`.
