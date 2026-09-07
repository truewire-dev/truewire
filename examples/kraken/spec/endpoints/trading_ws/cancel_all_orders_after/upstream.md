# Cancel All Orders After (Dead Man's Switch)

`cancel_all_orders_after` is a "Dead Man's Switch": protection against network
malfunction, extreme latency, or unexpected matching-engine downtime.

* The client sends a request with a `timeout` (seconds), starting a countdown timer in
  the trading engine that cancels all account orders when it expires.
* The client must keep sending new requests to push back the trigger time, or send
  `timeout: 0` to disable the mechanism.
* Once the timer expires, all orders in the account are cancelled and the feature is
  disabled until the next `cancel_all_orders_after` request.
* Recommended use: a call every 15-30 seconds, with `timeout: 60`. This keeps orders in
  place across a brief disconnection or transient delay, while still protecting them
  against a genuine network breakdown.
* Recommended to disable the timer ahead of scheduled trading-engine maintenance --
  a still-armed timer cancels all orders when the engine comes back from downtime.

**Note on naming:** the doc page's URL slug is `cancel_after` and its page title is
"Cancel on Disconnect", but the wire `method` (and the value in every JSON example on
the page) is `cancel_all_orders_after` -- that is what `spec.channel` uses here.

Doc: <https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/cancel_after>

## Example

Request:

```json
{
  "method": "cancel_all_orders_after",
  "params": {
    "timeout": 100,
    "token": "zwpdzWUe18Bn6h4TAMorh26+QbcMeST2B5tamfe+pgQ"
  },
  "req_id": 1234567890
}
```

Response:

```json
{
  "method": "cancel_all_orders_after",
  "req_id": 1234567890,
  "result": {
    "currentTime": "2023-09-21T15:49:29Z",
    "triggerTime": "2023-09-21T15:51:09Z"
  },
  "success": true,
  "time_in": "2023-09-21T15:49:28.627900Z",
  "time_out": "2023-09-21T15:49:28.649057Z"
}
```
