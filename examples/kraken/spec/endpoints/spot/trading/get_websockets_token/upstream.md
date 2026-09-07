# Get WebSockets Token

[Upstream docs](https://docs.kraken.com/api-reference/trading/get-websockets-token)

Issues a short-lived token used to authenticate a connection to Kraken's private
WebSocket API, in place of per-message signing. Requires the `WebSocket interface - On`
API key permission.

The token should be used within 15 minutes of issuance to open the connection and make
the first private subscription; once that subscription is live, the token does not
expire for the life of the connection (no per-message re-signing needed afterward).

Example response:

```json
{"error": [], "result": {"token": "1Dwc4lzSwNWOAwkMdqhssNNFhs1ed606d1WcF3XfEMw", "expires": 900}}
```
