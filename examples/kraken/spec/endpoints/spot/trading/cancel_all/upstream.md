# Cancel All Orders

[Upstream docs](https://docs.kraken.com/api-reference/trading/cancel-all-orders)

Cancels every open order for the account -- no parameters beyond the standard signed
POST envelope. Requires `Orders and trades - Create & modify orders` or `Orders and
trades - Cancel & close orders`.

Example response:

```json
{"error": [], "result": {"count": 1}}
```
