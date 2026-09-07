# Delete Export Report

`POST /private/RemoveExport` -- deletes or cancels an exported trades/ledgers report, by
report `id`. Requires `Data - Export data`.

`type: "delete"` only works on already-processed reports; `type: "cancel"` is for
reports still queued or processing. The response carries `delete` or `cancel`
(booleans) matching whichever `type` was requested -- which key is present depends on
the request, not on a discriminator in the response itself.

## Example response

```json
{"error": [], "result": {"delete": true}}
```

Reference: [Delete Export Report](https://docs.kraken.com/api-reference/account-data/delete-export-report)
