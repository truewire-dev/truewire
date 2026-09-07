# Get System Status

[`GET /public/SystemStatus`](https://docs.kraken.com/api-reference/market-data/get-system-status)

Reports the exchange's current operational mode. Public, unauthenticated, no
parameters. The `status` values are exhaustively documented:

- `online` — normal operations, all order types accepted, trades occur
- `maintenance` — exchange offline, no new orders or cancellations
- `cancel_only` — resting orders can be cancelled, no new submissions, no trades
- `post_only` — only post-only limit orders accepted, no trades

Example response body (`result`, already unwrapped):

```json
{
  "status": "online",
  "timestamp": "2023-07-06T18:52:00Z"
}
```
