"""
Pin the shape of the Python source the shared emitters produce.

Every generated client inherits these emitters verbatim, so a defect here is a defect in
eleven packages at once. `.agents/rules/python.md` is the standard the emitted code must
meet, and the repo publishes its API docs with mkdocstrings, so a docstring that Griffe
cannot parse is a functional defect rather than a cosmetic one.
"""
import pytest

from truewire.generation.python.code import Client, Docstring, Function, HttpRequest
from truewire.generation.python.util import body_param_name
from truewire.generation.util import indent
from truewire.generation.schema import MediaType, Operation, Reference, RequestBody, Schema
from truewire.generation.types import RenderedTypes
from truewire.generation.openapi import BODY_KEY

def docstring(**kwargs) -> str:
  """Render a docstring from its parts."""
  return Docstring(**kwargs).code()

class TestDocstringSections:
  """Docstrings must use Google sections, which Griffe/mkdocstrings can parse."""

  def test_description_only(self):
    assert docstring(description='Get an order book snapshot.') == (
      '"""Get an order book snapshot.\n"""'
    )

  def test_params_render_an_args_section(self):
    out = docstring(
      description='Get an order book snapshot.',
      params=[
        Docstring.Param(name='symbol', docstring='Symbol name.'),
        Docstring.Param(name='limit', docstring='Number of levels per side.'),
      ],
    )
    assert out == (
      '"""Get an order book snapshot.\n'
      '\n'
      'Args:\n'
      '  symbol: Symbol name.\n'
      '  limit: Number of levels per side.\n'
      '"""'
    )

  def test_no_bullet_list_is_emitted(self):
    out = docstring(params=[Docstring.Param(name='symbol', docstring='Symbol name.')])
    assert '- `symbol`' not in out

  def test_docs_url_renders_a_references_section(self):
    out = docstring(description='Get the server time.', docs_url='https://docs.example/time')
    assert out == (
      '"""Get the server time.\n'
      '\n'
      'References:\n'
      '  - [Official docs](https://docs.example/time)\n'
      '"""'
    )

  def test_docs_label_is_configurable(self):
    out = docstring(docs_url='https://docs.example/time', docs_label='Bybit API docs')
    assert '  - [Bybit API docs](https://docs.example/time)' in out

  def test_param_without_description_has_no_trailing_whitespace(self):
    out = docstring(params=[Docstring.Param(name='symbol')])
    assert '  symbol:\n' in out
    assert not any(line != line.rstrip() for line in out.splitlines())

  def test_empty_docstring_renders_nothing(self):
    assert docstring() == ''

class TestDocstringSubclassing:
  """`parse` must build the class it was called on, so subclasses need no `vars()` dance."""

  def test_parse_returns_the_subclass(self):
    from truewire.generation.schema import Operation
    from truewire.generation.types import RenderedTypes

    class Sub(Docstring):
      """A subclass that changes nothing."""

    op = Operation.model_validate({'summary': 'Get the server time.', 'responses': {}})
    parsed = Sub.parse(op, RenderedTypes(generation_order=[]))
    assert isinstance(parsed, Sub)

class TestClientDocstring:
  """`python.md` requires a docstring on every class, including generated ones."""

  def test_class_docstring_is_emitted(self):
    code = Client(
      class_name='Orderbook',
      mixin='Endpoint',
      docs='`Get Orderbook` — mixed into the router that owns `market.orderbook`.',
      header=Function(name='orderbook', asyn=True, method=True),
      docstring=Docstring(),
      request=None,  # type: ignore[arg-type]
    ).class_header()
    assert code == (
      'class Orderbook(Endpoint):\n'
      '  """`Get Orderbook` — mixed into the router that owns `market.orderbook`."""'
    )

  def test_class_without_docs_is_unchanged(self):
    code = Client(
      class_name='Orderbook',
      mixin='Endpoint',
      header=Function(name='orderbook', asyn=True, method=True),
      docstring=Docstring(),
      request=None,  # type: ignore[arg-type]
    ).class_header()
    assert code == 'class Orderbook(Endpoint):'

class TestIndent:
  """Indenting a blank line leaves trailing whitespace on it."""

  def test_blank_lines_are_not_indented(self):
    assert indent('a\n\nb') == '  a\n\n  b'

  def test_whitespace_only_lines_are_cleared(self):
    assert indent('a\n  \nb') == '  a\n\n  b'

class TestFunctionHeader:
  """`python.md` specifies grouped multi-line headers, not one parameter per line."""

  def test_short_header_stays_on_one_line(self):
    header = Function(
      name='time', asyn=True, method=True, return_type='ServerTime',
      kwargs=[Function.Param(name='validate', type='bool', required=False)],
    )
    assert header.code() == 'async def time(self, *, validate: bool | None = None) -> ServerTime:'

  def test_long_header_groups_args_and_kwargs(self):
    header = Function(
      name='orderbook', asyn=True, method=True, return_type='Orderbook',
      args=[Function.Param(name='category', type="Literal['spot', 'linear', 'inverse', 'option']")],
      kwargs=[
        Function.Param(name='symbol', type='str'),
        Function.Param(name='limit', type='int', required=False),
        Function.Param(name='validate', type='bool', required=False),
      ],
    )
    assert header.code() == (
      'async def orderbook(\n'
      "  self, category: Literal['spot', 'linear', 'inverse', 'option'], *,\n"
      '  symbol: str, limit: int | None = None, validate: bool | None = None,\n'
      ') -> Orderbook:'
    )

  def test_long_header_without_args_omits_the_positional_group(self):
    header = Function(
      name='instruments', asyn=True, method=True, return_type='Instruments',
      kwargs=[
        Function.Param(name='category', type="Literal['spot', 'linear', 'inverse', 'option']"),
        Function.Param(name='symbol', type='str', required=False),
        Function.Param(name='status', type='str', required=False),
      ],
    )
    assert header.code() == (
      'async def instruments(\n'
      '  self, *,\n'
      "  category: Literal['spot', 'linear', 'inverse', 'option'],\n"
      '  symbol: str | None = None, status: str | None = None,\n'
      ') -> Instruments:'
    )

  def test_no_line_of_a_header_exceeds_the_limit(self):
    header = Function(
      name='instruments', asyn=True, method=True, return_type='Instruments',
      kwargs=[Function.Param(name=f'parameter_{i}', type='str') for i in range(12)],
    )
    assert all(len(line) <= 80 for line in header.code().splitlines())

  def test_no_line_of_a_header_has_trailing_whitespace(self):
    header = Function(
      name='orderbook', asyn=True, method=True, return_type='Orderbook',
      kwargs=[Function.Param(name=f'p{i}', type='str') for i in range(12)],
    )
    assert not any(line != line.rstrip() for line in header.code().splitlines())


class TestFunctionParamCode:
  """`Param.code()` unconditionally appended `| None` to an optional parameter's own
  type, even when the resolved type was already nullable on its own -- doubling up to
  `X | None | None`. Confirmed real, not hypothetical: kraken's real spec has 31
  occurrences of exactly this shape (an optional property whose own schema is `anyOf:
  [..., {"type": "null"}]`, e.g. `spot.account.balance`'s `rebase_multiplier:
  Literal['rebased', 'base'] | None`), surfaced once kraken's codegen-mechanization
  migration actually exercised the case -- alchemy's/etherscan's endpoint shapes never
  happened to produce an already-nullable optional type."""

  def test_optional_param_gets_a_single_trailing_none(self):
    param = Function.Param(name='symbol', type='str', required=False)
    assert param.code() == 'symbol: str | None = None'

  def test_optional_param_whose_type_is_already_nullable_is_not_doubled(self):
    param = Function.Param(
      name='rebase_multiplier', type="Literal['rebased', 'base'] | None", required=False,
    )
    assert param.code() == "rebase_multiplier: Literal['rebased', 'base'] | None = None"
    assert '| None | None' not in param.code()

  def test_required_param_with_an_already_nullable_type_is_untouched(self):
    """A *required* field whose value can genuinely be `None` (a nullable, not optional,
    property) keeps its own `| None` unmodified -- `required` alone decides whether this
    method appends anything at all."""
    param = Function.Param(name='broker', type='str | None', required=True)
    assert param.code() == 'broker: str | None'


def test_a_required_timestamp_param_is_dumped():
  request = HttpRequest(method='GET', path='/api/v3/market/candles')
  request.query_params.append(HttpRequest.Param(name='startTime', required=True, type='TimestampMillis'))
  assert request.params_declaration() == (
    "params: dict = {\n"
    "  'startTime': timestamp_millis.dump(start_time),\n"
    "}"
  )


def test_an_optional_timestamp_param_is_dumped_inside_its_guard():
  request = HttpRequest(method='GET', path='/api/v3/market/candles')
  request.query_params.append(HttpRequest.Param(name='endTime', required=False, type='TimestampMillis'))
  assert request.params_declaration() == (
    "params = {}\n"
    "if end_time is not None:\n"
    "  params['endTime'] = timestamp_millis.dump(end_time)"
  )


def test_a_plain_param_is_untouched():
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='limit', required=False, type='int'))
  assert 'timestamp_millis.dump' not in request.params_declaration()


def test_a_required_decimal_param_is_stringified():
  """A `decimal-string`-formatted parameter renders to `Decimal`, which `httpx` can't
  encode on its own -- `str(...)` reproduces the exact wire value, no per-format helper
  needed (unlike a timestamp)."""
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='amount', required=True, type='Decimal'))
  assert request.params_declaration() == (
    "params: dict = {\n"
    "  'amount': str(amount),\n"
    "}"
  )


def test_an_optional_decimal_param_is_stringified_inside_its_guard():
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='price', required=False, type='Decimal'))
  assert request.params_declaration() == (
    "params = {}\n"
    "if price is not None:\n"
    "  params['price'] = str(price)"
  )


def test_timestamp_params_reports_every_location():
  """Two different timestamp shapes on the same request both count -- Bit2Me's real
  shape once Task 15 lands."""
  request = HttpRequest(method='GET', path='/x/{at}')
  request.path_params.append(HttpRequest.Param(name='at', required=True, type='TimestampMillis'))
  request.query_params.append(HttpRequest.Param(name='startTime', required=True, type='TimestampIso'))
  request.query_params.append(HttpRequest.Param(name='limit', required=False, type='int'))
  assert [p.name for p in request.timestamp_params] == ['at', 'startTime']


def test_a_request_with_an_epoch_param_asks_for_the_helper_it_emits():
  """The emitted `timestamp_millis.dump(...)` names a helper no import statement supplies."""
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='startTime', required=True, type='TimestampMillis'))
  assert request.helpers == frozenset({'timestamp_millis'})


def test_a_request_with_two_timestamp_shapes_asks_for_both_helpers():
  """One request needing both an epoch and an ISO parameter -- the request builder must
  not collapse two different formats onto one helper name."""
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='startTime', required=True, type='TimestampMillis'))
  request.query_params.append(HttpRequest.Param(name='until', required=True, type='TimestampIso'))
  assert request.helpers == frozenset({'timestamp_millis', 'timestamp_iso'})


def test_a_nanos_timestamp_param_asks_for_the_timestamp_nanos_helper():
  """Added after a deribit review found genuine epoch-nanosecond fields."""
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='startTime', required=True, type='TimestampNanos'))
  assert request.helpers == frozenset({'timestamp_nanos'})
  assert request.params_declaration() == (
    "params: dict = {\n"
    "  'startTime': timestamp_nanos.dump(start_time),\n"
    "}"
  )


def test_a_date_param_asks_for_the_date_iso_helper():
  """Added after a deribit review found `market_data.get_delivery_prices.date`, a genuine
  plain calendar date with no time component."""
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='date', required=True, type='DateIso'))
  assert request.helpers == frozenset({'date_iso'})
  assert request.params_declaration() == (
    "params: dict = {\n"
    "  'date': date_iso.dump(date),\n"
    "}"
  )


def test_a_request_without_an_epoch_param_needs_no_helper():
  request = HttpRequest(method='GET', path='/x')
  request.query_params.append(HttpRequest.Param(name='limit', required=False, type='int'))
  assert request.helpers == frozenset()


def test_a_parameter_shadowing_the_helper_is_refused():
  """A parameter whose own identifier collides with its render id's specific helper name
  still can't be dumped -- `timestamp_millis.dump(timestamp_millis)` would read the
  caller's own `datetime` and has no `.dump()`. Format-specific helper names make a real
  collision less likely than the old single `timestamp` name did (etherscan's
  `getblocknobytime` `timestamp` parameter, the original motivating case, no longer
  collides now that its helper would be `timestamp_seconds`) -- but the guard still has
  to hold for whichever name a given format picks. Failing at generation time is the
  point: the alternative ships an `AttributeError` no gate before a mock-backed test sees.
  """
  request = HttpRequest(method='GET', path='/v2/api')
  request.query_params.append(HttpRequest.Param(name='timestamp_millis', required=True, type='TimestampMillis'))
  with pytest.raises(ValueError, match='shadows'):
    request.params_declaration()


def test_a_parameter_shadowing_the_helper_is_refused_from_another_parameter():
  """The collision is with any parameter of the request, not only the epoch one."""
  request = HttpRequest(method='GET', path='/v2/api')
  request.query_params.append(HttpRequest.Param(name='startTime', required=True, type='TimestampMillis'))
  request.query_params.append(HttpRequest.Param(name='timestamp_millis', required=False, type='str'))
  with pytest.raises(ValueError, match='shadows'):
    request.params_declaration()


def test_a_body_with_no_timestamp_properties_needs_no_conversion():
  """The common case (every current caller, until a backend starts passing `body_schema`
  into `parse()`) is unaffected: the wire name is just the caller's own body identifier,
  and there's nothing to emit ahead of it."""
  request = HttpRequest(method='POST', path='/x')
  request.body = HttpRequest.Param(name='order', required=True, type='OrderRequest')
  assert request.body_wire_name == 'order'
  assert request.body_conversion_lines() == ''
  assert request.helpers == frozenset()


def test_a_request_without_a_body_has_no_wire_name():
  request = HttpRequest(method='GET', path='/x')
  assert request.body_wire_name is None
  assert request.body_conversion_lines() == ''


def test_a_parameter_named_body_wire_is_refused():
  """The same shadowing concern `test_a_parameter_shadowing_the_helper_is_refused` guards
  for `TIMESTAMP_HELPERS`, but against `BODY_WIRE_NAME` -- a query parameter literally
  named `body_wire` would collide with the fixed local `body_conversion_lines` declares."""
  request = HttpRequest(method='POST', path='/x')
  request.query_params.append(HttpRequest.Param(name='body_wire', required=True, type='str'))
  request.body = HttpRequest.Param(name='order', required=True, type='OrderRequest')
  request.body_decimal_props = ['qty']
  with pytest.raises(ValueError, match='body_wire'):
    request.body_wire_name


def test_a_body_timestamp_property_is_converted_before_it_reaches_the_wire():
  """kucoin's `broker.mark_up_set` was found live-broken this way: a request-body field
  typed `TimestampMillis` reached `json=...` as a real `datetime`, and nothing converted
  it before `httpx` tried to JSON-encode it. `body_wire_name` is a fixed local
  (`BODY_WIRE_NAME`), not derived from the caller's own (possibly long) body parameter
  name, so the conversion is a `dict` copy rather than a mutation of a `NotRequired`-keyed
  `TypedDict` in place (see `body_conversion_lines`'s own docstring for why a single
  conditional expression can't do this under pyright)."""
  request = HttpRequest(method='POST', path='/x')
  request.body = HttpRequest.Param(name='mark_up_fee_set_request', required=True, type='MarkUpFeeSetRequest')
  request.body_timestamp_props = {'effectAt': 'TimestampMillis'}
  assert request.body_wire_name == 'body_wire'
  assert request.body_conversion_lines() == (
    "body_wire: dict = dict(mark_up_fee_set_request)\n"
    "if body_wire.get('effectAt') is not None:\n"
    "  body_wire['effectAt'] = "
    "timestamp_millis.dump(body_wire['effectAt'])"
  )
  assert request.helpers == frozenset({'timestamp_millis'})


def test_two_body_timestamp_properties_each_get_their_own_conversion_line():
  """kucoin's `broker.kyc_submit`: `birthDate`/`expireDate`, both `DateIso`."""
  request = HttpRequest(method='POST', path='/x')
  request.body = HttpRequest.Param(name='kyc_submit_request', required=True, type='KycSubmitRequest')
  request.body_timestamp_props = {'birthDate': 'DateIso', 'expireDate': 'DateIso'}
  lines = request.body_conversion_lines()
  assert lines.startswith('body_wire: dict = dict(kyc_submit_request)\n')
  assert (
    "if body_wire.get('birthDate') is not None:\n"
    "  body_wire['birthDate'] = "
    "date_iso.dump(body_wire['birthDate'])"
  ) in lines
  assert (
    "if body_wire.get('expireDate') is not None:\n"
    "  body_wire['expireDate'] = "
    "date_iso.dump(body_wire['expireDate'])"
  ) in lines
  assert request.helpers == frozenset({'date_iso'})


def test_request_call_does_not_apply_body_timestamp_props():
  """`request_call`'s own contract is a single expression with nowhere to prepend
  `body_conversion_lines`'s declaration statement -- it intentionally still emits the raw
  body identifier even when `body_timestamp_props` is set, documented on its own
  docstring. A backend that needs the conversion has to build its own call (kucoin's
  `rpc_endpoint` is the reference), not call `request_call`."""
  request = HttpRequest(method='POST', path='/x')
  request.body = HttpRequest.Param(name='order', required=True, type='OrderRequest')
  request.body_timestamp_props = {'effectAt': 'TimestampMillis'}
  assert 'json=order' in request.request_call()
  assert 'body_wire' not in request.request_call()


def _op_with_body() -> Operation:
  """A minimal operation carrying just enough of a `requestBody` for `HttpRequest.parse`
  to see it as present -- `parse` reads the body's *name* from `types.identifiers`/
  `body_schema` (see `body_param_name`) and its *shape* from `body_schema`, so
  `op.request_body`'s own schema is never inspected."""
  return Operation(requestBody=RequestBody(content={'application/json': MediaType()}))


class TestBodyParamName:
  """`body_param_name` is the single place `Function.parse`, `HttpRequest.parse`, and
  `Docstring.parse` all derive a request body's generated parameter name from -- kept in
  one function specifically so the three can never independently disagree on it (they
  used to each duplicate the same `type := ... else 'body'` logic inline)."""

  def test_an_untitled_body_with_no_rendered_type_is_generic(self):
    assert body_param_name(None) == 'body'

  def test_a_plain_object_type_is_snake_cased(self):
    assert body_param_name('TransferRequest') == 'transfer_request'

  def test_an_untitled_union_type_falls_back_to_generic_rather_than_concatenating(self):
    """The bug this exists to fix: bitget's `uta.trade.order.place` renders its body as
    `LimitOrderRequest | MarketOrderRequest` (an `anyOf`-shaped, `docs/spec/authoring.md`
    rule-0 body) with no title of its own on the wrapper schema. Blindly snake-casing that
    whole union expression concatenates both variant names into one identifier
    (`limit_order_request_market_order_request`) -- `'body'` reads far better for
    something that's ambiguous by construction anyway."""
    assert body_param_name('LimitOrderRequest | MarketOrderRequest') == 'body'

  def test_a_titled_union_body_is_named_after_its_own_title_not_its_variants(self):
    """When a spec author *does* title the `anyOf` wrapper itself (distinct from titling
    each variant, which `LimitOrderRequest`/`MarketOrderRequest` already are), that title
    wins over both the union-string fallback and the untitled generic one."""
    body_schema = Schema(title='OrderRequest', anyOf=[
      Schema(title='LimitOrderRequest', type='object', properties={}),
      Schema(title='MarketOrderRequest', type='object', properties={}),
    ])
    assert body_param_name('LimitOrderRequest | MarketOrderRequest', body_schema) == 'order_request'

  def test_a_titled_plain_object_bodys_title_is_unaffected(self):
    """The common case: a plain object's own title already equals its rendered type, so
    preferring `body_schema.title` changes nothing here."""
    body_schema = Schema(title='TransferRequest', type='object', properties={'amount': Schema(type='string')})
    assert body_param_name('TransferRequest', body_schema) == 'transfer_request'

  def test_a_bare_reference_body_schema_is_not_a_title_source(self):
    """A `$ref`'d (not inlined) body has no `.title` of its own to read -- falls through
    to the rendered-type logic exactly as if no `body_schema` were passed at all."""
    assert body_param_name('LimitOrderRequest | MarketOrderRequest', Reference(ref='SharedOrder')) == 'body'

  def _union_op_and_types(self) -> tuple[Operation, RenderedTypes]:
    op = Operation(requestBody=RequestBody(content={'application/json': MediaType()}))
    types = RenderedTypes(
      identifiers={BODY_KEY: 'LimitOrderRequest | MarketOrderRequest'}, generation_order=[],
    )
    return op, types

  def _titled_union_body_schema(self) -> Schema:
    return Schema(title='OrderRequest', anyOf=[
      Schema(title='LimitOrderRequest', type='object', properties={}),
      Schema(title='MarketOrderRequest', type='object', properties={}),
    ])

  def test_function_parse_uses_the_titled_unions_name(self):
    """Confirms `Function.parse` -- the actual method *signature* -- goes through
    `body_param_name` too, not just `HttpRequest.parse`'s internal wire-conversion path."""
    op, types = self._union_op_and_types()
    header = Function.parse(
      op, types, name='place', asyn=True, method=True, body_schema=self._titled_union_body_schema(),
    )
    assert header.args[-1].name == 'order_request'

  def test_function_parse_falls_back_to_body_for_an_untitled_union(self):
    op, types = self._union_op_and_types()
    header = Function.parse(op, types, name='place', asyn=True, method=True)
    assert header.args[-1].name == 'body'

  def test_docstring_parse_names_the_args_entry_the_same_way(self):
    """The `Args:` entry has to name the same parameter the signature does, or the
    rendered docstring lies about its own method -- this is why `Docstring.parse` takes
    `body_schema` too, rather than re-deriving a name from `types` alone."""
    op, types = self._union_op_and_types()
    docstring = Docstring.parse(op, types, body_schema=self._titled_union_body_schema())
    assert docstring.params[-1].name == 'order_request'


def test_a_body_decimal_property_is_converted_before_it_reaches_the_wire():
  """bitget's `uta.transfers.transfer`: a request-body field typed `decimal-string`
  reaches `json=...` as a real `Decimal`, and nothing converted it before `httpx` tried to
  JSON-encode it -- the same failure mode `body_timestamp_props` already fixes for
  `datetime`, but needing no helper: a `Decimal`'s wire form is always its own plain
  string, so this is a bare `str(...)` call rather than a converter lookup."""
  request = HttpRequest(method='POST', path='/x')
  request.body = HttpRequest.Param(name='transfer_request', required=True, type='TransferRequest')
  request.body_decimal_props = ['amount']
  assert request.body_wire_name == 'body_wire'
  assert request.body_conversion_lines() == (
    "body_wire: dict = dict(transfer_request)\n"
    "if body_wire.get('amount') is not None:\n"
    "  body_wire['amount'] = str(body_wire['amount'])"
  )
  assert request.helpers == frozenset()


def test_timestamp_and_decimal_body_props_share_one_wire_copy():
  request = HttpRequest(method='POST', path='/x')
  request.body = HttpRequest.Param(name='order', required=True, type='OrderRequest')
  request.body_timestamp_props = {'goodTillTime': 'TimestampMillis'}
  request.body_decimal_props = ['qty']
  lines = request.body_conversion_lines()
  assert lines.startswith('body_wire: dict = dict(order)\n')
  assert "body_wire['goodTillTime'] = timestamp_millis.dump(body_wire['goodTillTime'])" in lines
  assert "body_wire['qty'] = str(body_wire['qty'])" in lines
  assert request.helpers == frozenset({'timestamp_millis'})


def test_parse_collects_decimal_props_from_a_plain_object_body():
  body_schema = Schema(
    title='TransferRequest', type='object',
    properties={
      'coin': Schema(type='string'),
      'amount': Schema(type='string', format='decimal-string'),
    },
  )
  request = HttpRequest.parse(
    _op_with_body(), RenderedTypes(generation_order=[]),
    method='POST', path='/x', body_schema=body_schema,
  )
  assert request.body_decimal_props == ['amount']
  assert not request.body_is_array


def test_parse_unions_props_across_anyof_variants():
  """bitget's `uta.trade.order.place`: an `anyOf`-shaped discriminated union
  (`LimitOrderRequest | MarketOrderRequest`) has no top-level `properties` of its own, so
  each inline variant is walked instead. `price` only exists on the limit variant, but the
  generated `if wire.get('price') is not None:` guard already handles a market-order call
  never having that key -- no variant-specific branching is needed in the emitted code."""
  body_schema = Schema(anyOf=[
    Schema(
      title='LimitOrderRequest', type='object',
      properties={
        'qty': Schema(type='string', format='decimal-string'),
        'price': Schema(type='string', format='decimal-string'),
      },
    ),
    Schema(
      title='MarketOrderRequest', type='object',
      properties={'qty': Schema(type='string', format='decimal-string')},
    ),
  ])
  request = HttpRequest.parse(
    _op_with_body(), RenderedTypes(generation_order=[]),
    method='POST', path='/x', body_schema=body_schema,
  )
  assert sorted(request.body_decimal_props) == ['price', 'qty']


def test_parse_drops_a_property_declared_with_conflicting_formats_across_variants():
  """Two variants disagreeing on one property's format is a spec inconsistency, not
  something to guess through -- the property is dropped from both collections rather than
  converted according to whichever variant happened to be walked last."""
  body_schema = Schema(anyOf=[
    Schema(
      title='VariantA', type='object',
      properties={'value': Schema(type='string', format='decimal-string')},
    ),
    Schema(
      title='VariantB', type='object',
      properties={'value': Schema(type='string', format='epoch-millis')},
    ),
  ])
  request = HttpRequest.parse(
    _op_with_body(), RenderedTypes(generation_order=[]),
    method='POST', path='/x', body_schema=body_schema,
  )
  assert request.body_decimal_props == []
  assert dict(request.body_timestamp_props) == {}


def test_parse_skips_a_reference_anyof_variant():
  """A variant that's a bare `$ref` (not inlined) is left alone rather than guessed at --
  resolving it would need a `Resolver` `parse` doesn't have."""
  body_schema = Schema(anyOf=[
    Reference(ref='SharedOrderVariant'),
    Schema(
      title='MarketOrderRequest', type='object',
      properties={'qty': Schema(type='string', format='decimal-string')},
    ),
  ])
  request = HttpRequest.parse(
    _op_with_body(), RenderedTypes(generation_order=[]),
    method='POST', path='/x', body_schema=body_schema,
  )
  assert request.body_decimal_props == ['qty']


def test_parse_collects_props_from_an_array_bodys_items():
  """bitget's `uta.trade.order.place_batch`: the body is `type: 'array'`, one row per
  order, and each row is itself the same `anyOf`-shaped union as the single-order
  endpoint."""
  item_schema = Schema(anyOf=[
    Schema(
      title='BatchLimitOrderItem', type='object',
      properties={'qty': Schema(type='string', format='decimal-string')},
    ),
    Schema(
      title='BatchMarketOrderItem', type='object',
      properties={'qty': Schema(type='string', format='decimal-string')},
    ),
  ])
  body_schema = Schema(title='PlaceBatchOrdersRequest', type='array', items=item_schema)
  request = HttpRequest.parse(
    _op_with_body(), RenderedTypes(generation_order=[]),
    method='POST', path='/x', body_schema=body_schema,
  )
  assert request.body_is_array
  assert request.body_decimal_props == ['qty']
  assert request.body_wire_name == 'body_wire'
  assert request.body_conversion_lines() == (
    "body_wire: list = [dict(item) for item in place_batch_orders_request]\n"
    "for item in body_wire:\n"
    "  if item.get('qty') is not None:\n"
    "    item['qty'] = str(item['qty'])"
  )


def test_parse_collects_props_from_a_nested_array_property():
  """bitget's `classic.mix.order.batch_place`: unlike `uta.trade.order.place_batch`, the
  per-order array isn't the body itself -- it's `orderList`, a property alongside
  `symbol`/`productType` on an enclosing plain-object body."""
  item_schema = Schema(anyOf=[
    Schema(
      title='MixBatchLimitOrderItem', type='object',
      properties={'size': Schema(type='string', format='decimal-string')},
    ),
    Schema(
      title='MixBatchMarketOrderItem', type='object',
      properties={'size': Schema(type='string', format='decimal-string')},
    ),
  ])
  body_schema = Schema(
    title='MixBatchPlaceOrderRequest', type='object',
    properties={
      'symbol': Schema(type='string'),
      'orderList': Schema(type='array', items=item_schema),
    },
  )
  request = HttpRequest.parse(
    _op_with_body(), RenderedTypes(generation_order=[]),
    method='POST', path='/x', body_schema=body_schema,
  )
  assert not request.body_is_array
  assert request.body_decimal_props == []
  assert request.body_array_props == [('orderList', {}, ['size'])]
  assert request.body_conversion_lines() == (
    "body_wire: dict = dict(mix_batch_place_order_request)\n"
    "if body_wire.get('orderList') is not None:\n"
    "  body_wire['orderList'] = [dict(item) for item in body_wire['orderList']]\n"
    "  for item in body_wire['orderList']:\n"
    "    if item.get('size') is not None:\n"
    "      item['size'] = str(item['size'])"
  )


class TestSelfShadowingReturnType:
  """A method named after a builtin it returns as a generic self-shadows under Python
  3.14's lazy annotation evaluation (PEP 649): a class method's annotations resolve
  against a namespace that includes the class body's own attributes, so `list` inside
  `list`'s own return annotation resolves to the method, not the builtin, and
  `inspect.signature()` raises evaluating it. Kucoin's `account.sub_account_api.list`,
  a method literally named `list` returning `list[SubAccountApiKey] | None`, is the real
  case (`.agents/handoff/codegen-builtin-name-collision.md`).

  `Function.parse` routes such a return type through a module-level alias instead of
  emitting the builtin generic inline, so the class body never binds a name its own
  return annotation depends on.
  """

  def _rendered(self, return_type: str) -> RenderedTypes:
    return RenderedTypes(identifiers={'$response/response200': return_type}, generation_order=[])

  def _op(self) -> Operation:
    return Operation.model_validate({'responses': {'200': {'description': 'ok'}}})

  def test_colliding_list_method_returns_an_alias_name(self):
    header = Function.parse(
      self._op(), self._rendered('list[SubAccountApiKey] | None'),
      name='list', asyn=True, method=True,
    )
    assert header.return_type == 'ListResponse'

  def test_colliding_list_method_defines_the_alias_at_module_scope(self):
    header = Function.parse(
      self._op(), self._rendered('list[SubAccountApiKey] | None'),
      name='list', asyn=True, method=True,
    )
    assert header.return_type_alias == 'ListResponse = list[SubAccountApiKey] | None'

  def test_colliding_dict_method_returns_an_alias_name(self):
    header = Function.parse(
      self._op(), self._rendered('dict[str, int]'),
      name='dict', asyn=True, method=True,
    )
    assert header.return_type == 'DictResponse'
    assert header.return_type_alias == 'DictResponse = dict[str, int]'

  def test_non_colliding_method_name_is_unaffected(self):
    header = Function.parse(
      self._op(), self._rendered('list[SubAccountApiKey] | None'),
      name='sub_account_api_keys', asyn=True, method=True,
    )
    assert header.return_type == 'list[SubAccountApiKey] | None'
    assert header.return_type_alias is None

  def test_a_colliding_method_with_parameters_still_gets_aliased(self):
    """`parse` walks `op.parameters` before it reaches the return type, reusing a
    loop-local also named `name` -- so the alias must key off the method's own name
    (`header.name`), not that reused local, or a real endpoint with parameters (like
    kucoin's `sub_name`/`api_key`) would never trigger it."""
    op = Operation.model_validate({
      'parameters': [
        {'name': 'subName', 'in': 'query', 'required': True, 'schema': {'$ref': '#/param/subName'}},
      ],
      'responses': {'200': {'description': 'ok'}},
    })
    types = RenderedTypes(
      identifiers={
        '#/param/subName': 'str',
        '$response/response200': 'list[SubAccountApiKey] | None',
      },
      generation_order=[],
    )
    header = Function.parse(op, types, name='list', asyn=True, method=True)
    assert header.return_type == 'ListResponse'
    assert header.return_type_alias == 'ListResponse = list[SubAccountApiKey] | None'

  def test_a_name_only_sharing_a_prefix_is_not_a_collision(self):
    """`listing` is not the builtin `list` -- no alias, even though it starts with it."""
    header = Function.parse(
      self._op(), self._rendered('list[SubAccountApiKey] | None'),
      name='listing', asyn=True, method=True,
    )
    assert header.return_type == 'list[SubAccountApiKey] | None'
    assert header.return_type_alias is None


def test_a_renamed_parameter_clears_the_shadow():
  """`identifier` is the documented hook, so renaming through it must be enough."""
  request = HttpRequest(
    method='GET', path='/v2/api',
    identifier=lambda name: 'at' if name == 'timestamp' else name,
  )
  request.query_params.append(HttpRequest.Param(name='timestamp', required=True, type='TimestampMillis'))
  assert request.params_declaration() == (
    "params: dict = {\n"
    "  'timestamp': timestamp_millis.dump(at),\n"
    "}"
  )
