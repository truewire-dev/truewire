# Post-Trade Data

[`GET /public/PostTrade`](https://docs.kraken.com/api-reference/transparency/post-trade-data)

The regulatory transparency counterpart to `/public/PreTrade`: a flat list of executed
trades across the whole exchange (or one pair via `symbol`), not a per-pair order book.
With no filters, the last 1000 trades across all pairs are returned. `from_ts` is
exclusive ("after"), `to_ts` is inclusive ("before or at"); `count` caps the batch
(1-1000, default 1000). `last_ts` in the response is documented as reusable directly as
the next call's `from_ts` for incremental walking, though the bounds are ISO-8601
strings rather than an epoch integer. Public, unauthenticated. No worked example
response is published on the doc page; the shape below is reconstructed from the
OpenAPI schema.

`base_notation` is always `UNIT`, `quote_notation` is always `MONE`.
