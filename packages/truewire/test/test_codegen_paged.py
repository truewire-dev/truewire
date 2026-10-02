"""
Pin the page-method renderers (ADR 0021): every declared `pagination` generates one
`PaginatedResponse`-shaped `<method>_paged` method whose `next(state)` is a pure function of
`state` -- `paged_response_indexed` for `page`/`offset`, `paged_response_token` for `token`,
`paged_response_seek` for `seek`.

A `pagination` block is a spec fact, but the walk it implies is a convention every client
would otherwise reinvent. These tests execute the source they generate against canned or
computed pages, because a walk that reads correctly and never terminates -- or silently
skips the rows a venue withheld -- is exactly the defect the declaration replaces.
"""
from typing_extensions import Any, Callable, Literal, Sequence, overload
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import subprocess
import sys
import textwrap

import pytest

from truewire.generation.python.code import Function
from truewire_core import PaginatedResponse
from truewire_core.exceptions import LogicError

from truewire.codegen.python import Generator
from truewire.spec import Endpoint


@pytest.fixture
def generator() -> Generator:
  """The shared base generator, with no client-specific behaviour."""
  return Generator()


def paged_endpoint(
  pagination: dict[str, Any], *, size_default: int | None = None,
  size_maximum: int | None = None, parameters: list[dict[str, Any]] | None = None,
) -> Endpoint:
  """Build an HTTP endpoint record carrying one pagination declaration.

  Args:
    pagination: The declaration the endpoint carries.
    size_default: Row cap the venue applies when the caller sends no page size, declared
      on the size parameter's own schema as bybit declares it. `None` leaves the operation
      silent about one, which is mexc's shape.
    size_maximum: Largest page size the venue honours, declared as the schema's `maximum`.
    parameters: Extra operation parameters, when a test needs them declared.
  """
  declared = list(parameters or [])
  size = pagination.get('size')
  if (size_default is not None or size_maximum is not None) and size is not None:
    schema: dict[str, Any] = {'type': 'integer', 'description': 'Page size.'}
    if size_default is not None:
      schema['default'] = size_default
    if size_maximum is not None:
      schema['maximum'] = size_maximum
    declared.append({
      'name': size['parameter'], 'in': 'query', 'required': False,
      'description': 'Page size.', 'schema': schema,
    })
  return Endpoint.model_validate({
    'function': 'market.orders',
    'pagination': pagination,
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/v5/market/orders', 'method': 'GET',
      'openapi': {
        'summary': 'Get Orders',
        'parameters': declared,
        'responses': {'200': {
          'description': 'Success',
          'content': {'application/json': {'schema': {
            'title': 'Orders', 'type': 'object', 'required': ['s'],
            'properties': {'s': {'type': 'string'}},
          }}},
        }},
      },
    },
  })


def header(*, args: list[str] = [], kwargs: list[str], types: dict[str, str] = {}, required: set[str] = set(), validate: bool = True) -> Function:
  """Build the header a backend would hand a page renderer, from bare parameter names.

  Args:
    args: Positional parameter names, typed `str`.
    kwargs: Keyword parameter names, typed `int` and optional unless overridden.
    types: Per-parameter type overrides.
    required: Keyword parameters that are required on the single-request method.
    validate: Whether the sibling carries a `validate` keyword.
  """
  out = Function(name='orders', asyn=True, method=True)
  out.args = [Function.Param(name=name, type=types.get(name, 'str')) for name in args]
  out.kwargs = [
    Function.Param(name=name, type=types.get(name, 'int'), required=name in required)
    for name in kwargs
  ]
  if validate:
    out.kwargs.append(Function.Param(name='validate', type='bool | None', default='None'))
  out.return_type = 'Orders'
  return out


class Millis:
  """Stand-in for a client core's `timestamp_millis` converter."""

  def parse(self, value: Any) -> datetime:
    """Epoch milliseconds (or a numeral string) to an aware `datetime`."""
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)

  def dump(self, value: datetime) -> int:
    """An aware `datetime` to epoch milliseconds."""
    return int(value.timestamp() * 1000)


def namespace(extra: dict[str, Any] | None) -> dict[str, Any]:
  """The exec namespace every generated page method needs."""
  from typing_extensions import Any, cast
  return {
    'Any': Any,
    'PaginatedResponse': PaginatedResponse, 'LogicError': LogicError, 'Orders': dict,
    'Sequence': Sequence,
    'datetime': datetime, 'timedelta': timedelta, 'cast': cast,
    'TimestampMillis': datetime, 'timestamp_millis': Millis(),
    **(extra or {}),
  }


def build(source: str, venue: Callable[..., Any], extra: dict[str, Any] | None = None) -> Any:
  """Exec a generated page method into a stub class whose `orders(**kwargs)` is `venue`,
  recording every call's keyword arguments on `instance.calls`.

  Args:
    source: Generated `<method>_paged` source, unindented.
    venue: Called with the single-request method's keyword arguments; returns the page.
    extra: Additional names the source needs at exec time.
  """
  space = namespace(extra)
  # `validate_overloads` stubs on every page method need these at class-definition time.
  space.setdefault('overload', overload)
  space.setdefault('Literal', Literal)
  space['_venue'] = venue
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint class the generated page method is mixed into."""',
    '  def __init__(self):',
    '    self.calls = []',
    '  async def orders(self, *args, **kwargs):',
    '    self.calls.append(kwargs)',
    '    return _venue(*args, **kwargs)',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, space)
  return space['Walk']()


def canned(pages: list[Any]) -> Callable[..., Any]:
  """A venue returning `pages` in order, regardless of arguments."""
  served = iter(pages)
  return lambda *args, **kwargs: next(served)


def walk(
  source: str, venue: Callable[..., Any] | list[Any], *, extra: dict[str, Any] | None = None,
  **call: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
  """Iterate a generated page method, returning the pages yielded and the calls made."""
  instance = build(source, canned(venue) if isinstance(venue, list) else venue, extra)

  async def collect() -> list[Any]:
    return [page async for page in getattr(instance, 'orders_paged')(**call)]

  return asyncio.run(collect()), instance.calls


def walk_partial(
  source: str, venue: Callable[..., Any] | list[Any], *, extra: dict[str, Any] | None = None,
  **call: Any,
) -> tuple[list[Any], list[dict[str, Any]], BaseException]:
  """Like `walk`, for a walk expected to raise partway: returns what was yielded before
  the raise, the calls made, and the exception."""
  instance = build(source, canned(venue) if isinstance(venue, list) else venue, extra)

  async def collect() -> tuple[list[Any], BaseException | None]:
    yielded: list[Any] = []
    try:
      async for page in getattr(instance, 'orders_paged')(**call):
        yielded.append(page)
    except BaseException as error:
      return yielded, error
    return yielded, None

  yielded, error = asyncio.run(collect())
  assert error is not None, 'expected the walk to raise'
  return yielded, instance.calls, error


def signature(source: str) -> str:
  """The generated method's implementation header, up to its return annotation (past the
  `validate` overload stubs truewire renders ahead of it)."""
  implementation = source.split('\ndef ')[-1] if '\ndef ' in source else source
  return implementation.split(') -> PaginatedResponse[')[0]


def prose(source: str) -> str:
  """The source with its docstring wrapping collapsed, for substring checks."""
  return ' '.join(source.split())


PAGE_TOTAL = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'total', 'path': 'data.totalPage', 'counts': 'pages', 'rows': 'data.list'},
}
"""mexc's shape: a page number and a page count nested under an envelope key."""

PAGE_TOTAL_ITEMS = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'rows'},
}
"""binance's `simple_earn/locked/list` shape: an item total beside a `rows` field."""

PAGE_SHORT = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'short_page', 'rows': 'items'},
}

PAGE_EMPTY_BARE = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'done': {'kind': 'empty'},
}
"""dYdX's shape: the payload is the row collection, an empty page ends the walk."""

OFFSET_EMPTY = {
  'strategy': 'offset',
  'offset': {'parameter': 'offset'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'empty'},
}
"""bit2me's shape: a row offset walked until a page comes back empty."""

OFFSET_TOTAL = {
  'strategy': 'offset',
  'offset': {'parameter': 'offset'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'rows'},
}

TOKEN = {
  'strategy': 'token',
  'cursor': {'parameter': 'cursor', 'from': 'nextPageCursor'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'absent_cursor', 'rows': 'list'},
}
"""bybit's shape: an opaque token echoed back until the response stops supplying one."""

TOKEN_EMPTY = {**TOKEN, 'done': {'kind': 'empty', 'rows': 'list'}}
"""bitget's p2p shape: the venue always populates the token, so an empty page ends it."""


class TestPagedIndexed:
  """`page`/`offset`: the state is the index, an empty page always stops."""

  def test_undeclared_endpoint_generates_nothing(self, generator: Generator):
    ep = Endpoint.model_validate({**paged_endpoint(PAGE_TOTAL).model_dump(by_alias=True, exclude_none=True), 'pagination': None})
    assert generator.paged_response_method(
      ep, method_name='orders', header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) is None

  def test_renders_a_paginated_response_without_the_index(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    assert 'def orders_paged(' in source
    assert '-> PaginatedResponse[dict, int]:' in source
    assert 'page_size' in signature(source)
    assert 'page:' not in signature(source)
    assert 'max_pages' not in source

  def test_page_total_walks_to_the_reported_page_count(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    page = {'data': {'totalPage': 3, 'list': [{'a': 1}]}}
    yielded, calls = walk(source, [page, page, page])
    assert yielded == [[{'a': 1}]] * 3
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_a_missing_total_keeps_walking_until_an_empty_page(self, generator: Generator):
    """ADR 0021 drops the strict total: a page without one is not an error, the venue's
    own rows running out is the terminator."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    yielded, calls = walk(source, [
      {'data': {'list': [{'a': 1}]}}, {'data': {'list': [{'a': 2}]}}, {'data': {'list': []}},
    ])
    assert yielded == [[{'a': 1}], [{'a': 2}]]
    assert len(calls) == 3

  def test_a_moving_total_is_not_an_error(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    yielded, _ = walk(source, [
      {'data': {'totalPage': 3, 'list': [1]}}, {'data': {'totalPage': 4, 'list': [2]}},
      {'data': {'totalPage': 4, 'list': [3]}}, {'data': {'totalPage': 4, 'list': [4]}},
    ])
    assert yielded == [[1], [2], [3], [4]]

  def test_page_start_is_honoured(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint({**PAGE_TOTAL, 'index': {'parameter': 'page', 'start': 0}}),
      method_name='orders', header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    page = {'data': {'totalPage': 2, 'list': [1]}}
    _, calls = walk(source, [page, page])
    assert [call['page'] for call in calls] == [0, 1]

  def test_item_total_is_divided_by_the_page_size(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ITEMS), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    page = {'total': 5, 'rows': [1, 2]}
    yielded, calls = walk(source, [page, page, page], page_size=2)
    assert len(yielded) == 3 and len(calls) == 3

  def test_item_total_uses_the_documented_default_when_size_is_omitted(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ITEMS, size_default=2), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    page = {'total': 5, 'rows': [1, 2]}
    yielded, calls = walk(source, [page, page, page])
    assert len(yielded) == 3
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_item_total_without_a_size_falls_back_to_the_empty_page(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ITEMS), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    page = {'total': 3, 'rows': [1, 2]}
    yielded, calls = walk(source, [page, page, {'total': 3, 'rows': []}])
    assert len(yielded) == 2 and len(calls) == 3

  def test_short_page_is_measured_against_the_venues_maximum(self, generator: Generator):
    """Same clamp for an indexed walk: `page_size=5000` against a 2-row maximum must not
    read a 2-row page as short."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT, size_maximum=2), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    assert 'min(page_size, 2)' in source
    yielded, calls = walk(source, [{'items': [1, 2]}, {'items': [3, 4]}, {'items': [5]}], page_size=5000)
    assert yielded == [[1, 2], [3, 4], [5]]
    assert len(calls) == 3

  def test_short_page_counts_the_declared_collection(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    yielded, calls = walk(source, [{'items': [1, 2]}, {'items': [3]}], page_size=2)
    assert yielded == [[1, 2], [3]]
    assert len(calls) == 2

  def test_a_bare_array_payload_is_the_row_collection(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_EMPTY_BARE), method_name='orders',
      header=header(kwargs=['page']), rows_type='dict',
    ) or ''
    yielded, calls = walk(source, [[1, 2], [3], []])
    assert yielded == [[1, 2], [3]]
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_offset_advances_by_the_rows_received(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(OFFSET_EMPTY), method_name='orders',
      header=header(kwargs=['offset', 'limit']), rows_type='dict',
    ) or ''
    _, calls = walk(source, [[1, 2, 3], [4, 5], []])
    assert [call['offset'] for call in calls] == [0, 3, 5]

  def test_offset_total_stops_once_the_item_count_is_covered(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(OFFSET_TOTAL), method_name='orders',
      header=header(kwargs=['offset', 'limit']), rows_type='dict',
    ) or ''
    yielded, calls = walk(source, [{'total': 5, 'rows': [1, 2, 3]}, {'total': 5, 'rows': [4, 5]}])
    assert yielded == [[1, 2, 3], [4, 5]]
    assert [call['offset'] for call in calls] == [0, 3]

  @pytest.mark.parametrize('size_kind', ['optional', 'default', 'required'])
  @pytest.mark.parametrize('row_count', [0, 2, 8, 10])
  @pytest.mark.parametrize('start', [0, 1, 5])
  def test_offset_page_total_counts_a_partial_last_page(self, generator: Generator, size_kind: str, row_count: int, start: int):
    pagination = {**OFFSET_TOTAL, 'done': {**OFFSET_TOTAL['done'], 'counts': 'pages'}}
    source = generator.paged_response_method(
      paged_endpoint(pagination, size_default=4 if size_kind == 'default' else None),
      method_name='orders', rows_type='dict',
      header=header(kwargs=['offset', 'limit'], required={'limit'} if size_kind == 'required' else set()),
    ) or ''
    data = list(range(row_count))
    pages = (row_count + 3) // 4
    instance = build(source, lambda **kw: {'total': pages, 'rows': data[kw['offset']:kw['offset'] + 4]})

    async def collect():
      return await instance.orders_paged(**({} if size_kind == 'default' else {'limit': 4})).resume(start)

    assert asyncio.run(collect()) == data[start:]
    expected_calls = list(range(start, max(row_count, start + 1), 4))
    # An unaligned page may end before the final page boundary; an empty call then
    # establishes exhaustion without assuming where the last row lies.
    if expected_calls[-1] // 4 + 1 < pages:
      expected_calls.append(row_count)
    assert [call['offset'] for call in instance.calls] == expected_calls

  def test_await_flattens_every_page(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    instance = build(source, canned([{'items': [1, 2]}, {'items': [3]}]))

    async def flatten():
      return await instance.orders_paged(page_size=2)

    assert asyncio.run(flatten()) == [1, 2, 3]

  def test_next_is_pure_in_its_state(self, generator: Generator):
    """Retrying or resuming a page needs `next(state)` to depend on nothing but `state`:
    fetching page 2 twice makes the same request and returns the same rows."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    instance = build(source, lambda **kw: {'items': [kw['page']] * 2 if kw['page'] < 3 else []})
    paging = instance.orders_paged(page_size=2)

    async def twice():
      return await paging.next(2), await paging.next(2)

    first, second = asyncio.run(twice())
    assert first == second == ([2, 2], 3)
    assert [call['page'] for call in instance.calls] == [2, 2]

  def test_docstring_names_the_terminator(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL), method_name='orders',
      header=header(kwargs=['page', 'page_size']), rows_type='dict',
    ) or ''
    assert 'Requests `page` from 1 upwards' in prose(source)
    assert 'covered the `data.totalPage` pages the response reports' in prose(source)


class TestPagedToken:
  """`token`: the state is the cursor, seeded from the type's zero value."""

  def test_absent_cursor_ends_the_walk(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN), method_name='orders', header=header(kwargs=['cursor', 'limit'], types={'cursor': 'str'}),
      rows_type='dict', state_type='str', zero_value_is_wire_absent=False,
    ) or ''
    assert '-> PaginatedResponse[dict, str]:' in source
    assert 'cursor' not in signature(source)
    yielded, calls = walk(source, [
      {'list': [1], 'nextPageCursor': 'c1'}, {'list': [2], 'nextPageCursor': 'c2'},
      {'list': [3], 'nextPageCursor': ''},
    ])
    assert yielded == [[1], [2], [3]]
    assert [call['cursor'] for call in calls] == [None, 'c1', 'c2']

  def test_empty_termination_relays_the_cursor_until_an_empty_page(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_EMPTY), method_name='orders', header=header(kwargs=['cursor', 'limit'], types={'cursor': 'str'}),
      rows_type='dict', state_type='str', zero_value_is_wire_absent=False,
    ) or ''
    yielded, calls = walk(source, [
      {'list': [1], 'nextPageCursor': 'c1'}, {'list': [], 'nextPageCursor': 'c2'},
    ])
    assert yielded == [[1]]
    assert [call['cursor'] for call in calls] == [None, 'c1']

  def test_a_proto3_zero_value_cursor_is_sent_as_is(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN), method_name='orders', header=header(kwargs=['cursor', 'limit'], types={'cursor': 'bytes'}),
      rows_type='dict', state_type='bytes', zero_value_is_wire_absent=True,
    ) or ''
    _, calls = walk(source, [{'list': [1], 'nextPageCursor': None}])
    assert calls[0]['cursor'] == b''

  def test_a_required_cursor_stays_on_the_signature_and_seeds_the_walk(self, generator: Generator):
    """deribit's `get_volatility_index_data`: `end_timestamp` has no server-side default,
    so the caller's own value is the seed, and no zero value is needed."""
    pagination = {
      'strategy': 'token', 'cursor': {'parameter': 'end_timestamp', 'from': 'continuation'},
      'done': {'kind': 'absent_cursor', 'rows': 'list'},
    }
    source = generator.paged_response_method(
      paged_endpoint(pagination), method_name='orders',
      header=header(kwargs=['end_timestamp'], types={'end_timestamp': 'TimestampMillis'}, required={'end_timestamp'}),
      rows_type='dict', state_type='TimestampMillis', zero_value_is_wire_absent=False,
    ) or ''
    assert 'end_timestamp: TimestampMillis' in signature(source)
    assert 'end_timestamp: TimestampMillis | None' not in signature(source)
    assert 'return PaginatedResponse(end_timestamp, next)' in source
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    later = datetime.fromtimestamp(5, tz=timezone.utc)
    yielded, calls = walk(source, [
      {'list': [{'a': 1}], 'continuation': later}, {'list': [{'a': 2}], 'continuation': None},
    ], end_timestamp=start)
    assert [call['end_timestamp'] for call in calls] == [start, later]
    assert yielded == [[{'a': 1}], [{'a': 2}]]

  def test_an_optional_cursor_with_no_zero_value_is_refused(self, generator: Generator):
    with pytest.raises(ValueError, match='no zero value'):
      generator.paged_response_method(
        paged_endpoint(TOKEN), method_name='orders',
        header=header(kwargs=['cursor'], types={'cursor': 'TimestampMillis'}),
        rows_type='dict', state_type='TimestampMillis', zero_value_is_wire_absent=False,
      )

  def test_pagination_driver_required_is_token_only(self, generator: Generator):
    pagination = paged_endpoint(TOKEN).pagination
    assert pagination is not None
    assert generator.pagination_driver_required(header(kwargs=['cursor'], required={'cursor'}), pagination) is True
    assert generator.pagination_driver_required(header(kwargs=['cursor']), pagination) is False
    page = paged_endpoint(PAGE_TOTAL).pagination
    assert page is not None
    assert generator.pagination_driver_required(header(kwargs=['page'], required={'page'}), page) is False


class FakePageRequest:
  """Stand-in for a betterproto2-generated nested pagination message."""

  def __init__(self, *, key: bytes = b'', limit: int | None = None):
    self.key = key
    self.limit = limit


TOKEN_NESTED = {
  'strategy': 'token',
  'cursor': {'parameter': 'pagination.key', 'from': 'pagination.next_key'},
  'size': {'parameter': 'pagination.limit'},
  'done': {'kind': 'absent_cursor', 'rows': 'items'},
}
"""dYdX's Cosmos-SDK shape: cursor and size nested inside one `PageRequest` message."""


def nested_header() -> Function:
  """One `pagination: PageRequest` parameter, not flat `key`/`limit` ones."""
  out = Function(name='orders', asyn=True, method=True)
  out.kwargs = [Function.Param(name='pagination', type='PageRequest', required=False)]
  out.return_type = 'Orders'
  return out


class TestPagedTokenNested:
  """A `token` walk whose cursor/size are dotted paths into one nested request message
  (`flatten_nested_pagination`) generates and executes the same as the flat case."""

  NESTED_FIELDS = {'pagination': {'key': 'bytes', 'limit': 'int'}}

  def render(self, generator: Generator, fields: dict[str, dict[str, str]] | None = None) -> str:
    return generator.paged_response_method(
      paged_endpoint(TOKEN_NESTED), method_name='orders', header=nested_header(),
      rows_type='dict', state_type='bytes', nested_fields=fields or self.NESTED_FIELDS,
      response_accessor='attr',
    ) or ''

  def test_nested_driver_leaves_the_outer_parameter_out_of_the_signature(self, generator: Generator):
    source = self.render(generator)
    assert 'pagination' not in signature(source)
    assert 'key' not in signature(source)
    assert 'limit: int | None = None' in signature(source)

  def test_nested_driver_walks_and_reconstructs_the_message_each_call(self, generator: Generator):
    source = self.render(generator)

    class Page:
      """A betterproto2-shaped response: attributes, not keys."""
      def __init__(self, items, next_key):
        self.items = items
        self.pagination = type('P', (), {'next_key': next_key})()

    yielded, calls = walk(
      source, [Page([1], b'page2'), Page([2], None)], extra={'PageRequest': FakePageRequest},
      limit=5,
    )
    assert yielded == [[1], [2]]
    assert list(calls[0].keys()) == ['pagination']
    assert calls[0]['pagination'].key == b'' and calls[0]['pagination'].limit == 5
    assert calls[1]['pagination'].key == b'page2' and calls[1]['pagination'].limit == 5

  def test_an_optional_nested_size_is_passed_through_uncoerced(self, generator: Generator):
    source = self.render(generator, {'pagination': {'key': 'bytes', 'limit': 'int | None'}})
    assert 'limit=limit' in source and 'if limit is not None else' not in source

  def test_unsupported_nested_field_type_raises(self, generator: Generator):
    with pytest.raises(ValueError, match='SomeEnum'):
      self.render(generator, {'pagination': {'key': 'bytes', 'limit': 'SomeEnum'}})

  def test_unknown_outer_parameter_raises(self, generator: Generator):
    bad = Function(name='orders', asyn=True, method=True)
    bad.kwargs = [Function.Param(name='address', type='str')]
    with pytest.raises(ValueError, match='pagination'):
      generator.paged_response_method(
        paged_endpoint(TOKEN_NESTED), method_name='orders', header=bad, rows_type='dict',
        state_type='bytes', nested_fields=self.NESTED_FIELDS,
      )


PAGE_NESTED = {
  'strategy': 'page',
  'index': {'parameter': 'pagination.page', 'start': 1},
  'size': {'parameter': 'pagination.limit'},
  'done': {'kind': 'short_page'},
}
"""bitget's shape: a REST POST body bundles `page`/`limit` inside one JSON object."""


class FakePageParams:
  """Stand-in for a generated request-body `TypedDict`."""

  def __init__(self, *, page: int | None = None, limit: int | None = None):
    self.page = page
    self.limit = limit


class TestPagedIndexedNested:
  def test_nested_index_walks_and_reconstructs_the_message_each_call(self, generator: Generator):
    out = Function(name='orders', asyn=True, method=True)
    out.kwargs = [Function.Param(name='pagination', type='PageParams', required=False)]
    source = generator.paged_response_method(
      paged_endpoint(PAGE_NESTED), method_name='orders', header=out, rows_type='dict',
      nested_fields={'pagination': {'page': 'int', 'limit': 'int'}},
    ) or ''
    assert 'pagination' not in signature(source)
    assert 'page:' not in signature(source) and 'page=' not in signature(source)
    yielded, calls = walk(source, [[1, 2, 3], [4]], extra={'PageParams': FakePageParams}, limit=3)
    assert yielded == [[1, 2, 3], [4]]
    assert calls[0]['pagination'].page == 1 and calls[0]['pagination'].limit == 3
    assert calls[1]['pagination'].page == 2 and calls[1]['pagination'].limit == 3


CANDLES_ASC = {
  'strategy': 'seek',
  'cursor': {'field': '[-1][0]', 'unique': True},
  'bound': {'start': 'start', 'end': 'end'},
  'anchor': 'start',
  'size': {'parameter': 'limit'},
}
"""binance's kline shape: oldest rows kept, walked forwards; row `[0]` is the open time."""

CANDLES_DESC = {**CANDLES_ASC, 'anchor': 'end'}
"""bybit's kline shape: newest rows kept, walked backwards."""

FILLS = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].t', 'unique': False},
  'bound': {'start': 'start', 'end': 'end'},
  'anchor': 'start',
  'cap': 3,
}
"""hyperliquid's shape: a non-unique millisecond cursor and a fixed row cap."""

ID_DESC = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].id', 'unique': True},
  'bound': {'end': 'id_less_than'},
  'anchor': 'end',
  'size': {'parameter': 'limit'},
}
"""bitget's shape: a string id, walked downwards from the newest, no far bound."""

OBSERVATIONS = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].properties.timestamp', 'unique': True},
  'bound': {'start': 'start', 'end': 'end'},
  'anchor': 'start',
  'size': {'parameter': 'limit'},
}
"""weather.gov's shape: the cursor two dict keys deep in each row."""

OBSERVATION_TYPES = '''
class Properties(TypedDict):
  timestamp: int

class Observation(TypedDict):
  properties: NotRequired[Properties]
'''
"""Row types for `OBSERVATIONS`: the outer key optional, as weather.gov's GeoJSON is."""

NESTED_LIST_TYPES = '''
class Leaf(TypedDict):
  b: int

class Row(TypedDict):
  a: NotRequired[list[Leaf]]
'''
"""Row types for `[-1].a[0].b`: a list index between two dict keys."""

THREE_LEVEL_TYPES = '''
class C(TypedDict):
  c: int

class B(TypedDict):
  b: NotRequired[C]

class Row(TypedDict):
  a: NotRequired[B]
'''
"""Row types for `[-1].a.b.c`: three dict keys, each level optional but the last."""


def candles(start: int, end: int, *, anchor: str, cap: int, step: int = 1) -> list[list[Any]]:
  """A venue serving one candle per `step` ticks over the inclusive range `[start, end]`,
  keeping the `cap` rows nearest `anchor`."""
  rows = [[t, 'o'] for t in range(start, end + 1, step)]
  return rows[:cap] if anchor == 'start' else rows[-cap:]


def int_header(*names: str, required: set[str] = set()) -> Function:
  """Bounds typed `int` (a block height, an epoch tick), plus `limit`."""
  return header(kwargs=[*names, 'limit'], types={name: 'int' for name in names}, required=required)


class TestPagedSeek:
  """`seek`: the moving bound follows the extreme cursor key of each full page."""

  def render(self, generator: Generator, pagination: dict[str, Any], *, size_default: int | None = 3, size_maximum: int | None = None, head: Function | None = None) -> str:
    return generator.paged_response_method(
      paged_endpoint(pagination, size_default=size_default, size_maximum=size_maximum),
      method_name='orders', header=head or int_header('start', 'end'), rows_type='list',
    ) or ''

  def test_a_limit_above_the_venues_maximum_is_clamped_for_the_cap(self, generator: Generator):
    """A caller asking for 5000 rows from a venue that clamps at 3 gets 3-row pages; a
    walk measuring fullness against 5000 would read every page as short and stop with
    rows left. The clamped size is also what every request sends."""
    source = self.render(generator, CANDLES_ASC, size_maximum=3)
    assert 'limit = min(max(limit, 2), 3) if limit is not None else None' in source
    assert 'cap: int | None = (limit if limit is not None else 3)' in source
    yielded, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=min(kw['limit'], 3)),
      start=0, end=9, limit=5000,
    )
    assert [row[0] for page in yielded for row in page] == list(range(10))
    assert len(calls) == 5
    assert {call['limit'] for call in calls} == {3}

  def test_renders_a_paginated_response_keeping_both_bounds(self, generator: Generator):
    source = self.render(generator, CANDLES_ASC)
    assert 'def orders_paged(' in source
    assert 'PaginatedResponse[list, tuple[int | None, list[list]]]' in source
    assert 'start: int | None = None' in signature(source)
    assert 'end: int | None = None' in signature(source)
    assert 'allow_truncation' not in source and 'max_pages' not in source

  def test_ascending_walk_covers_a_range_wider_than_one_page(self, generator: Generator):
    """A venue keeping the oldest 3 of a 10-row range: every row arrives exactly once, and
    no request ever leaves the caller's own `[0, 9]`."""
    source = self.render(generator, CANDLES_ASC)
    yielded, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=3), start=0, end=9,
    )
    assert [row[0] for page in yielded for row in page] == list(range(10))
    assert all(0 <= call['start'] <= call['end'] <= 9 for call in calls)
    assert [call['start'] for call in calls] == [0, 2, 4, 6, 8]

  def test_descending_walk_moves_the_end_bound(self, generator: Generator):
    source = self.render(generator, CANDLES_DESC)
    yielded, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='end', cap=3), start=0, end=9,
    )
    # Rows keep the venue's own wire order within a page; the pages arrive newest-first.
    assert [[row[0] for row in page] for page in yielded] == [[7, 8, 9], [5, 6], [3, 4], [1, 2], [0]]
    assert [call['end'] for call in calls] == [9, 7, 5, 3, 1]
    assert all(call['start'] == 0 for call in calls)

  def test_a_short_page_ends_the_walk_when_a_cap_resolves(self, generator: Generator):
    source = self.render(generator, CANDLES_ASC)
    _, calls = walk(source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=3), start=0, end=1)
    assert len(calls) == 1

  def test_an_explicit_size_is_the_cap(self, generator: Generator):
    source = self.render(generator, CANDLES_ASC)
    _, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=kw['limit']),
      start=0, end=9, limit=5,
    )
    assert [call['start'] for call in calls] == [0, 4, 8]

  def test_without_a_cap_the_walk_confirms_exhaustion_with_one_more_request(self, generator: Generator):
    """No `size` default and no `cap`: a short page cannot be told from a full one, so the
    walk keeps moving to the extreme key until a page brings nothing new."""
    source = self.render(generator, CANDLES_ASC, size_default=None)
    assert 'cap: int | None = limit' in source
    yielded, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=3), start=0, end=4,
    )
    assert [row[0] for page in yielded for row in page] == [0, 1, 2, 3, 4]
    assert [call['start'] for call in calls] == [0, 2, 4]

  def test_a_callers_limit_is_the_cap_even_without_a_documented_default(self, generator: Generator):
    """dYdX's `historical_funding`: a fixed `cap: 1000` beside a `limit` with no default. A
    caller asking for 2 rows gets 2-row pages, so 2 is what a full page is measured against;
    the fixed cap only stands in when `limit` is omitted."""
    pagination = {**CANDLES_ASC, 'cap': 1000}
    source = self.render(generator, pagination, size_default=None)
    assert 'cap: int | None = (limit if limit is not None else 1000)' in source
    _, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=kw['limit']),
      start=0, end=9, limit=2,
    )
    assert [call['start'] for call in calls] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]

  def test_a_re_served_boundary_row_is_dropped_by_key_not_content(self, generator: Generator):
    """An open candle's other fields change between requests; keyed dedup still drops it."""
    source = self.render(generator, CANDLES_ASC)
    served = {'n': 0}

    def venue(**kw):
      served['n'] += 1
      rows = candles(kw['start'], kw['end'], anchor='start', cap=3)
      return [[t, f'v{served["n"]}'] for t, _ in rows]

    yielded, _ = walk(source, venue, start=0, end=4)
    assert [row[0] for page in yielded for row in page] == [0, 1, 2, 3, 4]

  def test_an_exclusive_bound_needs_no_step(self, generator: Generator):
    """A venue whose moving bound is exclusive never re-serves the boundary row; the walk
    neither skips nor duplicates anything either way."""
    source = self.render(generator, CANDLES_ASC)
    yielded, _ = walk(
      source, lambda **kw: candles(kw['start'] + 1 if kw['start'] else 0, kw['end'], anchor='start', cap=3),
      start=0, end=9,
    )
    assert [row[0] for page in yielded for row in page] == list(range(10))

  def test_rows_arrive_in_any_wire_order(self, generator: Generator):
    """bitget serves candles oldest-first even when walked backwards; the extreme key is
    computed, never read positionally."""
    source = self.render(generator, CANDLES_DESC)
    yielded, calls = walk(
      source, lambda **kw: list(reversed(candles(kw['start'], kw['end'], anchor='end', cap=3))),
      start=0, end=9,
    )
    assert sorted(row[0] for page in yielded for row in page) == list(range(10))
    assert [call['end'] for call in calls] == [9, 7, 5, 3, 1]

  def test_a_full_page_sharing_one_key_raises(self, generator: Generator):
    source = self.render(generator, CANDLES_ASC)
    with pytest.raises(LogicError, match='all sharing one'):
      walk(source, lambda **kw: [[kw['start'], 'a']] * 3, start=0, end=9)

  def test_an_omitted_moving_bound_starts_from_the_venues_default(self, generator: Generator):
    source = self.render(generator, ID_DESC, head=header(kwargs=['id_less_than', 'limit'], types={'id_less_than': 'str'}))
    ids = ['9', '8', '7', '6', '5']

    def venue(**kw):
      below = ids if kw['id_less_than'] is None else [i for i in ids if int(i) < int(kw['id_less_than'])]
      return [{'id': i} for i in below[:3]]

    yielded, calls = walk(source, venue)
    assert [row['id'] for page in yielded for row in page] == ids
    assert [call['id_less_than'] for call in calls] == [None, '7']

  def test_a_string_key_takes_the_last_row_in_wire_order(self, generator: Generator):
    source = self.render(generator, ID_DESC, head=header(kwargs=['id_less_than', 'limit'], types={'id_less_than': 'str'}))
    assert 'values[-1] if values else None' in source
    assert 'min(values)' not in source

  def test_non_unique_keys_are_carried_over_and_dropped_by_content(self, generator: Generator):
    """hyperliquid's fills: three rows share millisecond 2; the page cap falls in the middle
    of them, so the walk re-requests from 2 and drops the two it already yielded."""
    rows = [{'t': 1, 'i': 'a'}, {'t': 2, 'i': 'b'}, {'t': 2, 'i': 'c'}, {'t': 3, 'i': 'e'}, {'t': 4, 'i': 'f'}]
    source = self.render(generator, FILLS, size_default=None, head=int_header('start', 'end'))
    yielded, calls = walk(
      source, lambda **kw: [r for r in rows if kw['start'] <= r['t'] <= kw['end']][:3], start=0, end=9,
    )
    assert [row['i'] for page in yielded for row in page] == ['a', 'b', 'c', 'e', 'f']
    assert [call['start'] for call in calls] == [0, 2, 3]

  def test_a_carried_row_missing_from_the_next_page_raises(self, generator: Generator):
    source = self.render(generator, FILLS, size_default=None, head=int_header('start', 'end'))
    calls_made = {'n': 0}

    def unstable(**kw):
      calls_made['n'] += 1
      if calls_made['n'] == 1:
        return [{'t': 1, 'i': 'a'}, {'t': 2, 'i': 'b'}, {'t': 2, 'i': 'c'}]
      return [{'t': 2, 'i': 'zzz'}, {'t': 3, 'i': 'e'}]

    yielded, _, error = walk_partial(source, unstable, start=0, end=9)
    assert isinstance(error, LogicError) and 'no longer returned' in str(error)
    assert yielded == [[{'t': 1, 'i': 'a'}, {'t': 2, 'i': 'b'}, {'t': 2, 'i': 'c'}]]

  def test_a_non_unique_full_page_of_one_key_raises(self, generator: Generator):
    source = self.render(generator, FILLS, size_default=None, head=int_header('start', 'end'))
    with pytest.raises(LogicError):
      walk(source, lambda **kw: [{'t': kw['start'], 'i': i} for i in 'abc'], start=0, end=9)

  def test_a_span_covers_a_wide_range_in_bounded_requests(self, generator: Generator):
    """coinbase refuses a range wider than its cap; a declared `span` keeps every request
    within it while still covering the caller's whole range."""
    pagination = {**CANDLES_ASC, 'span': {'parameter': 'span', 'default': 4, 'unit': 's'}}
    source = self.render(generator, pagination)
    assert 'span: int = 4' in signature(source)

    def venue(**kw):
      assert kw['end'] - kw['start'] <= 4, 'venue refuses a wide range'
      return candles(kw['start'], kw['end'], anchor='start', cap=3)

    yielded, calls = walk(source, venue, start=0, end=9)
    assert [row[0] for page in yielded for row in page] == list(range(10))
    assert all(0 <= call['start'] <= call['end'] <= 9 for call in calls)

  def test_a_span_requires_both_bounds(self, generator: Generator):
    pagination = {**CANDLES_ASC, 'span': {'parameter': 'span', 'default': 4, 'unit': 's'}}
    source = self.render(generator, pagination)
    instance = build(source, lambda **kw: [])
    with pytest.raises(ValueError, match='pass both'):
      instance.orders_paged(start=0)

  def test_timestamp_keys_are_parsed_before_comparing(self, generator: Generator):
    """With validation off the row's timestamp is a raw epoch integer while the bound is a
    `datetime`; the key is parsed through the bound's own converter first."""
    head = header(kwargs=['start', 'end', 'limit'], types={'start': 'TimestampMillis', 'end': 'TimestampMillis'})
    source = self.render(generator, CANDLES_DESC, head=head)
    assert 'timestamp_millis.parse' in source
    t = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)  # noqa: E731

    def venue(**kw):
      lo, hi = int(kw['start'].timestamp() * 1000), int(kw['end'].timestamp() * 1000)
      return candles(lo, hi, anchor='end', cap=3, step=1000)

    yielded, calls = walk(source, venue, start=t(0), end=t(9000))
    assert sorted(row[0] for page in yielded for row in page) == list(range(0, 10000, 1000))
    assert calls[1]['end'] == t(7000)

  def test_imports_follow_the_key_kind(self, generator: Generator):
    generator.core_package = 'venue.core'
    ep = paged_endpoint(CANDLES_DESC, size_default=3)
    plain = generator.paged_imports(ep, header=int_header('start', 'end'))
    assert plain == {
      'truewire_core': {'PaginatedResponse'}, 'typing_extensions': {'Sequence'},
      'truewire_core.exceptions': {'LogicError'},
    }
    stamped = generator.paged_imports(
      ep, header=header(kwargs=['start', 'end', 'limit'], types={'start': 'TimestampMillis', 'end': 'TimestampMillis'}),
    )
    assert stamped['truewire_core.types'] == {'timestamp_millis'}
    assert stamped['datetime'] == {'datetime'}
    assert stamped['typing_extensions'] == {'cast', 'Sequence'}

  def test_docstring_states_the_anchor_and_dedup(self, generator: Generator):
    source = self.render(generator, CANDLES_DESC)
    assert (
      'Walks backwards by moving `end` to the earliest first element among the rows of each '
      'page that came back full,'
    ) in prose(source)
    assert 'stops on the first page shorter than the venue' in prose(source)
    assert 'dropped by its first element,' in prose(source)
    docstrings = re.findall(r'"""(.*?)"""', source, re.DOTALL)
    assert docstrings and not any('[0]' in doc or '[-1]' in doc for doc in docstrings)

  def test_a_tuple_row_docstring_says_the_position_is_in_the_row(self, generator: Generator):
    """review/TRU-116: "the earliest value at position 0 of each page" reads as the
    page's first row, not each row's first element."""
    assert 'position 0 of each page' not in prose(self.render(generator, CANDLES_DESC))

  def test_a_cap_known_only_from_the_callers_size_says_every_page_moves_without_it(
    self, generator: Generator,
  ):
    """TRU-117: with `limit` unset and no default, the cap is unknown and every page that
    brings a new key moves the bound, full or not."""
    text = prose(self.render(generator, CANDLES_DESC, size_default=None))
    assert 'of each page that came back full, or of every page while `limit` is unset,' in text
    assert (
      'stops on the first page shorter than `limit`, or, while it is unset, on the first page '
      'that brings nothing new'
    ) in text

  def test_no_size_and_no_cap_says_every_page_moves(self, generator: Generator):
    text = prose(self.render(generator, {**CANDLES_ASC, 'size': None}, head=header(kwargs=['start', 'end'])))
    assert "to the latest first element among the rows of each page, never past the caller's own `end`" in text
    assert 'came back full' not in text

  def test_a_span_says_a_short_page_moves_to_the_edge_of_its_range(self, generator: Generator):
    """TRU-117: a short page of a span walk moves on to the next range; it does not end the walk."""
    text = prose(self.render(generator, {**CANDLES_ASC, 'span': {'parameter': 'span', 'default': 4, 'unit': 's'}}))
    assert 'came back full, and to the edge of the range it requested after a short one' in text
    assert 'covers the range in `span`-wide requests, stopping at `end`' in text
    assert 'stops on the first page shorter' not in text

  def test_a_bare_row_cursor_names_the_row_in_words(self, generator: Generator):
    """TRU-120: a `[-1]` cursor (the row itself is the key) never renders empty backticks."""
    endpoint = paged_endpoint({**OBSERVATIONS, 'cursor': {'field': '[-1]', 'unique': False}}, size_default=3)
    source = generator.paged_response_method(
      endpoint, method_name='orders', header=int_header('start', 'end'), rows_type='int',
    ) or ''
    assert '``' not in source
    text = prose(source)
    assert 'moving `start` to the latest row of each page that came back full' in text
    assert 'Rows sharing the boundary value are re-fetched' in text
    assert '"""One row, as a key, normalized' in source
    assert 'rows all sharing one row value' in source and 'already returned for that row;' in source

  def test_a_string_id_moves_to_the_last_rows(self, generator: Generator):
    """TRU-125: a plain string id is compared by equality only; the walk takes the last row's."""
    text = prose(self.render(generator, ID_DESC, head=header(kwargs=['id_less_than', 'limit'], types={'id_less_than': 'str'})))
    assert 'moving `id_less_than` to the `id` of the last row of each page that came back full' in text
    assert 'earliest' not in text

  def test_a_missing_bound_parameter_raises(self, generator: Generator):
    with pytest.raises(ValueError, match='nothing to move'):
      self.render(generator, CANDLES_ASC, head=header(kwargs=['end', 'limit']))

  # The page size (ADR 0013): a given integer size is clamped once, before the walk, to at
  # least 2 and at most the schema's `maximum`, and that value is both the cap and what
  # every request sends.

  def test_a_seek_walk_sends_the_size_it_clamps(self, generator: Generator):
    """`limit=600` against a 500-row maximum goes on the wire as 500, from the first request."""
    source = self.render(generator, CANDLES_DESC, size_default=500, size_maximum=500)
    assert 'limit = min(max(limit, 2), 500) if limit is not None else None' in source
    assert 'cap: int | None = (limit if limit is not None else 500)' in source
    assert 'response = await self.orders(start=start, end=pos, limit=limit, validate=validate)' in source
    _, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='end', cap=min(kw['limit'], 500)),
      start=0, end=9, limit=600,
    )
    assert [call['limit'] for call in calls] == [500]

  def test_limit_1_walks_every_row_of_an_inclusive_bound(self, generator: Generator):
    """The venue re-serves the row at the moving bound: a page of 1 would hold that row
    alone, full and sharing one key, and raise `LogicError`. The walk requests 2."""
    source = self.render(generator, CANDLES_DESC, size_default=500, size_maximum=500)
    yielded, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='end', cap=min(kw['limit'], 500)),
      start=0, end=9, limit=1,
    )
    assert sorted(row[0] for page in yielded for row in page) == list(range(10))
    assert {call['limit'] for call in calls} == {2}

  def test_an_omitted_size_stays_unset_and_the_default_is_the_cap(self, generator: Generator):
    source = self.render(generator, CANDLES_ASC, size_default=500, size_maximum=500)
    yielded, calls = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=3 if kw['limit'] is None else kw['limit']),
      start=0, end=9,
    )
    assert all(call['limit'] is None for call in calls)
    assert len(yielded) == 1  # 3 rows, short of the default 500: the walk ends

  def test_the_size_floor_holds_without_a_maximum_and_for_a_required_size(self, generator: Generator):
    unbounded = self.render(generator, CANDLES_ASC, size_default=500)
    assert 'limit = max(limit, 2) if limit is not None else None' in unbounded
    required = self.render(generator, CANDLES_ASC, size_default=500, size_maximum=500, head=int_header('start', 'end', required={'limit'}))
    assert '\n  limit = min(max(limit, 2), 500)\n' in required
    assert 'cap: int | None = limit\n' in required

  def test_a_maximum_below_2_is_the_page_size(self, generator: Generator):
    """With room for no floor, the walk requests pages of the maximum and the docstring
    claims no floor of 2: an exclusive venue still walks at 1 row a page."""
    source = self.render(generator, CANDLES_ASC, size_default=1, size_maximum=1)
    assert 'limit = min(max(limit, 2), 1) if limit is not None else None' in source
    assert 'The walk requests pages of 1 row.' in prose(source)
    assert 'at least 2' not in prose(source)

  def test_the_docstring_states_the_size_rule(self, generator: Generator):
    assert (
      'The walk requests pages of at least 2 rows and at most 500: a page must hold one new '
      'row beside the one it re-reads.'
    ) in prose(self.render(generator, CANDLES_ASC, size_default=500, size_maximum=500))

  @pytest.mark.parametrize('size_type, limit', [('float', 600.0), ('Literal[5, 600]', 600)])
  def test_a_size_the_clamp_skips_keeps_the_maximum_on_its_cap(self, generator: Generator, size_type: str, limit: object):
    """A size `paged_seek_size` leaves alone (not `int`) still measures a full page against
    the venue's `maximum`: a cap of 600 against a venue serving 500 would read the first
    page as short and stop with rows left."""
    head = header(kwargs=['start', 'end', 'limit'], types={'start': 'int', 'end': 'int', 'limit': size_type})
    source = self.render(generator, CANDLES_ASC, size_default=500, size_maximum=500, head=head)
    assert 'max(limit, 2)' not in source
    yielded, _ = walk(
      source, lambda **kw: candles(kw['start'], kw['end'], anchor='start', cap=min(int(kw['limit']), 500)),
      start=0, end=1999, limit=limit,
    )
    assert sum(len(page) for page in yielded) == 2000
  def test_a_nested_cursor_field_walks(self, generator: Generator):
    source = self.render(generator, OBSERVATIONS)
    yielded, calls = walk(
      source, lambda **kw: [{'properties': {'timestamp': t}} for t in range(kw['start'], kw['end'] + 1)][:3],
      start=0, end=9,
    )
    assert [row['properties']['timestamp'] for page in yielded for row in page] == list(range(10))
    assert [call['start'] for call in calls] == [0, 2, 4, 6, 8]

  def test_a_nested_cursor_field_binds_each_level(self, generator: Generator):
    """Each level is a local its child's guard narrows. Repeating the parent read inside
    the guard instead fails pyright: a repeated call expression never narrows."""
    source = self.render(generator, OBSERVATIONS)
    assert "raw_0 = (item.get('properties') if item is not None else None)" in source
    assert "raw = (raw_0.get('timestamp') if raw_0 is not None else None)" in source

  def test_a_missing_or_null_level_reads_as_no_key(self, generator: Generator):
    """A row without `properties`, or with `properties: None`, has no key: the walk skips
    it when picking the page's extreme instead of raising on `None.get`."""
    source = self.render(generator, OBSERVATIONS)
    pages = {0: [{}, {'properties': None}, {'properties': {'timestamp': 5}}], 5: [{'properties': {'timestamp': 6}}]}
    yielded, calls = walk(source, lambda **kw: pages[kw['start']], start=0, end=9)
    assert [row for page in yielded for row in page] == [*pages[0], *pages[5]]
    assert [call['start'] for call in calls] == [0, 5]

  @pytest.mark.parametrize(('field', 'rows_type', 'types'), [
    ('[-1].properties.timestamp', 'Observation', OBSERVATION_TYPES),
    ('[-1].a[0].b', 'Row', NESTED_LIST_TYPES),
    ('[-1].a.b.c', 'Row', THREE_LEVEL_TYPES),
    ('[-1]', 'int', ''),
  ], ids=['two-keys', 'list-index', 'three-keys', 'bare-row'])
  def test_a_cursor_field_type_checks(
    self, generator: Generator, tmp_path: Path, field: str, rows_type: str, types: str,
  ):
    """pyright in standard mode, the mode generated clients ship with, over the method
    mixed into a class whose single-request method returns the typed rows."""
    endpoint = paged_endpoint({**OBSERVATIONS, 'cursor': {'field': field, 'unique': True}}, size_default=3)
    head = int_header('start', 'end')
    source = generator.paged_response_method(
      endpoint, method_name='orders', header=head, rows_type=rows_type,
    ) or ''
    imports = generator.paged_imports(endpoint, header=head)
    imports.setdefault('typing_extensions', set()).update(
      {'Any', 'Literal', 'NotRequired', 'TypedDict', 'overload'},
    )
    module = '\n'.join([
      *(f'from {package} import {", ".join(sorted(names))}' for package, names in sorted(imports.items())),
      types,
      'class Walk:',
      '  async def orders(',
      '    self, *, start: int | None = None, end: int | None = None,',
      '    limit: int | None = None, validate: bool | None = None,',
      f'  ) -> list[{rows_type}]:',
      '    return []',
      '',
      textwrap.indent(source, '  '),
    ])
    (tmp_path / 'walk.py').write_text(module)
    (tmp_path / 'pyrightconfig.json').write_text(json.dumps({'typeCheckingMode': 'standard'}))
    result = subprocess.run(
      [sys.executable, '-m', 'pyright', '--pythonpath', sys.executable, 'walk.py'],
      capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr

class TestSeekBareLastRowCursor:
  """Review TRU-21 (PR #2): `[-1]` alone -- the row itself is the cursor -- is a valid
  `LastRowPath`, and `row_field_read` must still bind the key for it."""

  BARE = {
    'strategy': 'seek',
    'cursor': {'field': '[-1]', 'unique': True},
    'bound': {'start': 'start', 'end': 'end'},
    'anchor': 'start',
    'size': {'parameter': 'limit'},
  }

  def test_a_bare_last_row_cursor_walks(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(self.BARE, size_default=3), method_name='orders',
      header=int_header('start', 'end'), rows_type='list',
    ) or ''
    yielded, calls = walk(
      source, lambda **kw: list(range(kw['start'], kw['end'] + 1))[:3], start=0, end=9,
    )
    assert [row for page in yielded for row in page] == list(range(10))
    assert [call['start'] for call in calls] == [0, 2, 4, 6, 8]

USER_TRADES = {
  'strategy': 'seek',
  'cursor': {'field': '[-1].id', 'unique': True},
  'bound': {'start': 'from_id'},
  'anchor': 'start',
  'size': {'parameter': 'limit'},
  'exclusive': {
    'parameters': ['start_time', 'end_time'],
    'first': 'start_time',
    'far': {'parameter': 'end_time', 'field': '[-1].time'},
  },
}
"""aster's `userTrades`: a time range the venue refuses alongside `fromId`, so it is sent on
the first request only and `endTime` is enforced on each row's `time`."""


class TestPagedSeekExclusive:
  """`seek.exclusive`: parameters sent on the first request only, the far one enforced on
  the rows."""

  def render(self, generator: Generator) -> str:
    head = header(
      kwargs=['from_id', 'start_time', 'end_time', 'limit'],
      types={'start_time': 'TimestampMillis', 'end_time': 'TimestampMillis'},
    )
    return generator.paged_response_method(
      paged_endpoint(USER_TRADES, size_default=2), method_name='orders', header=head, rows_type='dict',
    ) or ''

  @staticmethod
  def venue(trades: list[dict[str, Any]]) -> Callable[..., Any]:
    """Two trades per page from `from_id` (inclusive), or from `start_time` on a first page;
    refuses a time filter next to `from_id`, as aster answers `-1106`."""
    def serve(**kw):
      if kw['from_id'] is not None and (kw['start_time'] is not None or kw['end_time'] is not None):
        raise AssertionError('the venue refuses startTime/endTime alongside fromId')
      if kw['from_id'] is not None:
        rows = [trade for trade in trades if trade['id'] >= kw['from_id']]
      else:
        start = int(kw['start_time'].timestamp() * 1000)
        rows = [trade for trade in trades if trade['time'] >= start]
      return rows[:kw['limit'] or 2]
    return serve

  def test_the_range_is_sent_first_and_the_far_bound_is_kept_on_the_rows(self, generator: Generator):
    source = self.render(generator)
    t = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)  # noqa: E731
    trades = [{'id': i, 'time': 1000 * i} for i in range(1, 8)]
    yielded, calls = walk(source, self.venue(trades), start_time=t(1000), end_time=t(4000))
    assert [trade['id'] for page in yielded for trade in page] == [1, 2, 3, 4]
    assert [(c['from_id'], c['start_time'], c['end_time']) for c in calls] == [
      (None, t(1000), t(4000)), (2, None, None), (3, None, None), (4, None, None),
    ]

  def test_a_caller_moving_bound_never_sends_the_range(self, generator: Generator):
    source = self.render(generator)
    trades = [{'id': i, 'time': 1000 * i} for i in range(1, 5)]
    yielded, calls = walk(source, self.venue(trades), from_id=2)
    assert [trade['id'] for page in yielded for trade in page] == [2, 3, 4]
    assert all(c['start_time'] is None and c['end_time'] is None for c in calls)

  def test_a_caller_moving_bound_keeps_the_far_bound_on_the_rows(self, generator: Generator):
    """`from_id` + `end_time` resumes a capped walk (review TRU-93): `end_time` is never
    sent, and the trade past it is dropped all the same."""
    source = self.render(generator)
    t = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)  # noqa: E731
    trades = [{'id': i, 'time': 1000 * i} for i in range(1, 8)]
    yielded, calls = walk(source, self.venue(trades), from_id=2, end_time=t(4000))
    assert [trade['id'] for page in yielded for trade in page] == [2, 3, 4]
    assert all(c['start_time'] is None and c['end_time'] is None for c in calls)

  def test_every_row_past_the_far_bound_is_dropped_and_the_page_ends_the_walk(self, generator: Generator):
    """A page `[3, 4 (past), 5]` yields `[3, 5]`: the rows are not assumed to arrive in
    time order, so an in-range row after a past one is kept."""
    source = self.render(generator)
    t = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)  # noqa: E731
    trades = [{'id': 3, 'time': 3000}, {'id': 4, 'time': 9000}, {'id': 5, 'time': 5000}]
    yielded, calls = walk(source, lambda **kw: trades, start_time=t(1000), end_time=t(6000), limit=3)
    assert [trade['id'] for page in yielded for trade in page] == [3, 5]
    assert len(calls) == 1

  def test_the_moving_bound_beside_a_non_far_parameter_or_neither_raises_before_any_request(self, generator: Generator):
    source = self.render(generator)
    instance = build(source, lambda **kw: [])
    with pytest.raises(ValueError, match='refuses alongside `start_time`: pass one or the other'):
      instance.orders_paged(from_id=1, start_time=datetime.now(timezone.utc))
    with pytest.raises(ValueError, match='needs `from_id` or `start_time`'):
      instance.orders_paged(end_time=datetime.now(timezone.utc))
    assert instance.calls == []

  def test_docstring_states_the_first_request_and_the_far_bound(self, generator: Generator):
    text = prose(self.render(generator))
    assert (
      'Drops every row whose `time` is past the caller\'s own `end_time`, and ends the walk on '
      'the page that held one. The venue refuses `start_time`/`end_time` alongside `from_id`, '
      'so they are sent on the first request only, and never when the caller gives `from_id`; '
      'pass `start_time` or `from_id`, not both. Without `from_id`, `start_time` is required.'
    ) in text
    assert 'stops at the first row' not in text

  def test_imports_the_far_bounds_converter(self, generator: Generator):
    generator.core_package = 'venue.core'
    head = header(
      kwargs=['from_id', 'start_time', 'end_time', 'limit'],
      types={'start_time': 'TimestampMillis', 'end_time': 'TimestampMillis'},
    )
    imports = generator.paged_imports(paged_endpoint(USER_TRADES, size_default=2), header=head)
    assert imports['truewire_core.types'] == {'timestamp_millis'}
    assert imports['datetime'] == {'datetime'}
