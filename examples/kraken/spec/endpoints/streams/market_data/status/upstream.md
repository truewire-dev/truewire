# Status

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/status) --
subscribe example only documented on
[Reconnection and resilience](https://docs.kraken.com/exchange/guides/websockets/reconnection)

The `status` channel verifies exchange status and successful initial connection. There is
no way to directly request a fresh update -- one is automatically generated on successful
WebSocket connection and whenever the trading engine status changes. `system` reflects the
trading engine's mode; when `maintenance` is received, stop sending orders and prepare to
reconnect.

The channel's own reference page (linked above) omits a `Subscribe` request/response
example -- the subscribe shape below comes from the reconnection guide instead, which
documents subscribing explicitly:

```json
{"method": "subscribe", "params": {"channel": "status"}}
```

## Example

Status push:

```json
{
  "channel": "status",
  "data": [
    {
      "api_version": "v2",
      "connection_id": 13834774380200032777,
      "system": "online",
      "version": "2.0.0"
    }
  ],
  "type": "update"
}
```
