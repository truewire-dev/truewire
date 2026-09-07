# Ping

[Upstream docs](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ping)

An application-level ping/pong, distinct from the protocol-level WebSocket ping. Clients
can send it to verify the connection is alive; the server answers with `method: "pong"`.
The general WebSocket introduction guide also recommends it (or any other request) as a
way to keep an otherwise-idle connection open, since Kraken closes connections after
roughly a minute without inbound traffic.

## Example

Request:

```json
{"method": "ping", "req_id": 101}
```

Response:

```json
{
  "method": "pong",
  "req_id": 101,
  "time_in": "2023-09-24T14:10:23.799685Z",
  "time_out": "2023-09-24T14:10:23.799703Z"
}
```
