# Get Export Report Status

`POST /private/ExportStatus` -- retrieves the status of previously requested data
exports (from `AddExport`) of the given `report` type. Requires `Data - Export data`.

Reports progress `Queued` -> `Processing` -> `Processed`. `error` carries an
`EExport:*` code when a report failed (`Internal error`, `Unexpected error`, `Canceled`,
`Deleted`, `Exported size too big`). Multiple reports of the same type can be returned.
`flags`/`expiretm`/`aclass`/`endtm` are deprecated (superseded by `fields`/`dataendtm`
etc where applicable) but still appear on live reports.

## Example response

```json
{
  "error": [],
  "result": [
    {
      "id": "VSKC", "descr": "my_trades_1", "format": "CSV", "report": "trades", "subtype": "all",
      "status": "Processed", "fields": "all", "createdtm": "1688669085", "starttm": "1688669093",
      "completedtm": "1688669093", "datastarttm": "1683556800", "dataendtm": "1688669085", "asset": "all"
    }
  ]
}
```

Reference: [Get Export Report Status](https://docs.kraken.com/api-reference/account-data/get-export-report-status)
