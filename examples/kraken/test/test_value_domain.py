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
async def test_trades_history_pages_by_hand_with_ofs_and_count(client):
  """`trades_history` declares no `pagination` (ADR 0013): its rows are a map keyed by
  trade id, and a `PaginatedResponse` needs an array row collection to yield, so no
  `trades_history_paged` is generated. A caller pages with `ofs`/`count` instead.

  Exercises a real 2-page walk against the mock server (`paged_page1`/`paged_page2`
  recorded examples, `count: 3` total across the two pages, `limit=2`): page 1 returns
  2 trades and isn't enough to cover `count`, so `ofs` moves from 0 to 2 and page 2
  returns the remaining 1 trade.
  """
  async with client:
    assert not hasattr(client.spot.account, 'trades_history_paged')
    pages = []
    ofs = 0
    while True:
      page = await client.spot.account.trades_history(limit=2, ofs=ofs)
      pages.append(page)
      ofs += len(page['trades'])
      if not page['trades'] or ofs >= page['count']:
        break
    assert len(pages) == 2
    assert [page['count'] for page in pages] == [3, 3]
    all_trades = {txid: trade for page in pages for txid, trade in page['trades'].items()}
    assert len(all_trades) == 3
    assert set(all_trades) == {
      'TP1AAA-11111-AAAAAA', 'TP1AAA-22222-BBBBBB', 'TP1AAA-33333-CCCCCC',
    }
