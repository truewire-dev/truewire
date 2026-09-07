"""
Exercise `truewire.standards.no_call.check_no_call_methods` (S29) against synthetic `.py`
fixtures, never `clients/` -- per the isolation contract `test_mock.py`'s module docstring
records, this must pass in a checkout with zero clients.
"""
from pathlib import Path

from truewire.standards.no_call import check_no_call_methods


def write_module(root: Path, *, name: str, source: str) -> None:
  """
  Write one `.py` file under a synthetic `pkg/src` tree.

  Args:
    root: `pkg/src` directory; created if absent.
    name: File name, e.g. `'widget.py'`.
    source: Full file contents.
  """
  root.mkdir(parents=True, exist_ok=True)
  (root / name).write_text(source)


def test_call_method_is_flagged(tmp_path):
  """A class defining `def __call__` is exactly S29's case."""
  write_module(
    tmp_path / 'pkg' / 'src', name='trade.py', source='''"""Trade endpoint."""


class Trade:
  """Trade endpoint."""

  def __call__(self, symbol: str):
    """Place a trade."""
    ...
''',
  )

  findings = check_no_call_methods(tmp_path / 'pkg' / 'src')

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S29'
  assert findings[0]['severity'] == 'error'
  assert findings[0]['location'] == 'pkg/src/trade.py:7'


def test_async_call_method_is_flagged(tmp_path):
  """`async def __call__` is the same violation as a plain `def __call__`."""
  write_module(
    tmp_path / 'pkg' / 'src', name='trade.py', source='''"""Trade endpoint."""


class Trade:
  """Trade endpoint."""

  async def __call__(self, symbol: str):
    """Place a trade."""
    ...
''',
  )

  findings = check_no_call_methods(tmp_path / 'pkg' / 'src')

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S29'


def test_named_method_produces_no_finding(tmp_path):
  """A real, distinctly-named method is exactly what S29 asks for instead -- clean."""
  write_module(
    tmp_path / 'pkg' / 'src', name='trade.py', source='''"""Trade endpoint."""


class Trade:
  """Trade endpoint."""

  def trade(self, symbol: str):
    """Place a trade."""
    ...
''',
  )

  assert check_no_call_methods(tmp_path / 'pkg' / 'src') == []


def test_module_level_call_function_is_not_a_method_and_is_ignored(tmp_path):
  """A bare module-level function literally named `__call__` isn't a method (it's not
  inside any class body) and isn't this rule's concern."""
  write_module(
    tmp_path / 'pkg' / 'src', name='helpers.py', source='''"""Helpers."""


def __call__(x):
  """Not a method."""
  return x
''',
  )

  assert check_no_call_methods(tmp_path / 'pkg' / 'src') == []


def test_every_call_method_across_files_is_reported(tmp_path):
  """Two separate offending files are two separate findings, not just the first."""
  write_module(
    tmp_path / 'pkg' / 'src', name='trade.py', source='''"""Trade."""


class Trade:
  """Trade."""

  def __call__(self):
    """Place a trade."""
    ...
''',
  )
  write_module(
    tmp_path / 'pkg' / 'src', name='cancel.py', source='''"""Cancel."""


class Cancel:
  """Cancel."""

  def __call__(self):
    """Cancel a trade."""
    ...
''',
  )

  findings = check_no_call_methods(tmp_path / 'pkg' / 'src')
  locations = {f['location'] for f in findings}

  assert locations == {'pkg/src/trade.py:7', 'pkg/src/cancel.py:7'}


def test_empty_directory_produces_no_findings(tmp_path):
  """A `pkg/src` with no `.py` files at all has nothing to flag."""
  (tmp_path / 'pkg' / 'src').mkdir(parents=True)
  assert check_no_call_methods(tmp_path / 'pkg' / 'src') == []
