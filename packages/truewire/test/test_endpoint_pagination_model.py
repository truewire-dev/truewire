"""The pagination models' own load-time rules (ADR 0013): `seek`'s bound/anchor/span
consistency, the `rows` field every `PaginatedResponse`-shaped walk extracts, and
`extra='forbid'` staying load-bearing across them."""

from pydantic import ValidationError
import pytest

from truewire.spec import SeekPagination, TotalDone


def test_total_done_accepts_a_declared_rows_field():
  done = TotalDone.model_validate(
    {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'rows'}
  )
  assert done.rows == 'rows'


def test_total_done_rows_defaults_to_none():
  done = TotalDone.model_validate({'kind': 'total', 'path': 'total', 'counts': 'items'})
  assert done.rows is None


def test_total_done_still_rejects_a_key_from_another_strategy():
  """`extra='forbid'` stays load-bearing -- adding `rows` must not open the door to an
  unrelated stray key silently passing through."""
  with pytest.raises(ValidationError):
    TotalDone.model_validate(
      {'kind': 'total', 'path': 'total', 'counts': 'items', 'cursor': 'nope'}
    )


SEEK_BASE = {
  'strategy': 'seek',
  'cursor': {'field': '[-1][0]', 'unique': True},
  'bound': {'start': 'startTime', 'end': 'endTime'},
  'anchor': 'end',
}
"""bybit's kline shape: both bounds, the venue keeps the newest rows."""


def test_seek_derives_the_moving_and_far_bounds_from_the_anchor():
  pagination = SeekPagination.model_validate(SEEK_BASE)
  assert pagination.moving == 'endTime'
  assert pagination.far == 'startTime'
  assert pagination.descending is True


def test_seek_anchored_to_start_moves_start():
  pagination = SeekPagination.model_validate({**SEEK_BASE, 'anchor': 'start'})
  assert pagination.moving == 'startTime'
  assert pagination.far == 'endTime'
  assert pagination.descending is False


def test_seek_accepts_a_single_bound():
  """bitget's `idLessThan`, mexc's `fromId`: one bound, no far cap."""
  pagination = SeekPagination.model_validate({
    **SEEK_BASE, 'bound': {'end': 'idLessThan'}, 'cursor': {'field': '[-1].id', 'unique': True},
  })
  assert pagination.moving == 'idLessThan'
  assert pagination.far is None


def test_seek_rejects_an_anchor_naming_an_undeclared_bound():
  with pytest.raises(ValidationError, match='`bound.start` is not declared'):
    SeekPagination.model_validate({**SEEK_BASE, 'bound': {'end': 'endTime'}, 'anchor': 'start'})


def test_seek_rejects_a_bound_naming_nothing():
  with pytest.raises(ValidationError, match='at least one'):
    SeekPagination.model_validate({**SEEK_BASE, 'bound': {}})


def test_seek_rejects_both_bounds_naming_one_parameter():
  with pytest.raises(ValidationError, match='two distinct parameters'):
    SeekPagination.model_validate({**SEEK_BASE, 'bound': {'start': 't', 'end': 't'}})


def test_seek_unique_is_required():
  """A venue fact the author has to state -- never defaulted either way."""
  with pytest.raises(ValidationError):
    SeekPagination.model_validate({**SEEK_BASE, 'cursor': {'field': '[-1][0]'}})


def test_seek_cursor_field_requires_the_last_row_index():
  """`SeekCursor.field` is a `LastRowPath`: `Generator.last_row_field` strips the fixed
  `[-1]` prefix unconditionally, so the type itself has to guarantee it's there."""
  with pytest.raises(ValidationError, match='does not index the last row'):
    SeekPagination.model_validate({**SEEK_BASE, 'cursor': {'field': 'openTime', 'unique': True}})


def test_seek_accepts_a_declared_cap_and_span():
  pagination = SeekPagination.model_validate({
    **SEEK_BASE, 'cap': 350,
    'span': {'parameter': 'span', 'default': 21000, 'unit': 's'},
  })
  assert pagination.cap == 350
  assert pagination.span is not None and pagination.span.default == 21000


def test_seek_rejects_a_non_positive_cap():
  with pytest.raises(ValidationError):
    SeekPagination.model_validate({**SEEK_BASE, 'cap': 0})


def test_seek_span_needs_both_bounds():
  with pytest.raises(ValidationError, match='`span` needs both'):
    SeekPagination.model_validate({
      **SEEK_BASE, 'bound': {'end': 'endTime'},
      'span': {'parameter': 'span', 'default': 10, 'unit': 'ms'},
    })


def test_seek_rejects_a_retired_window_key():
  """`order`/`step`/`overlap`/`done` were `window`'s; a declaration still carrying one has
  to fail loud rather than be read as a valid `seek` with the key silently dropped."""
  for stale in ({'order': 'descending'}, {'step': {'unit': 'ms', 'size': 1}},
                {'overlap': {'field': '[-1][0]'}}, {'done': {'kind': 'empty'}}):
    with pytest.raises(ValidationError):
      SeekPagination.model_validate({**SEEK_BASE, **stale})
