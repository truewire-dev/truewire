# Book (Level 2)

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book) --
checksum algorithm: [Book checksum (WebSocket v2)](https://docs.kraken.com/exchange/guides/websockets/book-checksum-v2)

Streams the L2 order book: aggregated bid/ask quantities at each price level, for one or
more subscribed symbols. The snapshot contains the requested `depth` levels per side; each
subsequent update carries only the changed levels (`qty: 0` means remove that level).
Multiple updates to the same price level can arrive in a single message and must be
processed in order.

Every snapshot/update also carries a CRC32 `checksum` of the top 10 bids (high to low) and
top 10 asks (low to high), always computed over the top 10 regardless of subscribed
`depth`, letting a client verify its locally-maintained book stays in sync with the
exchange. Verification is optional and can be done on every update or periodically.

## Example

Subscribe:

```json
{"method": "subscribe", "params": {"channel": "book", "symbol": ["ALGO/USD", "MATIC/USD"]}}
```

Snapshot push (truncated to 2 levels per side for brevity; a real snapshot has `depth` levels each):

```json
{
  "channel": "book",
  "type": "snapshot",
  "data": [
    {
      "symbol": "MATIC/USD",
      "bids": [
        {"price": 0.5666, "qty": 4831.75496356},
        {"price": 0.5665, "qty": 6658.22734739}
      ],
      "asks": [
        {"price": 0.5668, "qty": 4410.79769741},
        {"price": 0.5669, "qty": 4655.40412487}
      ],
      "checksum": 2439117997,
      "timestamp": "2023-10-06T17:35:55.440295Z"
    }
  ]
}
```

Update push:

```json
{
  "channel": "book",
  "type": "update",
  "data": [
    {
      "symbol": "MATIC/USD",
      "bids": [{"price": 0.5657, "qty": 1098.3947558}],
      "asks": [],
      "checksum": 2114181697,
      "timestamp": "2023-10-06T17:35:55.440295Z"
    }
  ]
}
```
