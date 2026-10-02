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
from truewire.spec.endpoint import (
  dotted_path, last_row_field, last_row_field_prose, path_segments, read_dotted_path, seek_move_prose,
)


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


class TestLastRowFieldProse:
  """How a generated doc names a `seek` cursor field: never in spec-path syntax."""

  def test_a_named_field_reads_as_itself(self):
    assert last_row_field_prose('[-1].properties.timestamp') == '`properties.timestamp`'
    assert last_row_field_prose('[-1].properties.timestamp', value=True) == '`properties.timestamp` value'

  def test_an_index_reads_as_the_rows_element(self):
    """TRU-119: an element of the row, never "position 0", which read as the page's."""
    assert last_row_field_prose('[-1][0]') == 'first element'
    assert last_row_field_prose('[-1][0]', value=True) == 'first element'
    assert last_row_field_prose('[-1][0][1]') == "first element's second element"

  def test_a_mixed_path_keeps_no_brackets(self):
    assert last_row_field_prose('[-1].trade.legs[2].id') == "`trade.legs`'s third element's `id`"

  # review/TRU-116: shapes the grammar accepts that the helper renders wrong.

  def test_a_bare_row_never_reads_as_empty_backticks(self):
    """`[-1]` is a supported cursor (`test_codegen_paged.py`'s `bare-row` case)."""
    assert '``' not in last_row_field_prose('[-1]')
    assert '``' not in last_row_field_prose('[-1]', value=True)
    assert last_row_field_prose('[-1]') == 'row'
    assert last_row_field_prose('[-1]', value=True) == 'value'

  def test_a_negative_position_reads_as_prose(self):
    assert '-1' not in last_row_field_prose('[-1][-1]')
    assert last_row_field_prose('[-1][-1]') == 'last element'
    assert last_row_field_prose('[-1][-2]') == 'second-to-last element'


class TestSeekMoveProse:
  """Where a generated `seek` walker's doc says the bound moves, as the runtimes move it."""

  def move(self, path='[-1].t', *, descending=False, ordered=True, cap=True, span=False):
    return seek_move_prose(path, descending=descending, ordered=ordered, cap=cap, span=span)

  def test_a_known_cap_moves_on_a_full_page(self):
    assert self.move() == 'to the latest `t` of each page that came back full'
    assert self.move(descending=True) == 'to the earliest `t` of each page that came back full'

  def test_an_element_is_anchored_on_the_rows(self):
    """TRU-119: "the latest first element of each page" would name the page's first row."""
    assert self.move('[-1][0]') == 'to the latest first element among the rows of each page that came back full'

  def test_a_bare_row_is_the_row(self):
    assert self.move('[-1]') == 'to the latest row of each page that came back full'

  def test_an_unordered_key_is_the_last_rows(self):
    """TRU-125: a plain string id is taken from the last row in wire order."""
    assert self.move('[-1].id', ordered=False) == 'to the `id` of the last row of each page that came back full'
    assert self.move('[-1]', ordered=False) == 'to the last row of each page that came back full'

  def test_no_cap_moves_on_every_page(self):
    """TRU-117: with no cap known, every page that brings a new key moves the bound."""
    assert self.move(cap=False) == 'to the latest `t` of each page'

  def test_a_cap_known_only_from_the_callers_size_says_so(self):
    assert self.move(cap='limit') == (
      'to the latest `t` of each page that came back full, or of every page while `limit` is unset'
    )

  def test_a_span_moves_past_a_short_page(self):
    """TRU-117: with a span, a short page moves the bound to the edge of its range."""
    assert self.move(span=True) == (
      'to the latest `t` of each page that came back full, and to the edge of the range it '
      'requested after a short one'
    )
    assert self.move(cap=False, span=True).endswith('after a page that brings nothing new')
    assert self.move(cap='limit', span=True).endswith('after any other page')


SEEK_BASE = {
  'strategy': 'seek', 'cursor': {'field': '[-1].id', 'unique': True},
  'bound': {'start': 'from_id'}, 'anchor': 'start', 'size': {'parameter': 'limit'},
}


class TestSeekCursorSyntax:
  """`SeekCursor.field` (`LastRowPath`) narrows the general grammar to indexing the last
  row -- retired from the old bespoke `last:<field>` prefix."""

  def test_accepts_the_new_bracket_index_syntax(self):
    pagination = SeekPagination.model_validate(SEEK_BASE)
    assert pagination.cursor.field == '[-1].id'

  def test_accepts_a_deeper_dotted_path(self):
    pagination = SeekPagination.model_validate(
      {**SEEK_BASE, 'cursor': {'field': '[-1].trade.id', 'unique': True}}
    )
    assert pagination.cursor.field == '[-1].trade.id'

  def test_rejects_the_old_last_prefix_syntax(self):
    """`last:id` isn't even a well-formed dotted-key/bracket-index path any more (`:` was
    never part of the general grammar, only this type's own retired bespoke prefix), so
    it fails `dotted_path` itself before the last-row-index check ever runs."""
    with pytest.raises(ValidationError, match='not a dotted key with optional bracket indices'):
      SeekPagination.model_validate(
        {**SEEK_BASE, 'cursor': {'field': 'last:id', 'unique': True}}
      )

  def test_rejects_a_bare_field_with_no_index(self):
    with pytest.raises(ValidationError, match='does not index the last row'):
      SeekPagination.model_validate(
        {**SEEK_BASE, 'cursor': {'field': 'id', 'unique': True}}
      )

  def test_rejects_an_index_other_than_the_last(self):
    """Only `[-1]` is admitted -- an arbitrary index is still refused."""
    with pytest.raises(ValidationError, match='does not index the last row'):
      SeekPagination.model_validate(
        {**SEEK_BASE, 'cursor': {'field': '[0].id', 'unique': True}}
      )
