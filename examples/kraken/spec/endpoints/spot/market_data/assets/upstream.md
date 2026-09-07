# Get Asset Info

[`GET /public/Assets`](https://docs.kraken.com/api-reference/market-data/get-asset-info)

Returns per-asset metadata for everything deposit/withdrawal/trading/earn-eligible on
Kraken: decimal precision (both storage and display), margin-collateral valuation, and
lifecycle `status`. Public, unauthenticated. `asset` filters to a comma-delimited list;
omitted, every asset is returned.

`status` is documented as exactly four values: `enabled`, `deposit_only`,
`withdrawal_only`, `funding_temporarily_disabled`.

`assetVersion=1` switches asset keys and the identifier fields from Kraken's legacy
`X`/`Z`-prefixed internal names (`XXBT`, `ZUSD`) to canonical display names (`BTC`,
`USD`); omitted, internal names are used. Only `assetVersion=1` currently exists.

Example response entry (`result`, already unwrapped):

```json
{
  "XXBT": {
    "aclass": "currency",
    "altname": "XBT",
    "decimals": 10,
    "display_decimals": 5,
    "collateral_value": 1,
    "status": "enabled"
  }
}
```
