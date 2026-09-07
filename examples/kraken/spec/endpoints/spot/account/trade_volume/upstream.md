# Get Trade Volume

`POST /private/TradeVolume` -- returns 30-day USD trading volume and the resulting fee
schedule for the given asset pair(s). Requires `Funds permissions - Query`.

`pair` accepts either a comma-delimited list of pair names (forex pairs) or a list of
`{asset, aclass}` objects -- the latter is required to request fees for non-forex
classes (`equity_pair`, `derivatives`, `futures_contract`, ...). Fees are omitted
entirely if `pair` isn't given. Maker/taker pairs split into `fees` (taker) and
`fees_maker` (maker); pairs without a maker/taker split appear only in `fees`.
`fee_schedule: true` additionally returns the full tiered fee schedule per pair in
`schedules`. `volume_subaccounts` only appears for master accounts.

## Example response

```json
{
  "error": [],
  "result": {
    "currency": "ZUSD", "asset_class": "currency", "volume": "200709587.4223",
    "inputs": {"domain_spot_volume_30d": "200709587.4223", "domain_futures_volume_30d": "0.0000", "domain_assets_on_platform": "0.0000"},
    "fees": {"XXBTZUSD": {"fee": "0.1000", "minfee": "0.1000", "maxfee": "0.2600", "nextfee": null, "tiervolume": "10000000.0000", "nextvolume": null}},
    "fees_maker": {"XXBTZUSD": {"fee": "0.0000", "minfee": "0.0000", "maxfee": "0.1600", "nextfee": null, "tiervolume": "10000000.0000", "nextvolume": null}}
  }
}
```

Reference: [Get Trade Volume](https://docs.kraken.com/api-reference/account-data/get-trade-volume)
