"""
Exercise rule 7's `check_pagination` against synthetic fixtures, never `clients/` — per the
isolation contract `common/lib/test/test_mock.py`'s module docstring records.

T1 (`docs/TODO.md`): a `window` bound typed `string` with `format: 'date-time'` renders to
a stdlib `datetime` (`truewire.generation.python.types.parser.Parser.string`), and `datetime`
supports the same `-`/`+` arithmetic a number does, through `timedelta`. Before this,
`is_arithmetic` (née `is_numeric`) refused any `string`-typed parameter outright, so a
`window` block over a `date-time` bound — dYdX's `indexer/date_time`, used by
`get_candles`'s `fromISO`/`toISO`, is the real shape this generalises from — read as a
violation despite codegen handling it correctly once `DATETIME_TYPE` is recognised
(`common/lib/test/test_codegen.py`'s `TestPagedMethod` covers that half).

The carve-out is `window`-only: a `page` index or an `offset` is always a fresh loop
counter the walk seeds and increments itself, never a value read back out of the caller's
own parameter, so it must stay genuinely numeric regardless of what the venue calls it.

T2 (`docs/TODO.md`): `seek`'s cursor is read off the *last row* of the previous page's own
row collection, not a plain top-level field -- mexc's `historical_trades`, whose `fromId`
is the `id` of the last row. Checking `cursor.from`'s `last:<field>` needs the *item*
schema of the row collection (`SeekPagination.done`'s `rows`, or the whole payload), one
level past what `resolves` checks for every other path here — `row_item_schema` is that
one extra step, exercised below against both shapes.
"""

import json
from pathlib import Path

import pytest
import typer

from truewire.cli.check import check as spec_test


def write_endpoint(
  root: Path, *, function: str, parameters: list, pagination: dict
) -> None:
  """Write one minimal HTTP `endpoint.json` carrying a `pagination` declaration.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'pagination': pagination,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': f'/{function}',
      'openapi': {
        'description': f'List {function}.',
        'parameters': parameters,
        'responses': {
          '200': {
            'description': 'A page of results.',
            'content': {
              'application/json': {
                'schema': {
                  'title': 'Widget',
                  'type': 'object',
                  'description': 'A widget.',
                  'properties': {
                    'name': {'type': 'string', 'description': 'Widget name.'}
                  },
                }
              }
            },
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def write_seek_endpoint(
  root: Path, *,
  function: str, parameters: list, pagination: dict, row_properties: dict,
  wrap: str | None = None,
) -> None:
  """Write one minimal HTTP `endpoint.json` whose response is a page of rows, for a `seek`
  cursor's `last:<field>` to resolve against.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
    row_properties: `properties` of one row's schema.
    wrap: Property name the row array sits under, or `None` when the payload is itself
      the array -- mexc's `historical_trades` shape.
  """
  rows_schema = {
    'type': 'array',
    'description': 'A page of rows.',
    'items': {
      'title': 'Row', 'type': 'object', 'description': 'One row.', 'properties': row_properties,
    },
  }
  response_schema = (
    rows_schema if wrap is None
    else {
      'title': 'Page', 'type': 'object', 'description': 'A page.',
      'properties': {wrap: rows_schema},
    }
  )
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'pagination': pagination,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': f'/{function}',
      'openapi': {
        'description': f'List {function}.',
        'parameters': parameters,
        'responses': {
          '200': {
            'description': 'A page of results.',
            'content': {'application/json': {'schema': response_schema}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def write_window_overlap_endpoint(
  root: Path, *,
  function: str, parameters: list, pagination: dict, row_schema: dict,
  wrap: str | None = None,
) -> None:
  """Write one minimal HTTP `endpoint.json` whose response is a page of rows, for a
  `window`+`overlap`'s `overlap.field` to resolve against -- `row_schema` is usually a
  `prefixItems` tuple (a candle), the shape `docs/pagination.md` §3's bracket indices
  were added to reach.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
    row_schema: Schema of one row.
    wrap: Property name the row array sits under, or `None` when the payload is itself
      the array.
  """
  rows_schema = {'type': 'array', 'description': 'A page of rows.', 'items': row_schema}
  response_schema = (
    rows_schema if wrap is None
    else {
      'title': 'Page', 'type': 'object', 'description': 'A page.',
      'properties': {wrap: rows_schema},
    }
  )
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'pagination': pagination,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': f'/{function}',
      'openapi': {
        'description': f'List {function}.',
        'parameters': parameters,
        'responses': {
          '200': {
            'description': 'A page of results.',
            'content': {'application/json': {'schema': response_schema}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


DATE_TIME_SCHEMA = {'type': 'string', 'format': 'date-time'}
"""dYdX's `indexer/date_time` shape: a `string` the type renderer maps to `datetime`."""

WINDOW = {
  'strategy': 'window',
  'bound': {'start': 'start', 'end': 'end'},
  'order': 'descending',
  'step': {'unit': 'ms', 'size': 1},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'empty'},
}
"""dYdX's `get_candles` shape once paginated: `fromISO`/`toISO` walked backwards."""

WINDOW_PARAMETERS = [
  {
    'name': 'start',
    'in': 'query',
    'required': False,
    'description': 'Start.',
    'schema': DATE_TIME_SCHEMA,
  },
  {
    'name': 'end',
    'in': 'query',
    'required': False,
    'description': 'End.',
    'schema': DATE_TIME_SCHEMA,
  },
  {
    'name': 'limit',
    'in': 'query',
    'required': False,
    'description': 'Page size.',
    'schema': {'type': 'integer'},
  },
]


def test_a_window_bound_typed_date_time_is_clean(tmp_path, capsys):
  """The carve-out this task adds: a `date-time` `window` bound is no longer flagged."""
  write_endpoint(
    tmp_path, function='candles.list', parameters=WINDOW_PARAMETERS, pagination=WINDOW
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_window_bound_typed_plain_string_is_still_flagged(tmp_path, capsys):
  """A `string` with no `format: 'date-time'` still can't do the walk's arithmetic."""
  parameters = [
    {**p, 'schema': {'type': 'string'}} if p['name'] == 'start' else p
    for p in WINDOW_PARAMETERS
  ]
  write_endpoint(
    tmp_path, function='candles.list', parameters=parameters, pagination=WINDOW
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert 'Result: FAILED' in captured.out
  assert '`start` is typed `string`' in captured.err
  assert 'format: "date-time"' in captured.err


def test_a_page_index_typed_date_time_is_still_flagged(tmp_path, capsys):
  """The carve-out does not leak past `window`: a `page` index is a fresh loop counter,
  never a value read back out of the caller's own parameter, so it stays numeric-only."""
  write_endpoint(
    tmp_path,
    function='orders.list',
    parameters=[
      {
        'name': 'page',
        'in': 'query',
        'required': False,
        'description': 'Page.',
        'schema': DATE_TIME_SCHEMA,
      },
    ],
    pagination={
      'strategy': 'page',
      'index': {'parameter': 'page', 'start': 1},
      'done': {'kind': 'empty'},
    },
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`page` is typed `string`' in captured.err
  assert 'format: "date-time"' not in captured.err


def test_an_offset_typed_date_time_is_still_flagged(tmp_path, capsys):
  """Same reasoning as `page`: an `offset` is the walk's own running total, never the
  caller's `datetime`."""
  write_endpoint(
    tmp_path,
    function='trades.list',
    parameters=[
      {
        'name': 'offset',
        'in': 'query',
        'required': False,
        'description': 'Offset.',
        'schema': DATE_TIME_SCHEMA,
      },
    ],
    pagination={
      'strategy': 'offset',
      'offset': {'parameter': 'offset'},
      'done': {'kind': 'empty'},
    },
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`offset` is typed `string`' in captured.err


PAGE_TOTAL_PARAMETERS = [
  {
    'name': 'page',
    'in': 'query',
    'required': False,
    'description': 'Page.',
    'schema': {'type': 'integer'},
  },
  {
    'name': 'pageSize',
    'in': 'query',
    'required': False,
    'description': 'Page size.',
    'schema': {'type': 'integer'},
  },
]

PAGE_TOTAL_ROWS = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'rows'},
}
"""binance's `simple_earn/locked/list` shape: a page walk terminated by a total, whose rows
sit under a declared `rows` field alongside it."""


def write_page_total_endpoint(
  root: Path, *, function: str, parameters: list, pagination: dict, row_properties: dict,
) -> None:
  """One minimal HTTP `endpoint.json` whose response carries both a total count and a named
  row collection -- for `TotalDone.rows` to resolve against.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
    row_properties: `properties` of one row's schema.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'pagination': pagination,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': f'/{function}',
      'openapi': {
        'description': f'List {function}.',
        'parameters': parameters,
        'responses': {
          '200': {
            'description': 'A page of results.',
            'content': {
              'application/json': {
                'schema': {
                  'title': 'Page',
                  'type': 'object',
                  'description': 'A page of results.',
                  'properties': {
                    'total': {'type': 'integer', 'description': 'Total matching records.'},
                    'rows': {
                      'type': 'array',
                      'description': 'Matching records on this page.',
                      'items': {
                        'title': 'Row',
                        'type': 'object',
                        'description': 'One row.',
                        'properties': row_properties,
                      },
                    },
                  },
                },
              },
            },
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_a_page_total_rows_field_naming_a_real_collection_is_clean(tmp_path, capsys):
  write_page_total_endpoint(
    tmp_path, function='products.list', parameters=PAGE_TOTAL_PARAMETERS,
    pagination=PAGE_TOTAL_ROWS,
    row_properties={'asset': {'type': 'string', 'description': 'Asset.'}},
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Result: OK' in out


def test_a_page_total_rows_field_naming_a_field_absent_from_the_response_is_flagged(
  tmp_path, capsys,
):
  write_page_total_endpoint(
    tmp_path, function='products.list', parameters=PAGE_TOTAL_PARAMETERS,
    pagination={**PAGE_TOTAL_ROWS, 'done': {**PAGE_TOTAL_ROWS['done'], 'rows': 'items'}},
    row_properties={'asset': {'type': 'string', 'description': 'Asset.'}},
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`items` names no property of the response payload' in captured.err


SEEK_PARAMETERS = [
  {
    'name': 'from_id',
    'in': 'query',
    'required': False,
    'description': 'Cursor.',
    'schema': {'type': 'string'},
  },
  {
    'name': 'limit',
    'in': 'query',
    'required': False,
    'description': 'Page size.',
    'schema': {'type': 'integer'},
  },
]

SEEK = {
  'strategy': 'seek',
  'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'short_page'},
}
"""mexc's shape: `fromId` read off the `id` field of the previous page's last row."""


def test_a_seek_cursor_naming_a_real_row_field_is_clean(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS, pagination=SEEK,
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_seek_cursor_terminated_by_unchanged_naming_a_real_row_field_is_clean(tmp_path, capsys):
  """`unchanged`'s own `rows` -- the collection whose last row's cursor field is compared
  -- is checked the same way `short_page`/`empty`'s already are."""
  write_seek_endpoint(
    tmp_path, function='data.historical_funding', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'done': {'kind': 'unchanged'}},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_seek_cursor_naming_a_field_absent_from_the_row_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'cursor': {'parameter': 'from_id', 'from': '[-1].tradeId'}},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`tradeId` names no property of a row' in captured.err


def test_a_seek_cursor_resolves_against_a_declared_row_collections_items(tmp_path, capsys):
  """The field is checked against the *item* schema of `done.rows`, not the payload's own
  top-level properties -- the one thing `row_item_schema` adds past `resolves`."""
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'done': {'kind': 'short_page', 'rows': 'data'}},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
    wrap='data',
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Result: OK' in out


def test_a_seek_cursor_parameter_that_is_not_declared_is_flagged(tmp_path, capsys):
  """The generic `pagination_parameters` check applies to `seek`'s cursor too."""
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'cursor': {'parameter': 'cursor', 'from': '[-1].id'}},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert '`cursor` is not a parameter of this operation' in captured.err


OVERLAP_PARAMETERS = [
  {
    'name': 'startTime',
    'in': 'query',
    'required': True,
    'description': 'Start time.',
    'schema': {'type': 'integer', 'format': 'epoch-millis'},
  },
]

OVERLAP_SEEK = {
  'strategy': 'seek',
  'cursor': {'parameter': 'startTime', 'from': '[-1].time'},
  'done': {'kind': 'empty'},
  'overlap': {'cap': 500},
}
"""hyperliquid's shape: `startTime` re-sent as the largest `time` collected so far -- a
cursor field that is not unique per row, unlike mexc's `fromId` above."""


def test_an_overlap_seek_cursor_naming_a_numeric_row_field_is_clean(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=OVERLAP_PARAMETERS,
    pagination=OVERLAP_SEEK,
    row_properties={
      'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Event time.'},
    },
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_an_overlap_seek_cursor_naming_a_non_numeric_row_field_is_flagged(tmp_path, capsys):
  """An `overlap` walk compares successive field values with `>` and takes their `max` --
  the same arithmetic requirement rule 7 already puts on a `window` bound."""
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=OVERLAP_PARAMETERS,
    pagination=OVERLAP_SEEK,
    row_properties={
      'time': {'type': 'string', 'format': 'date-time', 'description': 'Event time.'},
    },
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`time` is not typed as a number' in captured.err


def test_an_overlap_seek_declared_against_a_wrapped_row_collection_is_accepted(tmp_path, capsys):
  """`Generator.paged_overlap_seek` unwraps `pagination.done.rows` via `paged_rows`
  (`docs/pagination.md`) -- the generated walk genuinely does reconstruct the row
  collection out of an enclosing envelope now, so this spec-level refusal is stale and
  was removed. bybit's `market/funding_history` (`{category, symbol, list}`) is the real,
  otherwise-qualifying endpoint this unblocks."""
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=OVERLAP_PARAMETERS,
    pagination={**OVERLAP_SEEK, 'done': {'kind': 'empty', 'rows': 'data'}},
    row_properties={
      'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Event time.'},
    },
    wrap='data',
  )

  spec_test(path=str(tmp_path), verbose=True)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


WINDOW_OVERLAP_PARAMETERS = [
  {
    'name': 'startTime', 'in': 'query', 'required': True, 'description': 'Start time.',
    'schema': {'type': 'integer', 'format': 'epoch-millis'},
  },
  {
    'name': 'endTime', 'in': 'query', 'required': True, 'description': 'End time.',
    'schema': {'type': 'integer', 'format': 'epoch-millis'},
  },
  {
    'name': 'limit', 'in': 'query', 'required': False, 'description': 'Page size.',
    'schema': {'type': 'integer', 'default': 500},
  },
]

WINDOW_OVERLAP = {
  'strategy': 'window',
  'bound': {'start': 'startTime', 'end': 'endTime'},
  'order': 'ascending',
  'step': {'unit': 'ms', 'size': 1},
  'size': {'parameter': 'limit'},
  'overlap': {'field': '[-1][0]'},
  'done': {'kind': 'empty'},
}
"""binance-style candle shape: the row's own open time sits at tuple position 0."""

CANDLE_ROW = {
  'title': 'Candle', 'type': 'array', 'minItems': 2, 'maxItems': 2,
  'prefixItems': [
    {
      'title': 'openTime', 'type': 'integer', 'format': 'epoch-millis',
      'description': 'Open time.',
    },
    {
      'title': 'open', 'type': 'string', 'format': 'decimal-string',
      'description': 'Open price.',
    },
  ],
}


def test_a_window_overlap_field_naming_a_real_tuple_position_is_clean(tmp_path, capsys):
  write_window_overlap_endpoint(
    tmp_path, function='market.candles', parameters=WINDOW_OVERLAP_PARAMETERS,
    pagination=WINDOW_OVERLAP, row_schema=CANDLE_ROW,
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_window_overlap_field_naming_an_out_of_range_position_is_flagged(tmp_path, capsys):
  """`[-1][9]` resolves against the row collection's own item schema -- a 2-element
  `prefixItems` tuple -- the same one extra step `row_item_schema`/`row_field_schema`
  already take for `seek`'s `last:<field>`."""
  write_window_overlap_endpoint(
    tmp_path, function='market.candles', parameters=WINDOW_OVERLAP_PARAMETERS,
    pagination={**WINDOW_OVERLAP, 'overlap': {'field': '[-1][9]'}},
    row_schema=CANDLE_ROW,
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`[9]` names no property of a row' in captured.err


def test_a_window_overlap_field_naming_a_non_numeric_tuple_position_is_flagged(tmp_path, capsys):
  """An `overlap` walk compares successive field values with `>` and takes their `max` --
  the same arithmetic requirement `seek`'s own `overlap` already carries."""
  row_schema = {
    'title': 'Candle', 'type': 'array', 'minItems': 1, 'maxItems': 1,
    'prefixItems': [
      {'title': 'openTime', 'type': 'string', 'format': 'date-time', 'description': 'Open time.'},
    ],
  }
  write_window_overlap_endpoint(
    tmp_path, function='market.candles', parameters=WINDOW_OVERLAP_PARAMETERS,
    pagination=WINDOW_OVERLAP, row_schema=row_schema,
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`[0]` is not typed as a number' in captured.err


def test_a_window_overlap_declared_against_a_wrapped_row_collection_is_accepted(tmp_path, capsys):
  """Same reasoning as `seek`'s own `overlap` above: `Generator.paged_window_overlap` now
  genuinely unwraps `pagination.done.rows`, so this refusal is stale and was removed.
  bybit's/kucoin's/coinbase's candle endpoints (`{category, symbol, list}`, `{dataList,
  hasMore}`, `{candles: [...]}`) are the real, otherwise-qualifying endpoints this
  unblocks."""
  write_window_overlap_endpoint(
    tmp_path, function='market.candles', parameters=WINDOW_OVERLAP_PARAMETERS,
    pagination={**WINDOW_OVERLAP, 'done': {'kind': 'empty', 'rows': 'data'}},
    row_schema=CANDLE_ROW, wrap='data',
  )

  spec_test(path=str(tmp_path), verbose=True)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_window_overlap_cap_redundant_with_a_resolvable_size_default_is_flagged(
  tmp_path, capsys,
):
  """`limit`'s own declared `default` already resolves a row cap -- declaring
  `overlap.cap` too leaves two sources of truth for the same fact."""
  write_window_overlap_endpoint(
    tmp_path, function='market.candles', parameters=WINDOW_OVERLAP_PARAMETERS,
    pagination={**WINDOW_OVERLAP, 'overlap': {'field': '[-1][0]', 'cap': 500}},
    row_schema=CANDLE_ROW,
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert '`overlap.cap` is redundant' in captured.err


def test_a_window_overlap_with_no_resolvable_cap_at_all_is_flagged(tmp_path, capsys):
  """Neither `limit` (no declared default here) nor `overlap.cap` settles the row count a
  full chunk is measured against."""
  parameters = [
    {**p, 'schema': {'type': 'integer'}} if p['name'] == 'limit' else p
    for p in WINDOW_OVERLAP_PARAMETERS
  ]
  write_window_overlap_endpoint(
    tmp_path, function='market.candles', parameters=parameters,
    pagination=WINDOW_OVERLAP, row_schema=CANDLE_ROW,
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert 'resolves a row cap' in captured.err


def test_a_seek_cursor_source_missing_the_last_prefix_is_rejected_at_load_time():
  """`LastRowPath` refuses a plain dotted key -- `seek`'s one narrow indexing exception has
  to index the last row, spelled `[-1]<...>`, never bare."""
  from pydantic import ValidationError

  from truewire.spec import Endpoint

  with pytest.raises(ValidationError, match='does not index the last row'):
    Endpoint.model_validate({
      'function': 'trades.historical',
      'pagination': {**SEEK, 'cursor': {'parameter': 'from_id', 'from': 'id'}},
      'spec': {
        'kind': 'rpc', 'transports': ['http'], 'method': 'GET', 'path': '/trades',
        'openapi': {
          'description': 'x', 'parameters': SEEK_PARAMETERS,
          'responses': {'200': {'description': 'x'}},
        },
      },
    })


def test_parameter_schema_reads_new_shape():
  """`parameter_schema` reads the new-shape `request` schema's `properties` -- design §7 --
  feeding `check_pagination`'s arithmetic/type checks the same way it already does for the
  legacy `parameters`/`requestBody` shape."""
  from truewire.spec.authoring import parameter_schema

  operation = {'request': {'type': 'object', 'properties': {
    'startTime': {'type': 'integer', 'format': 'epoch-millis'},
  }}}
  assert parameter_schema(operation, 'startTime') == {'type': 'integer', 'format': 'epoch-millis'}
