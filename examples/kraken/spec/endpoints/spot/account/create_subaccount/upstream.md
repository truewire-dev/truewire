# Create Subaccount

`POST /private/CreateSubaccount` -- creates a trading subaccount. Must be called with
an API key from the master account. Requires `Funds permissions - Withdraw`.

**Institutional only:** "Subaccounts are currently only available to institutional
clients. Please contact your Account Manager for more details." A personal, self-serve
API key cannot use this endpoint.

## Example response

```json
{"error": [], "result": true}
```

Reference: [Create Subaccount](https://docs.kraken.com/api-reference/subaccounts/create-subaccount)
