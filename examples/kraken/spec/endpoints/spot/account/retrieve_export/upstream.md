# Retrieve Data Export

`POST /private/RetrieveExport` -- retrieves a processed data export by report `id`
(from `AddExport`, once `ExportStatus` shows `"Processed"`). Requires
`Data - Export data`.

**Not JSON.** The response is `application/octet-stream`: a binary zip archive
containing the exported CSV/TSV file, not the `{error, result}` envelope every other
Account Data endpoint uses. There is no documented example JSON body -- the request
still needs signing (`API-Key`/`API-Sign` headers, `nonce` in the body) exactly like any
other private call, only the response shape differs.

Reference: [Retrieve Data Export](https://docs.kraken.com/api-reference/account-data/retrieve-data-export)
