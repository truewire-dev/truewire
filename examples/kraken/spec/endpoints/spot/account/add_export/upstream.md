# Request Export Report

`POST /private/AddExport` -- requests an export of trades or ledgers. Requires
`Data - Export data`.

This starts an asynchronous job: the response is just a report `id`, not the data. Poll
`ExportStatus` with that id/report type until `status: "Processed"`, then fetch the
binary ZIP with `RetrieveExport`.

`starttm`/`endtm` default to the 1st of the current month and now, respectively.
`fields` (comma-delimited, default `all`) restricts the exported columns -- valid names
differ between `trades` and `ledgers` reports.

## Example response

```json
{"error": [], "result": {"id": "TCJA"}}
```

Reference: [Request Export Report](https://docs.kraken.com/api-reference/account-data/request-export-report)
