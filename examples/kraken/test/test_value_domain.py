"""Handwritten value-domain and pagination-mechanics coverage -- the checks
`docs/testing.md` says a generic replay can't produce.
"""

import pytest


@pytest.mark.asyncio
async def test_deposit_methods_union_branch(client):
  """`DepositMethod.limit` is `str | Literal[False]` -- Kraken sends the literal `false`
  for a method with no deposit cap, not a string. Also exercises the hyphenated fields
  (`gen-address`, `fee-percentage`) dropped from the type: the raw dict still carries
  them (`truewire_core.validation.TypedDict` tolerates undocumented fields), so validation
  succeeding here proves the drop doesn't break the response, just leaves them untyped.
  """
  async with client:
    methods = await client.spot.funding.deposit_methods(asset='USDC')
    assert any(method['limit'] is False for method in methods)


@pytest.mark.asyncio
async def test_trades_history_paged_walks_multiple_pages(client):
  """`trades_history` declares `pagination` (`offset`, terminated by an item-counted
  `total`) and now generates a real `trades_history_paged` iterator: moving `limit`
  from `requestBody` to `parameters`/`in: 'query'` (production_standards.md S18) let
  the shared `Generator.paged_size_default` find `limit`'s declared `default` (50),
  which is what the walk needs to advance `ofs` by on every call, including the ones
  where the caller passes no explicit `limit`.

  Exercises a real 2-page walk against the mock server (`paged_page1`/`paged_page2`
  recorded examples, `count: 3` total across the two pages, `limit=2`): page 1 returns
  2 trades and isn't enough to cover `count`, so the walk advances `ofs` from 0 to 2 and
  fetches page 2, which returns the remaining 1 trade and satisfies `count`, stopping
  the walk after exactly 2 pages -- not just 1, which couldn't distinguish a correct
  walk from one that happens to terminate immediately.
  """
  async with client:
    assert hasattr(client.spot.account, 'trades_history_paged')
    pages = [
      page async for page in client.spot.account.trades_history_paged(limit=2)
    ]
    assert len(pages) == 2
    assert [page['count'] for page in pages] == [3, 3]
    all_trades = {txid: trade for page in pages for txid, trade in page['trades'].items()}
    assert len(all_trades) == 3
    assert set(all_trades) == {
      'TP1AAA-11111-AAAAAA', 'TP1AAA-22222-BBBBBB', 'TP1AAA-33333-CCCCCC',
    }
