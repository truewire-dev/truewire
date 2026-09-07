# Cancel All Orders After X

[Upstream docs](https://docs.kraken.com/api-reference/trading/cancel-all-orders-after-x)

A "dead man's switch" protecting against network malfunction, extreme latency, or
unexpected matching-engine downtime. Starts (or resets) a countdown timer of `timeout`
seconds; when it expires, every one of the client's orders is cancelled and the timer
then stays disabled until a new non-zero `timeout` is sent. Sending `timeout: 0`
disables the timer without waiting for expiry. Requires `Orders and trades - Create &
modify orders` or `Orders and trades - Cancel & close orders`.

Recommended usage: call every 15-30 seconds with a 60 second timeout, so a brief
disconnection doesn't cancel resting orders but a real breakdown does. Also disable the
timer ahead of scheduled matching-engine maintenance -- a live timer cancels all orders
when the engine comes back from *any* downtime, planned or otherwise.

Example response:

```json
{"error": [], "result": {"currentTime": "2023-03-24T17:41:56Z", "triggerTime": "2023-03-24T17:42:56Z"}}
```
