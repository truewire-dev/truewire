"""
Exercise `truewire.standards.paged_shape.check_paged_shape` (S24) against synthetic `.py`
fixtures, never `clients/` -- per the isolation contract `test_mock.py`'s module docstring
records, this must pass in a checkout with zero clients.
"""
from pathlib import Path

import pytest

from truewire.standards.paged_shape import check_paged_shape


def write_module(root: Path, *, name: str, source: str) -> None:
  """
  Write one `.py` file under a synthetic `pkg/src` tree.

  Args:
    root: `pkg/src` directory; created if absent.
    name: File name, e.g. `'orders.py'`.
    source: Full file contents.
  """
  root.mkdir(parents=True, exist_ok=True)
  (root / name).write_text(source)


def test_async_generator_paged_method_is_flagged(tmp_path):
  """The retired shape: a `_paged` method rendered as a bare async generator."""
  write_module(
    tmp_path / 'pkg' / 'src', name='orders.py', source='''"""Orders."""
from typing_extensions import AsyncIterator


class Orders:
  """Orders."""

  async def orders_paged(self) -> AsyncIterator[dict]:
    """Walk."""
    yield {}

  async def orders(self) -> dict:
    """One page."""
    return {}
''',
  )

  findings = check_paged_shape(tmp_path / 'pkg' / 'src')

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S24'
  assert findings[0]['severity'] == 'error'
  assert findings[0]['location'] == 'pkg/src/orders.py:8'


def test_a_paged_named_endpoint_without_a_sibling_is_not_a_walk(tmp_path):
  """A `futures_ip_rate_limits_paged` is a single-request endpoint the API itself
  names that way; with no `futures_ip_rate_limits` sibling to drive, it is not a walk."""
  write_module(
    tmp_path / 'pkg' / 'src', name='limits.py', source='''"""Limits."""


class Limits:
  """Limits."""

  async def futures_ip_rate_limits_paged(self) -> dict:
    """One request."""
    return {}
''',
  )

  assert check_paged_shape(tmp_path / 'pkg' / 'src') == []


def test_paginated_response_paged_method_is_clean(tmp_path):
  write_module(
    tmp_path / 'pkg' / 'src', name='orders.py', source='''"""Orders."""
from truewire_core import PaginatedResponse


class Orders:
  """Orders."""

  def orders_paged(self) -> PaginatedResponse[dict, int]:
    """Walk."""
    ...
''',
  )

  assert check_paged_shape(tmp_path / 'pkg' / 'src') == []


def test_unannotated_paged_method_is_flagged(tmp_path):
  """No annotation at all is not a pass either: the shape has to be stated."""
  write_module(
    tmp_path / 'pkg' / 'src', name='orders.py', source='''"""Orders."""


class Orders:
  """Orders."""

  def orders_paged(self):
    """Walk."""
    ...

  async def orders(self) -> dict:
    """One page."""
    return {}
''',
  )

  findings = check_paged_shape(tmp_path / 'pkg' / 'src')

  assert [f['location'] for f in findings] == ['pkg/src/orders.py:7']


def test_non_paged_methods_are_ignored(tmp_path):
  write_module(
    tmp_path / 'pkg' / 'src', name='orders.py', source='''"""Orders."""
from typing_extensions import AsyncIterator


class Orders:
  """Orders."""

  async def orders(self) -> dict:
    """One page."""
    return {}

  async def stream(self) -> AsyncIterator[dict]:
    """A stream, not a walk."""
    yield {}
''',
  )

  assert check_paged_shape(tmp_path / 'pkg' / 'src') == []


def test_empty_directory_produces_no_findings(tmp_path):
  (tmp_path / 'pkg' / 'src').mkdir(parents=True)
  assert check_paged_shape(tmp_path / 'pkg' / 'src') == []


@pytest.mark.parametrize('annotation', [
  'PaginatedResponse',
  'PaginatedResponse[int]',
  'truewire_core.PaginatedResponse',
  'truewire_core.PaginatedResponse[int]',
  "'PaginatedResponse'",
  "'PaginatedResponse[int]'",
  "'truewire_core.PaginatedResponse'",
  "'truewire_core.PaginatedResponse[int]'",
])
def test_supported_return_annotations_with_a_sibling(tmp_path, annotation):
  root = tmp_path / 'src' / 'pkg'
  write_module(root, name='rows.py', source=f'''
class Rows:
  async def rows(self): ...
  def rows_paged(self) -> {annotation}: ...
''')
  assert check_paged_shape(root) == []


@pytest.mark.parametrize('annotation', [
  'int', "'AsyncIterator[int]'", "'PaginatedResponse['", 'list[PaginatedResponse[int]]',
])
def test_other_or_invalid_string_annotations_are_flagged(tmp_path, annotation):
  root = tmp_path / 'src' / 'pkg'
  write_module(root, name='rows.py', source=f'''
class Rows:
  async def rows(self): ...
  def rows_paged(self) -> {annotation}: ...
''')
  findings = check_paged_shape(root)
  assert [f['location'] for f in findings] == ['src/pkg/rows.py:4']


def test_inherited_sibling_and_unrelated_paged_endpoint(tmp_path):
  root = tmp_path / 'src' / 'pkg'
  write_module(root, name='rows.py', source='''
class Base:
  async def trades(self): ...
class Middle(Base): pass
class Other: pass
class Derived(Other, Middle):
  async def trades_paged(self):
    yield 1
  async def venue_paged(self):
    return {}
''')
  findings = check_paged_shape(root)
  assert [f['location'] for f in findings] == ['src/pkg/rows.py:7']
  assert '`trades_paged`' in findings[0]['message']


@pytest.mark.parametrize('import_line, base', [
  ('from .bases import Base', 'Base'),
  ('from pkg.bases import Base as Parent', 'Parent'),
  ('from . import bases', 'bases.Base'),
  ('import pkg.bases', 'pkg.bases.Base'),
  ('import pkg.bases as bases', 'bases.Base[int]'),
  ('from .exports import Exported', 'Exported'),
])
def test_inherited_sibling_in_another_module(tmp_path, import_line, base):
  root = tmp_path / 'src' / 'pkg'
  write_module(root, name='bases.py', source='''
raise RuntimeError('the checker must never execute client code')
class Base:
  async def trades(self): ...
''')
  write_module(root / 'exports', name='__init__.py', source='from ..bases import Base as Exported\n')
  write_module(root, name='rows.py', source=f'''
{import_line}
class Derived({base}):
  async def trades_paged(self):
    yield 1
''')
  findings = check_paged_shape(root)
  assert [f['location'] for f in findings] == ['src/pkg/rows.py:4']


def test_external_and_unrelated_bases_do_not_supply_a_sibling(tmp_path):
  root = tmp_path / 'src' / 'pkg'
  write_module(root, name='unrelated.py', source='''
class Base:
  def trades(self): ...
''')
  write_module(root, name='rows.py', source='''
from external import Base
class Derived(Base):
  async def trades_paged(self):
    return {}
''')
  assert check_paged_shape(root) == []


def test_inheritance_and_import_cycles_terminate(tmp_path):
  root = tmp_path / 'src' / 'pkg'
  write_module(root, name='bases.py', source='from .rows import Alias\n')
  write_module(root, name='rows.py', source='''
from .bases import Alias
class First(Second, Alias):
  async def trades_paged(self):
    yield 1
class Second(First):
  async def trades(self): ...
''')
  findings = check_paged_shape(root)
  assert [f['location'] for f in findings] == ['src/pkg/rows.py:4']
