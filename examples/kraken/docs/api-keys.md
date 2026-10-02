# API keys

Market data, public streams and the server time need no key. Everything that touches an
account does: balances, orders, trades, ledgers, funding, Earn, and the private socket.

## Getting a key

In Kraken Pro, open Settings → API → Create API key. Kraken shows the private key once, so
copy both halves before closing the page. The client reads them from the environment:

```
KRAKEN_API_KEY=...
KRAKEN_PRIVATE_KEY=...
```

Keep them in `.env` (git-ignored) or your shell's environment. `Kraken.new()` reads both.
You can also pass `api_key=` and `private_key=` yourself:

```python
from kraken import Kraken


async def balances() -> None:
  async with Kraken.new() as client:
    for asset, amount in (await client.spot.account.balance()).items():
      print(asset, amount)
```

Kraken rejects a request whose nonce is not higher than the last one it saw for the key.
Give each running process its own key.

## Which permissions to grant

Grant only what your calls use. Each row is Kraken's own requirement for the calls named:

| Permission | Needed by |
| --- | --- |
| Funds — Query | `balance`, `balance_ex`, `trade_volume`, `credit_lines`, `deposit_addresses`, `wallet_transfer` |
| Funds — Deposit | `deposit_methods` (with Funds — Query) |
| Funds — Earn | `spot.earn` |
| Orders and trades — Query open orders & trades | `open_orders`, `open_positions`, `trade_balance`; `query_orders` and `order_amends` for open orders |
| Orders and trades — Query closed orders & trades | `closed_orders`, `trades_history`, `query_trades`; `query_orders` and `order_amends` for closed ones |
| Orders and trades — Create & modify orders | `add_order`, `amend_order`; with Cancel & close, `add_order_batch` and `edit_order` |
| Orders and trades — Cancel & close orders | `cancel_order`, `cancel_order_batch`, `cancel_all`, `cancel_all_orders_after` |
| Data — Query ledger entries | `ledgers`, `query_ledgers` |
| Data — Export data | `add_export`, `export_status`, `retrieve_export`, `remove_export` |
| WebSocket interface — On | `get_websockets_token`, so `streams.private` and `trading_ws` |

## What to refuse

- **Funds — Withdraw**, **Add withdrawal addresses** and **Update withdrawal addresses**.
  With Withdraw, a key can call `withdraw`, `account_transfer` and `create_subaccount`.
  Without it, Kraken refuses them whatever the code does.
- Every permission in the table that your calls do not use.

Two cautions. `wallet_transfer` needs only Funds — Query, and it moves funds between your
own Kraken wallets. And set an IP allowlist on the key to the addresses your code runs
from.
