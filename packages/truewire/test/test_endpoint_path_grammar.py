"""
`docs/pagination.md` §3: response paths admit bracket indices (`key[N]`, `[N].key2`,
composable to a small depth) alongside dotted keys, closing the gap `seek`'s old bespoke
`last:<field>` prefix didn't cover -- most `window`-strategy rows are positional tuples (a
candle's timestamp at a fixed array index), not the named objects that prefix was built
for. Exercises the grammar itself (`dotted_path`/`path_segments`), its resolution
(`read_dotted_path`), and `seek`'s own narrowing on top of it (`LastRowPath`).
"""
from pydantic import ValidationError
import pytest

from truewire.spec import SeekPagination
from truewire.spec.endpoint import dotted_path, last_row_field, path_segments, read_dotted_path


class TestDottedPath:
  """The path grammar itself: dotted keys, optionally carrying bracket indices."""

  @pytest.mark.parametrize('value', [
    'pageKey',
    'data.totalPage',
    'key[0]',
    'key[-1]',
    'key[0].key2',
    '[-1].id',
    '[-1][0]',
    '[-1].trade.id',
  ])
  def test_valid_paths_are_accepted(self, value: str):
    assert dotted_path(value) == value

  @pytest.mark.parametrize('value', [
    'result.*',
    '$.result',
    'key[abc]',
    'key[',
    'key]',
    'key[0',
    '.key',
    'key.',
    '',
  ])
  def test_invalid_paths_are_rejected(self, value: str):
    with pytest.raises(ValueError):
      dotted_path(value)


class TestPathSegments:
  """Tokenizing an already-validated path into its ordered key/index segments."""

  def test_a_bare_key_is_one_segment(self):
    assert path_segments('pageKey') == [('key', 'pageKey')]

  def test_dotted_keys_split_on_the_dot(self):
    assert path_segments('data.totalPage') == [('key', 'data'), ('key', 'totalPage')]

  def test_a_positive_index_is_its_own_segment(self):
    assert path_segments('rows[0]') == [('key', 'rows'), ('index', 0)]

  def test_a_negative_index_is_its_own_segment(self):
    assert path_segments('rows[-1]') == [('key', 'rows'), ('index', -1)]

  def test_index_then_key_composes(self):
    """`[-1].id`: the row collection's last element, then its `id` field."""
    assert path_segments('[-1].id') == [('index', -1), ('key', 'id')]

  def test_key_then_index_composes(self):
    """`envelope[0]`: a key, then a positional element of it."""
    assert path_segments('envelope[0]') == [('key', 'envelope'), ('index', 0)]

  def test_index_then_index_composes(self):
    """`[-1][0]`: the last row, then its own tuple position 0 -- binance's candle shape."""
    assert path_segments('[-1][0]') == [('index', -1), ('index', 0)]

  def test_empty_path_has_no_segments(self):
    assert path_segments('') == []


class TestReadDottedPath:
  """Resolving a path against real JSON-shaped data."""

  def test_reads_a_plain_key(self):
    assert read_dotted_path({'total': 3}, 'total') == 3

  def test_reads_a_dotted_key(self):
    assert read_dotted_path({'data': {'totalPage': 3}}, 'data.totalPage') == 3

  def test_reads_a_positive_index(self):
    assert read_dotted_path({'rows': [1, 2, 3]}, 'rows[0]') == 1

  def test_reads_a_negative_index(self):
    assert read_dotted_path({'rows': [1, 2, 3]}, 'rows[-1]') == 3

  def test_reads_an_index_then_a_key(self):
    assert read_dotted_path([{'id': 1}, {'id': 2}], '[-1].id') == 2

  def test_reads_a_key_then_an_index(self):
    assert read_dotted_path({'candle': [1690000000000, '100.0']}, 'candle[0]') == 1690000000000

  def test_reads_an_index_then_an_index(self):
    assert read_dotted_path([[1690000000000, '100.0'], [1690000060000, '101.0']], '[-1][0]') == 1690000060000

  def test_missing_key_returns_none(self):
    assert read_dotted_path({'data': {}}, 'data.totalPage') is None

  def test_out_of_range_index_returns_none(self):
    assert read_dotted_path({'rows': [1]}, 'rows[5]') is None
    assert read_dotted_path({'rows': [1]}, 'rows[-5]') is None

  def test_indexing_a_non_sequence_returns_none(self):
    assert read_dotted_path({'rows': {'a': 1}}, 'rows[0]') is None

  def test_indexing_a_bare_string_returns_none(self):
    """A `str`/`bytes` is technically a `Sequence` but never a JSON array."""
    assert read_dotted_path({'rows': 'abc'}, 'rows[0]') is None

  def test_empty_path_returns_the_value_unread(self):
    value = {'a': 1}
    assert read_dotted_path(value, '') is value


class TestLastRowField:
  """Stripping a `LastRowPath`'s fixed `[-1]` prefix to get the path relative to one row."""

  def test_strips_the_bracket_and_dot(self):
    assert last_row_field('[-1].id') == 'id'

  def test_strips_a_deeper_dotted_path(self):
    assert last_row_field('[-1].trade.id') == 'trade.id'

  def test_strips_a_trailing_index_with_no_dot(self):
    assert last_row_field('[-1][0]') == '[0]'


SEEK_BASE = {
  'strategy': 'seek', 'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
  'done': {'kind': 'short_page'}, 'size': {'parameter': 'limit'},
}


class TestSeekCursorSyntax:
  """`SeekCursor.from_` (`LastRowPath`) narrows the general grammar to indexing the last
  row -- retired from the old bespoke `last:<field>` prefix."""

  def test_accepts_the_new_bracket_index_syntax(self):
    pagination = SeekPagination.model_validate(SEEK_BASE)
    assert pagination.cursor.from_ == '[-1].id'

  def test_accepts_a_deeper_dotted_path(self):
    pagination = SeekPagination.model_validate(
      {**SEEK_BASE, 'cursor': {'parameter': 'from_id', 'from': '[-1].trade.id'}}
    )
    assert pagination.cursor.from_ == '[-1].trade.id'

  def test_rejects_the_old_last_prefix_syntax(self):
    """`last:id` isn't even a well-formed dotted-key/bracket-index path any more (`:` was
    never part of the general grammar, only this type's own retired bespoke prefix), so
    it fails `dotted_path` itself before the last-row-index check ever runs."""
    with pytest.raises(ValidationError, match='not a dotted key with optional bracket indices'):
      SeekPagination.model_validate(
        {**SEEK_BASE, 'cursor': {'parameter': 'from_id', 'from': 'last:id'}}
      )

  def test_rejects_a_bare_field_with_no_index(self):
    with pytest.raises(ValidationError, match='does not index the last row'):
      SeekPagination.model_validate(
        {**SEEK_BASE, 'cursor': {'parameter': 'from_id', 'from': 'id'}}
      )

  def test_rejects_an_index_other_than_the_last(self):
    """Only `[-1]` is admitted -- an arbitrary index is still refused."""
    with pytest.raises(ValidationError, match='does not index the last row'):
      SeekPagination.model_validate(
        {**SEEK_BASE, 'cursor': {'parameter': 'from_id', 'from': '[0].id'}}
      )
