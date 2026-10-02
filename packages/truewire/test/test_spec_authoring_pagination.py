"""
Exercise rule 7's `check_pagination` against synthetic fixtures, never `clients/` — per the
isolation contract `common/lib/test/test_mock.py`'s module docstring records.

A `seek` bound typed `string` with `format: 'date-time'` renders to a stdlib `datetime`
(`truewire.generation.python.types.parser.Parser.string`), and `datetime` supports the same
`-`/`+` arithmetic a number does, through `timedelta` -- so a `span`-bearing `seek` bound
may be one (dYdX's `indexer/date_time`, used by `get_candles`'s `fromISO`/`toISO`). The
carve-out is `seek`-only: a `page` index or an `offset` is always a fresh loop counter the
walk seeds and increments itself, never a value read back out of the caller's own
parameter, so it must stay genuinely numeric regardless of what the venue calls it.

`seek`'s cursor is read off a row of the walk's own row collection (`SeekPagination.rows`,
or the whole payload). Checking `cursor.field` needs the *item* schema of that collection,
one level past what `resolves` checks for every other path here — `row_item_schema` is that
one extra step, exercised below against both shapes. ADR 0013's `seek`-specific rules
(`unique: false` needs a resolvable cap and an orderable field, `cap` isn't declared where
`size` already resolves one, `span` never shadows a wire parameter, a paginated request is a
read) live in `check_seek` and are exercised here too.
"""

import json
from pathlib import Path

import pytest
import typer

from truewire.cli.check import check as spec_test


def write_endpoint(
  root: Path, *, function: str, parameters: list, pagination: dict,
  response_schema: dict | None = None, method: str = 'GET',
) -> None:
  """Write one minimal HTTP `endpoint.json` carrying a `pagination` declaration.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
    response_schema: The 2xx payload schema; a one-property `Widget` record by default.
    method: HTTP method the operation declares.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'pagination': pagination,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': method,
      'path': f'/{function}',
      'openapi': {
        'description': f'List {function}.',
        'parameters': parameters,
        'responses': {
          '200': {
            'description': 'A page of results.',
            'content': {
              'application/json': {
                'schema': response_schema or {
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


def rows_response(row_schema: dict, *, wrap: str | None = None) -> dict:
  """A page-of-rows payload schema: a bare array of `row_schema`, or one wrapped under
  `wrap` (bybit's `{list: [...]}`).

  Args:
    row_schema: Schema of one row.
    wrap: Property name the row array sits under, or `None` for the bare-array shape.
  """
  rows_schema = {'type': 'array', 'description': 'A page of rows.', 'items': row_schema}
  if wrap is None:
    return rows_schema
  return {
    'title': 'Page', 'type': 'object', 'description': 'A page.',
    'properties': {wrap: rows_schema},
  }


def record_row(properties: dict) -> dict:
  """One named-field row schema (a fill, a ledger entry)."""
  return {'title': 'Row', 'type': 'object', 'description': 'One row.', 'properties': properties}


def write_seek_endpoint(
  root: Path, *,
  function: str, parameters: list, pagination: dict, row_properties: dict,
  wrap: str | None = None, method: str = 'GET',
) -> None:
  """Write one minimal HTTP `endpoint.json` whose response is a page of named-field rows,
  for a `seek` cursor's `[-1].<field>` to resolve against.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
    row_properties: `properties` of one row's schema.
    wrap: Property name the row array sits under, or `None` when the payload is itself
      the array -- mexc's `historical_trades` shape.
    method: HTTP method the operation declares.
  """
  write_endpoint(
    root, function=function, parameters=parameters, pagination=pagination,
    response_schema=rows_response(record_row(row_properties), wrap=wrap), method=method,
  )


def write_candle_endpoint(
  root: Path, *,
  function: str, parameters: list, pagination: dict, row_schema: dict,
  wrap: str | None = None,
) -> None:
  """Write one minimal HTTP `endpoint.json` whose response is a page of positional-tuple
  rows (a candle), the shape `docs/pagination.md` §3's bracket indices were added to reach.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: The operation's `parameters` list.
    pagination: The `pagination` declaration to attach beside `spec`.
    row_schema: Schema of one row.
    wrap: Property name the row array sits under, or `None` when the payload is itself
      the array.
  """
  write_endpoint(
    root, function=function, parameters=parameters, pagination=pagination,
    response_schema=rows_response(row_schema, wrap=wrap),
  )


def assert_clean(capsys) -> None:
  """The one-declaration, zero-violation, zero-warning summary."""
  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  1\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def run_failing(tmp_path, capsys) -> tuple[str, str]:
  """Run `spec test` expecting exit 1; return `(out, err)`."""
  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)
  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  return captured.out, captured.err


DATE_TIME_SCHEMA = {'type': 'string', 'format': 'date-time'}
"""dYdX's `indexer/date_time` shape: a `string` the type renderer maps to `datetime`."""

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

CANDLE_PARAMETERS = [
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

CANDLES = {
  'strategy': 'seek',
  'cursor': {'field': '[-1][0]', 'unique': True},
  'bound': {'start': 'startTime', 'end': 'endTime'},
  'anchor': 'start',
  'size': {'parameter': 'limit'},
}
"""binance's kline shape: the row's own open time at tuple position 0, oldest rows kept."""

ISO_CANDLE_PARAMETERS = [
  {'name': 'start', 'in': 'query', 'required': False, 'description': 'Start.', 'schema': DATE_TIME_SCHEMA},
  {'name': 'end', 'in': 'query', 'required': False, 'description': 'End.', 'schema': DATE_TIME_SCHEMA},
  {'name': 'limit', 'in': 'query', 'required': False, 'description': 'Page size.', 'schema': {'type': 'integer'}},
]

ISO_CANDLES_SPAN = {
  'strategy': 'seek',
  'cursor': {'field': '[-1][0]', 'unique': True},
  'bound': {'start': 'start', 'end': 'end'},
  'anchor': 'end',
  'size': {'parameter': 'limit'},
  'span': {'parameter': 'span', 'default': 21000, 'unit': 's'},
}
"""dYdX's `get_candles` shape with a span: `fromISO`/`toISO` walked backwards in chunks."""


def test_a_span_bound_typed_date_time_is_clean(tmp_path, capsys):
  """A `date-time` bound supports the `pos + span` arithmetic a span needs."""
  write_candle_endpoint(
    tmp_path, function='candles.list', parameters=ISO_CANDLE_PARAMETERS,
    pagination=ISO_CANDLES_SPAN, row_schema=CANDLE_ROW,
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_span_bound_typed_plain_string_is_flagged(tmp_path, capsys):
  """A `string` with no `format: 'date-time'` can't have a span added to it."""
  parameters = [
    {**p, 'schema': {'type': 'string'}} if p['name'] == 'end' else p
    for p in ISO_CANDLE_PARAMETERS
  ]
  write_candle_endpoint(
    tmp_path, function='candles.list', parameters=parameters,
    pagination=ISO_CANDLES_SPAN, row_schema=CANDLE_ROW,
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`end` is typed `string`' in err
  assert 'format: "date-time"' in err


def test_a_plain_string_bound_without_a_span_is_not_flagged(tmp_path, capsys):
  """Without a `span` nothing is added to the bound -- it is re-sent as read off a row --
  so bitget's/mexc's string ids are fine."""
  write_seek_endpoint(
    tmp_path, function='trades.historical',
    parameters=[
      {'name': 'fromId', 'in': 'query', 'required': False, 'description': 'Cursor.', 'schema': {'type': 'string'}},
      {'name': 'limit', 'in': 'query', 'required': False, 'description': 'Page size.', 'schema': {'type': 'integer'}},
    ],
    pagination={
      'strategy': 'seek', 'cursor': {'field': '[-1].id', 'unique': True},
      'bound': {'start': 'fromId'}, 'anchor': 'start', 'size': {'parameter': 'limit'},
    },
    row_properties={'id': {'type': 'string', 'description': 'Trade id.'}},
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_page_index_typed_date_time_is_still_flagged(tmp_path, capsys):
  """The carve-out does not leak past `seek`: a `page` index is a fresh loop counter,
  never a value read back out of the caller's own parameter, so it stays numeric-only."""
  write_endpoint(
    tmp_path,
    function='orders.list',
    parameters=[
      {'name': 'page', 'in': 'query', 'required': False, 'description': 'Page.', 'schema': DATE_TIME_SCHEMA},
    ],
    pagination={
      'strategy': 'page',
      'index': {'parameter': 'page', 'start': 1},
      'done': {'kind': 'empty'},
    },
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`page` is typed `string`' in err
  assert 'format: "date-time"' not in err


def test_an_offset_typed_date_time_is_still_flagged(tmp_path, capsys):
  """Same reasoning as `page`: an `offset` is the walk's own running total, never the
  caller's `datetime`."""
  write_endpoint(
    tmp_path,
    function='trades.list',
    parameters=[
      {'name': 'offset', 'in': 'query', 'required': False, 'description': 'Offset.', 'schema': DATE_TIME_SCHEMA},
    ],
    pagination={
      'strategy': 'offset',
      'offset': {'parameter': 'offset'},
      'done': {'kind': 'empty'},
    },
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`offset` is typed `string`' in err


PAGE_TOTAL_PARAMETERS = [
  {'name': 'page', 'in': 'query', 'required': False, 'description': 'Page.', 'schema': {'type': 'integer'}},
  {'name': 'pageSize', 'in': 'query', 'required': False, 'description': 'Page size.', 'schema': {'type': 'integer'}},
]

PAGE_TOTAL_ROWS = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'rows'},
}
"""binance's `simple_earn/locked/list` shape: a page walk terminated by a total, whose rows
sit under a declared `rows` field alongside it."""

PAGE_TOTAL_RESPONSE = {
  'title': 'Page',
  'type': 'object',
  'description': 'A page of results.',
  'properties': {
    'total': {'type': 'integer', 'description': 'Total matching records.'},
    'rows': {
      'type': 'array',
      'description': 'Matching records on this page.',
      'items': record_row({'asset': {'type': 'string', 'description': 'Asset.'}}),
    },
  },
}


def test_a_page_total_rows_field_naming_a_real_collection_is_clean(tmp_path, capsys):
  write_endpoint(
    tmp_path, function='products.list', parameters=PAGE_TOTAL_PARAMETERS,
    pagination=PAGE_TOTAL_ROWS, response_schema=PAGE_TOTAL_RESPONSE,
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert 'Result: OK' in capsys.readouterr().out


def test_a_page_total_rows_field_naming_a_field_absent_from_the_response_is_flagged(
  tmp_path, capsys,
):
  write_endpoint(
    tmp_path, function='products.list', parameters=PAGE_TOTAL_PARAMETERS,
    pagination={**PAGE_TOTAL_ROWS, 'done': {**PAGE_TOTAL_ROWS['done'], 'rows': 'items'}},
    response_schema=PAGE_TOTAL_RESPONSE,
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`items` names no property of the response payload' in err


SEEK_PARAMETERS = [
  {'name': 'from_id', 'in': 'query', 'required': False, 'description': 'Cursor.', 'schema': {'type': 'integer'}},
  {'name': 'limit', 'in': 'query', 'required': False, 'description': 'Page size.', 'schema': {'type': 'integer'}},
]

SEEK = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].id', 'unique': True},
  'bound': {'start': 'from_id'},
  'anchor': 'start',
  'size': {'parameter': 'limit'},
}
"""An id cursor read off the `id` of a row and re-sent as the lower bound, walked upwards."""


def test_a_seek_cursor_naming_a_real_row_field_is_clean(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS, pagination=SEEK,
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_seek_cursor_naming_a_field_absent_from_the_row_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'cursor': {'field': '[-1].tradeId', 'unique': True}},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`tradeId` names no property of a row' in err


def test_a_seek_cursor_resolves_against_a_declared_row_collections_items(tmp_path, capsys):
  """The field is checked against the *item* schema of `rows`, not the payload's own
  top-level properties -- the one thing `row_item_schema` adds past `resolves`."""
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'rows': 'data'},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
    wrap='data',
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert 'Result: OK' in capsys.readouterr().out


def test_a_seek_rows_path_absent_from_the_response_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'rows': 'list'},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
    wrap='data',
  )
  _, err = run_failing(tmp_path, capsys)
  assert '`list` names no property of the response payload' in err


def test_a_seek_bound_that_is_not_declared_is_flagged(tmp_path, capsys):
  """The generic `pagination_parameters` check applies to `seek`'s bounds too."""
  write_seek_endpoint(
    tmp_path, function='trades.historical', parameters=SEEK_PARAMETERS,
    pagination={**SEEK, 'bound': {'start': 'cursor'}},
    row_properties={'id': {'type': 'integer', 'description': 'Trade id.'}},
  )
  _, err = run_failing(tmp_path, capsys)
  assert '`cursor` is not a parameter of this operation' in err


NON_UNIQUE_PARAMETERS = [
  {
    'name': 'startTime', 'in': 'query', 'required': True, 'description': 'Start time.',
    'schema': {'type': 'integer', 'format': 'epoch-millis'},
  },
]

NON_UNIQUE = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].time', 'unique': False},
  'bound': {'start': 'startTime'},
  'anchor': 'start',
  'cap': 500,
}
"""hyperliquid's shape: `startTime` re-sent as the largest `time` seen so far -- a cursor
field that is not unique per row, unlike an id."""


def test_a_non_unique_cursor_with_a_numeric_field_and_a_cap_is_clean(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=NON_UNIQUE_PARAMETERS,
    pagination=NON_UNIQUE,
    row_properties={'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Event time.'}},
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_non_unique_cursor_with_a_non_orderable_field_is_flagged(tmp_path, capsys):
  """A non-unique walk takes the extreme value off a page (`max`/`min`) to know which rows
  to carry over; a plain string can't be ordered, and under an epoch-millis bound it has no
  unit to convert from either."""
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=NON_UNIQUE_PARAMETERS,
    pagination=NON_UNIQUE,
    row_properties={'time': {'type': 'string', 'description': 'Event time.'}},
  )
  out, err = run_failing(tmp_path, capsys)
  # Plus rule 3's warning: a field named `time` states no timestamp format.
  assert 'violations 2\n  warnings   1' in out
  assert '`time` is declared `unique: false` but is not orderable' in err
  assert '`time` declares no timestamp `format` the walk can convert' in ' '.join(err.split())


@pytest.mark.parametrize('row', [
  {'type': 'string', 'format': 'epoch-millis'},
  {'type': 'string', 'format': 'date-time'},
  {'type': 'integer', 'format': 'epoch-seconds'},
  {'type': 'number', 'format': 'epoch-seconds'},
])
def test_a_non_unique_cursor_with_a_timestamp_field_is_clean(tmp_path, capsys, row):
  """A row timestamp in any instant format converts to the epoch-millis bound before
  comparing (TRU-197): the same format (bitget's/bybit's candle rows), an ISO string, or
  lighter's epoch seconds."""
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=NON_UNIQUE_PARAMETERS,
    pagination=NON_UNIQUE,
    row_properties={'time': {**row, 'description': 'Event time.'}},
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


FUNDINGS = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].timestamp', 'unique': True},
  'bound': {'start': 'start_timestamp', 'end': 'end_timestamp'},
  'anchor': 'end',
  'cap': 749,
}
"""lighter's `fundings` shape: an epoch-millis range walked backwards by its row `timestamp`."""

FUNDINGS_PARAMETERS = [
  {
    'name': name, 'in': 'query', 'required': True, 'description': 'Range bound.',
    'schema': {'type': 'integer', 'format': 'epoch-millis'},
  }
  for name in ('start_timestamp', 'end_timestamp')
]


def test_an_epoch_seconds_cursor_under_an_epoch_millis_bound_is_clean(tmp_path, capsys):
  """Every backend reads the row as seconds and sends the bound as milliseconds."""
  write_seek_endpoint(
    tmp_path, function='markets.fundings', parameters=FUNDINGS_PARAMETERS, pagination=FUNDINGS,
    row_properties={'timestamp': {'type': 'integer', 'format': 'epoch-seconds', 'description': 'Funding time.'}},
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


@pytest.mark.parametrize(('row', 'stated'), [
  ({'type': 'integer'}, ''),
  ({'type': 'string', 'format': 'date'}, ' (it is `date`)'),
])
def test_a_cursor_with_no_convertible_timestamp_format_under_a_timestamp_bound_is_flagged(
  tmp_path, capsys, row, stated,
):
  """A bare integer under an epoch-millis bound leaves the walk guessing its unit, and a
  day converts to no instant: an error naming both fields, unique cursor or not."""
  write_seek_endpoint(
    tmp_path, function='markets.fundings', parameters=FUNDINGS_PARAMETERS, pagination=FUNDINGS,
    row_properties={'timestamp': {**row, 'description': 'Funding time.'}},
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n' in out
  assert (
    f'`timestamp` declares no timestamp `format` the walk can convert to the moving bound '
    f'`end_timestamp`\'s `epoch-millis`{stated}: the walk reads a raw row value'
  ) in ' '.join(err.split())


def test_a_non_unique_cursor_with_no_resolvable_cap_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=NON_UNIQUE_PARAMETERS,
    pagination={key: value for key, value in NON_UNIQUE.items() if key != 'cap'},
    row_properties={'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Event time.'}},
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert 'neither `pagination.size`\'s own declared default nor `cap`' in err


def test_a_unique_cursor_with_no_resolvable_cap_is_not_flagged(tmp_path, capsys):
  """A unique cursor confirms exhaustion with one extra request instead; requiring a cap
  would push an author to invent one."""
  parameters = [
    {**p, 'schema': {'type': 'integer'}} if p['name'] == 'limit' else p for p in CANDLE_PARAMETERS
  ]
  write_candle_endpoint(
    tmp_path, function='market.candles', parameters=parameters,
    pagination=CANDLES, row_schema=CANDLE_ROW,
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_non_unique_cursor_against_a_wrapped_row_collection_is_accepted(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=NON_UNIQUE_PARAMETERS,
    pagination={**NON_UNIQUE, 'rows': 'data'},
    row_properties={'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Event time.'}},
    wrap='data',
  )
  spec_test(path=str(tmp_path), verbose=True)
  assert_clean(capsys)


def test_a_cursor_field_naming_a_real_tuple_position_is_clean(tmp_path, capsys):
  write_candle_endpoint(
    tmp_path, function='market.candles', parameters=CANDLE_PARAMETERS,
    pagination=CANDLES, row_schema=CANDLE_ROW,
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_cursor_field_naming_an_out_of_range_position_is_flagged(tmp_path, capsys):
  """`[-1][9]` resolves against the row collection's own item schema -- a 2-element
  `prefixItems` tuple -- the same one extra step `row_item_schema`/`row_field_schema`
  take for a named field."""
  write_candle_endpoint(
    tmp_path, function='market.candles', parameters=CANDLE_PARAMETERS,
    pagination={**CANDLES, 'cursor': {'field': '[-1][9]', 'unique': True}},
    row_schema=CANDLE_ROW,
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`[9]` names no property of a row' in err


def test_a_cursor_field_against_a_wrapped_row_collection_is_accepted(tmp_path, capsys):
  """bybit's/kucoin's/coinbase's candle endpoints (`{category, symbol, list}`, `{dataList,
  hasMore}`, `{candles: [...]}`)."""
  write_candle_endpoint(
    tmp_path, function='market.candles', parameters=CANDLE_PARAMETERS,
    pagination={**CANDLES, 'rows': 'data'}, row_schema=CANDLE_ROW, wrap='data',
  )
  spec_test(path=str(tmp_path), verbose=True)
  assert_clean(capsys)


def test_a_cap_redundant_with_a_resolvable_size_default_is_flagged(tmp_path, capsys):
  """`limit`'s own declared `default` already resolves a row cap -- declaring `cap` too
  leaves two sources of truth for the same fact."""
  write_candle_endpoint(
    tmp_path, function='market.candles', parameters=CANDLE_PARAMETERS,
    pagination={**CANDLES, 'cap': 500}, row_schema=CANDLE_ROW,
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`cap` is redundant' in err


def test_a_span_parameter_shadowing_a_wire_parameter_is_flagged(tmp_path, capsys):
  write_candle_endpoint(
    tmp_path, function='market.candles', parameters=CANDLE_PARAMETERS,
    pagination={**CANDLES, 'span': {'parameter': 'limit', 'default': 1000, 'unit': 'ms'}},
    row_schema=CANDLE_ROW,
  )
  out, err = run_failing(tmp_path, capsys)
  assert 'violations 1\n  warnings   0' in out
  assert '`limit` is a real parameter of this operation' in err


def test_a_paginated_post_is_warned_about(tmp_path, capsys):
  """A walk repeats its request on retry; a `POST` read (hyperliquid's info endpoints) is
  legitimate but has to be confirmed by a human, so this warns rather than fails."""
  write_seek_endpoint(
    tmp_path, function='funding.history', parameters=NON_UNIQUE_PARAMETERS,
    pagination=NON_UNIQUE, method='POST',
    row_properties={'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Event time.'}},
  )
  spec_test(path=str(tmp_path), verbose=True)
  captured = capsys.readouterr()
  assert 'violations 0\n  warnings   1' in captured.out
  assert 'Result: OK' in captured.out
  assert 'this operation is `POST`' in captured.err


def test_a_seek_cursor_field_missing_the_last_prefix_is_rejected_at_load_time():
  """`LastRowPath` refuses a plain dotted key -- `seek`'s one narrow indexing exception has
  to index the last row, spelled `[-1]<...>`, never bare."""
  from pydantic import ValidationError

  from truewire.spec import Endpoint

  with pytest.raises(ValidationError, match='does not index the last row'):
    Endpoint.model_validate({
      'function': 'trades.historical',
      'pagination': {**SEEK, 'cursor': {'field': 'id', 'unique': True}},
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


EXCLUSIVE_PARAMETERS = [
  *SEEK_PARAMETERS,
  {'name': 'startTime', 'in': 'query', 'required': False, 'description': 'Range start.', 'schema': {'type': 'integer', 'format': 'epoch-millis'}},
  {'name': 'endTime', 'in': 'query', 'required': False, 'description': 'Range end.', 'schema': {'type': 'integer', 'format': 'epoch-millis'}},
]

EXCLUSIVE = {
  **SEEK,
  'exclusive': {
    'parameters': ['startTime', 'endTime'],
    'first': 'startTime',
    'far': {'parameter': 'endTime', 'field': '[-1].time'},
  },
}
"""aster's `userTrades`: a time range refused alongside the id cursor."""

TRADE_ROW = {
  'id': {'type': 'integer', 'description': 'Trade id.'},
  'time': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Trade time.'},
}


def exclusive_parameters(end_time: dict) -> list[dict]:
  """`EXCLUSIVE_PARAMETERS` with `startTime`/`endTime` retyped."""
  return [
    *SEEK_PARAMETERS,
    *({**parameter, 'schema': end_time} for parameter in EXCLUSIVE_PARAMETERS[len(SEEK_PARAMETERS):]),
  ]


def test_a_seek_exclusive_range_naming_real_parameters_and_fields_is_clean(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.mine', parameters=EXCLUSIVE_PARAMETERS, pagination=EXCLUSIVE,
    row_properties=TRADE_ROW,
  )
  spec_test(path=str(tmp_path), verbose=False)
  assert_clean(capsys)


def test_a_seek_exclusive_parameter_the_operation_lacks_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.mine', parameters=EXCLUSIVE_PARAMETERS[:3], pagination=EXCLUSIVE,
    row_properties=TRADE_ROW,
  )
  _, err = run_failing(tmp_path, capsys)
  assert 'pagination.exclusive.parameters[1]' in err
  assert '`endTime` is not a parameter of this operation' in err


def test_a_seek_exclusive_far_field_absent_from_the_row_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.mine', parameters=EXCLUSIVE_PARAMETERS, pagination=EXCLUSIVE,
    row_properties={'id': TRADE_ROW['id']},
  )
  _, err = run_failing(tmp_path, capsys)
  assert 'pagination.exclusive.far.field' in err
  assert '`time` names no property of a row' in err


def test_a_seek_exclusive_far_field_that_cannot_be_ordered_is_flagged(tmp_path, capsys):
  write_seek_endpoint(
    tmp_path, function='trades.mine', parameters=exclusive_parameters({'type': 'integer'}), pagination=EXCLUSIVE,
    row_properties={**TRADE_ROW, 'time': {'type': 'string', 'description': 'Trade time, as text.'}},
  )
  _, err = run_failing(tmp_path, capsys)
  assert '`time` cannot be ordered against `endTime`' in err


@pytest.mark.parametrize(('end_time', 'time', 'location', 'message'), [
  (
    {'type': 'integer'}, {'type': 'integer', 'format': 'epoch-millis'},
    'pagination.exclusive.far.field',
    '`time` declares `epoch-millis` and `endTime` no timestamp format',
  ),
  (
    {'type': 'string'}, {'type': 'integer'},
    'pagination.exclusive.far.parameter', '`endTime` has no order to compare rows with',
  ),
  (
    {'type': 'integer', 'format': 'epoch-seconds'}, {'type': 'integer', 'format': 'epoch-millis'},
    'pagination.exclusive.far.field',
    '`time` declares `epoch-millis` and `endTime` `epoch-seconds`',
  ),
  (
    {'type': 'integer', 'format': 'epoch-millis'}, {'type': 'integer'},
    'pagination.exclusive.far.field',
    '`time` declares no timestamp format and `endTime` `epoch-millis`',
  ),
], ids=['int-param-millis-field', 'str-param-int-field', 'seconds-param-millis-field', 'millis-param-int-field'])
def test_a_seek_exclusive_far_bound_the_walk_cannot_compare_is_flagged(
  tmp_path, capsys, end_time: dict, time: dict, location: str, message: str,
):
  """Every backend reads the row's `time` through `endTime`'s own converter (review TRU-74,
  TRU-75): a parameter with no order, or a timestamp format on one side only or different
  on each, would drop the wrong rows or none."""
  write_seek_endpoint(
    tmp_path, function='trades.mine', parameters=exclusive_parameters(end_time), pagination=EXCLUSIVE,
    row_properties={**TRADE_ROW, 'time': {**time, 'description': 'Trade time.'}},
  )
  _, err = run_failing(tmp_path, capsys)
  assert location in err
  assert message in ' '.join(err.split())


@pytest.mark.parametrize(('end_time', 'time'), [
  ({'type': 'integer'}, {'type': 'integer'}),
  ({'type': 'integer'}, {'type': 'string', 'format': 'integer-string'}),
  ({'type': 'string', 'format': 'date-time'}, {'type': 'string', 'format': 'date-time'}),
], ids=['int-int', 'int-integer-string', 'date-time'])
def test_a_seek_exclusive_far_bound_of_one_kind_on_both_sides_is_clean(tmp_path, capsys, end_time: dict, time: dict):
  write_seek_endpoint(
    tmp_path, function='trades.mine', parameters=exclusive_parameters(end_time), pagination=EXCLUSIVE,
    row_properties={**TRADE_ROW, 'time': {**time, 'description': 'Trade time.'}},
  )
  spec_test(path=str(tmp_path), verbose=False)
  out = capsys.readouterr().out
  assert 'violations 0' in out  # an unformatted `startTime`/`endTime` still warns (rule 3)


@pytest.mark.parametrize(('exclusive', 'message'), [
  ({'parameters': ['startTime'], 'first': 'endTime'}, '`exclusive.first` is `endTime`'),
  ({'parameters': ['startTime'], 'far': {'parameter': 'endTime', 'field': '[-1].time'}}, '`exclusive.far.parameter` is `endTime`'),
  ({'parameters': ['startTime', 'startTime']}, 'lists a parameter twice'),
  ({'parameters': []}, 'at least 1 item'),
  ({'parameters': ['from_id']}, 'the walk sends on every request'),
  ({'parameters': ['startTime'], 'far': {'parameter': 'startTime', 'field': 'time'}}, 'does not index the last row'),
])
def test_a_malformed_seek_exclusive_is_rejected_at_load_time(exclusive, message):
  """Each shape `SeekExclusive`/`SeekPagination` refuse before any check runs."""
  from pydantic import ValidationError

  from truewire.spec import Endpoint

  with pytest.raises(ValidationError, match=message):
    Endpoint.model_validate({
      'function': 'trades.mine',
      'pagination': {**SEEK, 'exclusive': exclusive},
      'spec': {
        'kind': 'rpc', 'transports': ['http'], 'method': 'GET', 'path': '/trades',
        'openapi': {
          'description': 'x', 'parameters': EXCLUSIVE_PARAMETERS,
          'responses': {'200': {'description': 'x'}},
        },
      },
    })


def test_a_seek_exclusive_beside_a_span_is_rejected_at_load_time():
  """A span re-sends both bounds on every request; an exclusive parameter is sent once."""
  from pydantic import ValidationError

  from truewire.spec.endpoint import SeekPagination

  with pytest.raises(ValidationError, match='`exclusive` with `span` has no walker'):
    SeekPagination.model_validate({
      **SEEK, 'bound': {'start': 'from_id', 'end': 'to_id'},
      'span': {'parameter': 'span', 'default': 10, 'unit': 'ms'},
      'exclusive': {'parameters': ['startTime']},
    })
