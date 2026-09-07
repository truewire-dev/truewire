"""`TotalDone.rows`: a `page`/`offset` walk terminated by a total needs to name its row
collection too, the same reason `AbsentCursorDone` already carries `rows` for `token`
strategy (`docs/spec/authoring.md` rule 8) -- irrelevant to the plain async-generator
`paged_method`, which yields the whole response and never needs to know, but required by
`paged_response_method`'s `PaginatedResponse`-shaped wrapper, which has to extract exactly
that field."""

from pydantic import ValidationError
import pytest

from truewire.spec import SeekPagination, TotalDone, UnchangedDone, WindowPagination


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


def test_unchanged_done_accepts_a_declared_rows_field():
  """Same shape as `ShortPageDone.rows`/`EmptyDone.rows` -- optional, and named only when
  the payload isn't itself the row collection."""
  done = UnchangedDone.model_validate({'kind': 'unchanged', 'rows': 'data'})
  assert done.rows == 'data'


def test_unchanged_done_rows_defaults_to_none():
  done = UnchangedDone.model_validate({'kind': 'unchanged'})
  assert done.rows is None


def test_seek_pagination_accepts_unchanged():
  pagination = SeekPagination.model_validate({
    'strategy': 'seek',
    'cursor': {'parameter': 'before', 'from': '[-1].height'},
    'done': {'kind': 'unchanged'},
  })
  assert pagination.done.kind == 'unchanged'


def test_seek_pagination_rejects_unchanged_alongside_overlap():
  """`overlap`'s own generated walk (`Generator.paged_overlap_seek`) always terminates on
  an empty page and never reads `done.kind` -- an `unchanged` terminator declared beside
  it would be silently ignored rather than change anything, so the combination is refused
  at the model level instead."""
  with pytest.raises(ValidationError):
    SeekPagination.model_validate({
      'strategy': 'seek',
      'cursor': {'parameter': 'before', 'from': '[-1].height'},
      'done': {'kind': 'unchanged'},
      'overlap': {'cap': 500},
    })


def test_seek_pagination_allows_overlap_with_empty():
  """The rejection above is specific to `unchanged`, not to declaring `overlap` and
  `done` together at all -- `overlap`'s own worked example (rule 8) pairs it with
  `empty`."""
  pagination = SeekPagination.model_validate({
    'strategy': 'seek',
    'cursor': {'parameter': 'before', 'from': '[-1].height'},
    'done': {'kind': 'empty'},
    'overlap': {'cap': 500},
  })
  assert pagination.overlap is not None


WINDOW_BASE = {
  'strategy': 'window',
  'bound': {'start': 'start', 'end': 'end'},
  'order': 'ascending',
  'step': {'unit': 'ms', 'size': 1},
  'done': {'kind': 'empty'},
}


def test_window_pagination_overlap_defaults_to_none():
  pagination = WindowPagination.model_validate(WINDOW_BASE)
  assert pagination.overlap is None


def test_window_pagination_accepts_a_bare_overlap_field():
  pagination = WindowPagination.model_validate(
    {**WINDOW_BASE, 'overlap': {'field': '[-1][0]'}}
  )
  assert pagination.overlap is not None
  assert pagination.overlap.field == '[-1][0]'
  assert pagination.overlap.cap is None
  assert pagination.overlap.chunk is None


def test_window_overlap_accepts_a_declared_cap():
  pagination = WindowPagination.model_validate(
    {**WINDOW_BASE, 'overlap': {'field': '[-1][0]', 'cap': 500}}
  )
  assert pagination.overlap.cap == 500


def test_window_overlap_rejects_a_non_positive_cap():
  with pytest.raises(ValidationError):
    WindowPagination.model_validate(
      {**WINDOW_BASE, 'overlap': {'field': '[-1][0]', 'cap': 0}}
    )


def test_window_overlap_accepts_a_declared_chunk():
  pagination = WindowPagination.model_validate({
    **WINDOW_BASE,
    'overlap': {'field': '[-1][0]', 'chunk': {'parameter': 'chunk_span', 'default': 500}},
  })
  assert pagination.overlap.chunk is not None
  assert pagination.overlap.chunk.parameter == 'chunk_span'
  assert pagination.overlap.chunk.default == 500


def test_window_overlap_chunk_rejects_a_non_positive_default():
  """`chunk.default` is never invented (S8's own discipline) and always a real, positive
  density fact once declared at all."""
  with pytest.raises(ValidationError):
    WindowPagination.model_validate({
      **WINDOW_BASE,
      'overlap': {'field': '[-1][0]', 'chunk': {'parameter': 'chunk_span', 'default': 0}},
    })


def test_window_overlap_field_admits_a_named_field_too():
  """Not every `window` row is a positional tuple -- `[-1].openTime` (a named field of the
  last row) is just as valid as `[-1][0]`."""
  pagination = WindowPagination.model_validate(
    {**WINDOW_BASE, 'overlap': {'field': '[-1].openTime'}}
  )
  assert pagination.overlap.field == '[-1].openTime'


def test_window_overlap_field_requires_the_last_row_index():
  """`WindowOverlap.field` is a `LastRowPath`, the same `[-1]<...>` requirement
  `SeekCursor.from_` carries -- `Generator.last_row_field` strips that fixed prefix
  unconditionally, so a field with no `[-1]` at all would be silently mis-processed
  rather than caught, if the type didn't refuse it here."""
  with pytest.raises(ValidationError, match='does not index the last row'):
    WindowPagination.model_validate(
      {**WINDOW_BASE, 'overlap': {'field': 'openTime'}}
    )
