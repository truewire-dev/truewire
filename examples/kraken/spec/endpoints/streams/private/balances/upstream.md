# Balances

<https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/balances>

The `balances` channel streams client asset balances and account ledger transactions, over
the private connection (`wss://ws-auth.kraken.com/v2`, token auth required). By default
(`snapshot: true`) the first push after subscribing is a full snapshot of every asset held in
the account, each with its total balance and a per-wallet (`spot`/`earn`) breakdown. After
that, one `update` push is sent per completed ledger transaction affecting the account — a
deposit, a trade, a staking event, etc. — each carrying the ledger entry (`ledger_id`,
`ref_id`, `type`, `category`, `amount`, `fee`, resulting `balance`) for a single asset. A
trade that moves two assets (e.g. buying BTC with USD) shows up as two separate `update`
messages sharing the same `ref_id`.

`rebased` and `users` are edge-case params: `rebased` only affects xstocks display (SPV
token vs. underlying equity terms), and `users: "all"` is master-account-only (streams
sub-account events too, but suppresses the snapshot).

## Documented example

Snapshot:

```json
{
  "channel": "balances",
  "data": [
    {"asset": "BTC", "asset_class": "currency", "balance": 1.2,
     "wallets": [{"type": "spot", "id": "main", "balance": 1.2}]},
    {"asset": "USD", "asset_class": "currency", "balance": 80595.4943,
     "wallets": [{"type": "spot", "id": "main", "balance": 80595.4943}]}
  ],
  "type": "snapshot",
  "sequence": 1
}
```

Trade update (one side of a two-message pair — the matching leg for the other asset arrives
as a separate message sharing `ref_id`):

```json
{
  "channel": "balances",
  "type": "update",
  "data": [
    {
      "ledger_id": "AAICKV-NMQSR-ZO5IJD",
      "ref_id": "AGBB7L-HT5LX-J3BB4A",
      "timestamp": "2023-09-22T10:33:05.710082Z",
      "type": "trade",
      "asset": "BTC",
      "asset_class": "currency",
      "category": "trade",
      "wallet_type": "spot",
      "wallet_id": "main",
      "amount": -0.005,
      "fee": 0.0,
      "balance": 0.005
    }
  ],
  "sequence": 9
}
```
