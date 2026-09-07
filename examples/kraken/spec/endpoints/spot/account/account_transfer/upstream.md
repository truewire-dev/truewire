# Account Transfer

`POST /private/AccountTransfer` -- transfers funds to and from the master account and
its subaccounts. Must be called with an API key from the master account. Requires
`Funds permissions - Withdraw`.

**Institutional only**, same gating as `CreateSubaccount`. `from`/`to` are public
account IDs in the `ABCD 1234 EFGH 5678` format. `status` is `pending` or `complete` --
transfers may settle immediately or asynchronously.

## Example response

```json
{"error": [], "result": {"transfer_id": "TOH3AS2-LPCWR8-JDQGEU", "status": "complete"}}
```

Reference: [Account Transfer](https://docs.kraken.com/api-reference/subaccounts/account-transfer)
