"""`truewire surface --language rust`: every spec reconciled against the Rust package on
disk -- generated, hand-written (an inherent `impl` in the crate), absent, or a gap."""
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.surface import BackendUnavailable, reconcile

EXAMPLES = Path(__file__).parents[3] / 'examples'


def _kraken() -> Path:
  root = EXAMPLES / 'kraken'
  if not (root / 'src' / 'kraken' / 'lib.rs').is_file():
    pytest.skip('examples/kraken has no generated Rust package checked out')
  return root


def test_kraken_reconciles_with_no_gaps():
  result = reconcile(_kraken(), language='rust')
  assert result.gaps == [] and result.missing_validate == []
  assert 'trading_ws.add_order' in result.generated and 'streams.market_data.ticker' in result.generated
  # Declared hand-written for Python; the Rust package has a function by that name.
  assert result.handwritten == ['spot.account.retrieve_export']
  assert result.total == 75


def test_gaps_name_the_missing_module_method_twin_and_symbol(tmp_path: Path):
  root = tmp_path / 'kraken'
  shutil.copytree(_kraken(), root, ignore=shutil.ignore_patterns('target', 'node_modules', '.truewire'))
  package = root / 'src' / 'kraken'
  (package / 'spot' / 'market_data' / 'time.rs').unlink()
  ticker = package / 'spot' / 'market_data' / 'ticker.rs'
  ticker.write_text(ticker.read_text().replace('fn ticker_raw(', 'fn ticker_unvalidated('))
  depth = package / 'spot' / 'market_data' / 'depth.rs'
  depth.write_text(depth.read_text().replace('fn depth(', 'fn order_book(').replace('.depth_raw(', '.order_book_raw('))
  for rs in package.rglob('*.rs'):
    text = rs.read_text()
    if 'fn retrieve_export' in text:
      rs.write_text(text.replace('fn retrieve_export', 'fn fetch_export'))
  result = reconcile(root, language='rust')
  faults = {gap.function: gap.fault for gap in result.gaps}
  assert faults == {
    'spot.market_data.time': 'no_module',
    'spot.market_data.depth': 'no_method',
    'spot.account.retrieve_export': 'no_symbol',
  }
  assert result.missing_validate == ['spot.market_data.ticker']


def test_a_project_without_a_rust_section_is_unavailable():
  with pytest.raises(BackendUnavailable):
    reconcile(EXAMPLES / 'github' / 'spec' / '..' / '..' / '..' / 'packages' / 'truewire' / 'test' / 'fixtures' / 'codegen_fixture_client', language='rust')


def test_the_cli_reports_rust_callables():
  result = CliRunner().invoke(app, ['surface', '--project', str(_kraken()), '--language', 'rust'])
  assert result.exit_code == 0, result.output
  assert 'Callables: 74 generated, 1 hand-written, 0 declared absent, over 75 spec(s)' in result.output
