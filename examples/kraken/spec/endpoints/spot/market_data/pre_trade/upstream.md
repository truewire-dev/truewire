# Pre-Trade Data

[`GET /public/PreTrade`](https://docs.kraken.com/api-reference/transparency/pre-trade-data)

Part of Kraken's regulatory transparency surface (MiFID-style pre-trade transparency),
not the trading-UI order book -- distinct from `/public/Depth`/`/public/GroupedBook`.
Returns the top 10 aggregated price levels per side for a pair, plus reference data
(base/quote asset, DTI codes, venue MIC, order-book system). `symbol` required.
Public, unauthenticated. No worked example response is published on the doc page; the
shape below is reconstructed from the OpenAPI schema.

Fixed/near-constant fields observed in the schema: `venue` is always `PGSL` (Kraken's
MIC), `system` is always `CLOB`, `quote_notation` is always `MONE`, and each
bid/ask entry's `side` is fixed to `BUY`/`SELL` respectively.
