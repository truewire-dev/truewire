"""The docs type checker: every python example checks against the package it documents.

Synthetic fixtures only — nothing here reads `clients/`, so the suite passes in a checkout
with zero clients. Most tests substitute the pyright invocation, because depending on `npx`
would make the whole file conditional on a network fetch; they cover extraction, wrapping,
severity filtering and the position mapping, which is where the logic actually lives.

`test_pyright_checks_the_documented_package` is the one test that runs pyright for real. It
builds a four-file package under `tmp_path` and checks doc blocks against it, so it needs no
network beyond whatever `npx` has already cached, and skips cleanly when it has cached
nothing.
"""

from pathlib import Path
import subprocess

import pytest

from truewire.docs.check import (
  Diagnostic, PyrightUnavailable, Runner, check_docs, examples, quickstart_examples, run_pyright,
)

PACKAGE = '''\
"""A synthetic venue package, small enough to type-check inside a test."""

from typing import TypedDict


class Ticker(TypedDict):
  """One ticker row."""
  symbol: str
  last: str


class Market:
  """Market data endpoints."""

  async def ticker(self, *, symbol: str) -> Ticker:
    """Return the ticker for one symbol."""
    ...


class Venue:
  """The synthetic client."""

  market: Market

  @classmethod
  def public(cls) -> 'Venue':
    """Build a client with no credentials."""
    ...

  async def __aenter__(self) -> 'Venue':
    """Open the client."""
    ...

  async def __aexit__(self, *args: object) -> None:
    """Close the client."""
    ...
'''


@pytest.fixture
def client(tmp_path: Path) -> Path:
  """A client directory: a README, a docs tree, and a package for blocks to check against."""
  (tmp_path / 'docs' / 'how-to').mkdir(parents=True)
  (tmp_path / 'pkg' / 'src' / 'venue').mkdir(parents=True)
  (tmp_path / 'pkg' / 'src' / 'venue' / '__init__.py').write_text(PACKAGE)
  (tmp_path / 'truewire.toml').write_text('[project]\nname = "venue"\n[python]\nsrc = "pkg/src"\n[python.cores.default]\nbase = "venue.core:Endpoint"\n')
  (tmp_path / 'README.md').write_text('# Venue\n\nA typed client.\n')
  return tmp_path


def page(client: Path, name: str, body: str) -> Path:
  """Write one markdown page under `docs/` and return its path."""
  path = client / 'docs' / name
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(body)
  return path


def quickstart(client: Path, text: str) -> Path:
  """Write `docs/quickstart.yaml` and return its path."""
  path = client / 'docs' / 'quickstart.yaml'
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text)
  return path


QUICKSTART = (
  'first_call:\n'                            # line 1
  '  body: Say hi.\n'                        # line 2
  '  code: |\n'                              # line 3
  '    async with Venue.public() as client:\n'  # line 4
  '      pass\n'                             # line 5
  '\n'                                       # line 6
  'how_tos:\n'                               # line 7
  '  - title: One\n'                         # line 8
  '    body: Do a thing.\n'                  # line 9
  '    code: |\n'                            # line 10
  '      await client.market.gone()\n'       # line 11
)
"""A `first_call` with one `how_to` — both use top-level `async with`/`await`, so both
need the coroutine wrapper, matching every real `quickstart.yaml` in the repo."""


def runner_at(
  block: int, line: int, character: int, *,
  severity: str = 'error', rule: str = 'reportAttributeAccessIssue',
) -> Runner:
  """A stand-in pyright reporting one diagnostic at a 0-indexed position of one block."""
  def run(directory: Path, *, cwd: Path) -> list[dict]:
    return [{
      'file': str(directory / f'block_{block:04d}.py'),
      'severity': severity,
      'rule': rule,
      'message': 'synthetic diagnostic',
      'range': {
        'start': {'line': line, 'character': character},
        'end': {'line': line, 'character': character + 1},
      },
    }]
  return run


def refuse(directory: Path, *, cwd: Path) -> list[dict]:
  """A runner that fails the test if it is called at all."""
  raise AssertionError('pyright should not have been invoked')


def test_a_python_block_is_extracted_with_its_page_and_line(client: Path):
  page(client, 'index.md', '# Venue\n\nCall it:\n\n```python\nx = 1\n```\n')
  found = [e for e in examples(client) if e.path.name == 'index.md']
  assert len(found) == 1
  assert (found[0].index, found[0].line, found[0].code) == (1, 6, 'x = 1\n')


def test_a_top_level_await_block_is_wrapped_rather_than_a_syntax_error(client: Path):
  """The defining case: 116 of bitget's 133 blocks do not compile as written."""
  page(client, 'index.md', '```python\nasync with Venue.public() as c:\n  await c.market.ticker(symbol="X")\n```\n')
  found = [e for e in examples(client) if e.path.name == 'index.md']
  assert found[0].wrapped
  assert found[0].source.startswith('import asyncio\nasync def _main():\n  async with')
  compile(found[0].source, '<test>', 'exec')


def test_a_block_that_already_compiles_is_not_wrapped(client: Path):
  page(client, 'index.md', '```python\nx = 1\n```\n')
  assert [e.wrapped for e in examples(client) if e.path.name == 'index.md'] == [False]


def test_a_syntactically_broken_block_is_left_unwrapped(client: Path):
  """Wrapping would not save it, and pyright reports it better at its true position."""
  page(client, 'index.md', '```python\ndef (:\n```\n')
  assert [e.wrapped for e in examples(client) if e.path.name == 'index.md'] == [False]


def test_only_python_blocks_are_collected(client: Path):
  page(client, 'index.md', '```bash\nls\n```\n\n```\nplain\n```\n\n```python\nx = 1\n```\n')
  assert [e.line for e in examples(client) if e.path.name == 'index.md'] == [10]


def test_an_info_string_with_attributes_still_counts(client: Path):
  page(client, 'index.md', '```python title="example.py"\nx = 1\n```\n')
  assert len([e for e in examples(client) if e.path.name == 'index.md']) == 1


def test_blocks_are_numbered_within_their_own_page(client: Path):
  page(client, 'index.md', '```python\nx = 1\n```\n\n```python\ny = 2\n```\n')
  page(client, 'how-to/one.md', '```python\nz = 3\n```\n')
  numbered = {(e.path.name, e.index) for e in examples(client)}
  assert ('index.md', 1) in numbered
  assert ('index.md', 2) in numbered
  assert ('one.md', 1) in numbered


def test_quickstart_first_call_and_how_tos_are_extracted(client: Path):
  """`install`/`env_setup` are shell/dotenv, not python, and contribute nothing."""
  quickstart(client, QUICKSTART)
  found = quickstart_examples(client)
  assert [(e.index, e.line, e.code, e.prefix) for e in found] == [
    (1, 4, 'async with Venue.public() as client:\n  pass\n', ''),
    (2, 11, 'await client.market.gone()\n', 'async with Venue.public() as client:\n  pass\n'),
  ]


def test_quickstart_examples_are_included_in_the_full_set(client: Path):
  quickstart(client, QUICKSTART)
  assert len([e for e in examples(client) if e.path.name == 'quickstart.yaml']) == 2


def test_quickstart_with_no_first_call_or_how_tos_contributes_nothing(client: Path):
  quickstart(client, 'install:\n  body: Install it.\n  code: pip install venue\n')
  assert quickstart_examples(client) == []


def test_a_missing_quickstart_yaml_contributes_nothing(client: Path):
  assert quickstart_examples(client) == []


def test_quickstart_how_to_diagnostic_maps_to_its_own_yaml_line(client: Path):
  quickstart(client, QUICKSTART)
  _, diagnostics = check_docs(client, runner=runner_at(1, line=4, character=2))
  assert [(d.example.path.name, d.line) for d in diagnostics] == [('quickstart.yaml', 11)]


def test_quickstart_first_call_diagnostic_maps_to_its_own_yaml_line(client: Path):
  quickstart(client, QUICKSTART)
  _, diagnostics = check_docs(client, runner=runner_at(0, line=2, character=2))
  assert [(d.example.path.name, d.line) for d in diagnostics] == [('quickstart.yaml', 4)]


def test_a_diagnostic_inside_a_how_tos_prefix_is_dropped(client: Path):
  """A real defect in `first_call` is reported once, via `first_call`'s own example —
  not once per `how_to` that carries it as context."""
  quickstart(client, QUICKSTART)
  _, diagnostics = check_docs(client, runner=runner_at(1, line=2, character=2))
  assert diagnostics == []


def test_the_readme_is_checked_too(client: Path):
  (client / 'README.md').write_text('# Venue\n\n```python\nx = 1\n```\n')
  assert [e.path.name for e in examples(client)] == ['README.md']


def test_a_position_in_a_plain_block_maps_back_to_the_markdown_line(client: Path):
  path = page(client, 'index.md', '# Venue\n\n```python\nx = 1\ny = 2\n```\n')
  _, diagnostics = check_docs(client, runner=runner_at(0, line=1, character=4))
  assert [(d.example.path, d.line, d.column) for d in diagnostics] == [(path, 5, 5)]


def test_a_position_in_a_wrapped_block_maps_back_through_the_wrapper(client: Path):
  """`block_0000.py:4:7` is the reader-useless form this mapping exists to undo."""
  body = '# Venue\n\n```python\nasync with Venue.public() as c:\n  await c.market.gone()\n```\n'
  path = page(client, 'index.md', body)
  _, diagnostics = check_docs(client, runner=runner_at(0, line=3, character=8))
  assert [(d.example.path, d.line, d.column) for d in diagnostics] == [(path, 5, 7)]
  assert diagnostics[0].example.index == 1


def test_a_diagnostic_on_a_wrapper_line_is_clamped_into_the_block(client: Path):
  """Nothing may point at a line the author never wrote."""
  page(client, 'index.md', '```python\nasync with Venue.public() as c:\n  await c.market.gone()\n```\n')
  _, diagnostics = check_docs(client, runner=runner_at(0, line=0, character=0))
  assert diagnostics[0].line == 2


def test_hints_are_dropped_and_warnings_are_kept(client: Path):
  page(client, 'index.md', '```python\nx = 1\n```\n')
  _, hints = check_docs(client, runner=runner_at(0, 0, 0, severity='information'))
  assert hints == []
  _, warnings = check_docs(client, runner=runner_at(0, 0, 0, severity='warning'))
  assert [d.severity for d in warnings] == ['warning']


def test_a_doc_set_with_no_python_never_invokes_pyright(client: Path):
  page(client, 'index.md', '# Venue\n\nNo examples yet.\n\n```bash\nls\n```\n')
  assert check_docs(client, runner=refuse) == ([], [])


def test_a_missing_npx_is_a_message_not_a_traceback(client: Path, monkeypatch):
  def missing(*args, **kwargs):
    raise FileNotFoundError('npx')
  monkeypatch.setattr(subprocess, 'run', missing)
  with pytest.raises(PyrightUnavailable, match='pyright could not be run'):
    run_pyright(client, cwd=client)


def test_unparseable_pyright_output_is_a_message_not_a_traceback(client: Path, monkeypatch):
  class Result:
    returncode = 127
    stdout = ''
    stderr = 'npm error could not determine executable to run'
  monkeypatch.setattr(subprocess, 'run', lambda *a, **k: Result())
  with pytest.raises(PyrightUnavailable, match='no report'):
    run_pyright(client, cwd=client)


@pytest.fixture(scope='session')
def pyright():
  """Skip when pyright cannot be fetched, so the suite never depends on a network call."""
  try:
    result = subprocess.run(
      ['npx', '--yes', 'pyright', '--version'],
      capture_output=True, text=True, timeout=300,
    )
  except (OSError, subprocess.SubprocessError) as e:
    pytest.skip(f'pyright unavailable: {e}')
  if result.returncode != 0:
    pytest.skip('pyright unavailable: `npx --yes pyright --version` failed')


def by_line(diagnostics: list[Diagnostic]) -> dict[tuple[str, int], str]:
  """Index diagnostics by page and line, for assertions that do not pin pyright's wording."""
  return {(d.example.path.name, d.line): (d.rule or '') for d in diagnostics}


def test_pyright_checks_the_documented_package(client: Path, pyright):
  """The real thing, end to end: real pyright, real package, real position mapping.

  Four blocks on one page. The clean one must stay clean — a checker that flags everything
  is as useless as one that flags nothing — and each of the other three is a defect the
  executing gate could not have caught without a live call.
  """
  page(client, 'how-to/read-tickers.md', (
    '# Read tickers\n'
    '\n'
    '```python\n'                                     # block 1, lines 4-8: clean
    'from venue import Venue\n'
    '\n'
    'async with Venue.public() as client:\n'
    "  ticker = await client.market.ticker(symbol='BTCUSDT')\n"
    "  print(ticker['last'])\n"
    '```\n'
    '\n'
    '```python\n'                                     # block 2, defect on line 15
    'from venue import Venue\n'
    '\n'
    'async with Venue.public() as client:\n'
    "  await client.market.orderbook(symbol='BTCUSDT')\n"
    '```\n'
    '\n'
    '```python\n'                                     # block 3, defect on line 22
    'from venue import Venue\n'
    '\n'
    'async with Venue.public() as client:\n'
    '  await client.market.ticker(symbol=123)\n'
    '```\n'
    '\n'
    '```python\n'                                     # block 4, defect on line 29
    'from venue import Ticker\n'
    '\n'
    "row: Ticker = {'symbol': 'BTC', 'last': '1'}\n"
    "print(row['bogus'])\n"
    '```\n'
  ))
  found, diagnostics = check_docs(client)

  assert [(e.index, e.wrapped) for e in found] == [(1, True), (2, True), (3, True), (4, False)]

  located = by_line(diagnostics)
  assert ('read-tickers.md', 15) in located, f'no such method not caught: {located}'
  assert ('read-tickers.md', 22) in located, f'wrong argument type not caught: {located}'
  assert ('read-tickers.md', 29) in located, f'no such TypedDict key not caught: {located}'

  clean = [d for d in diagnostics if 4 <= d.line <= 9]
  assert clean == [], f'the clean block was flagged: {clean}'
