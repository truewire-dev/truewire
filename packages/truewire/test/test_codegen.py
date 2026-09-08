"""
Pin the shared codegen contract that keeps an endpoint class from shadowing its
response type, and the WebSocket half of that contract.

The CLI derives an endpoint class name from the last segment of `endpoint.function`
(`market.orderbook` -> `Orderbook`) while `truewire.generation` names the response
TypedDict from the schema's `title` — also `Orderbook`, correctly, since it is the same
concept. Both land in one module and the later binding wins for a type checker, so the
public type is silently wrong. The class name is mechanical and must yield; the
spec-authored type name must not.

The same reasoning applies to a subscription, whose message schema is titled in the
spec exactly like an HTTP response schema — so `type_names` must see through a
`ws` spec too, or every WebSocket endpoint class silently shadows its message type.
Beyond that, a subscription carries three things HTTP does not: a channel template
interpolated from `in: 'path'` parameters, a `requestBody` that is a bag of
subscription parameters rather than one payload, and two responses with different
cardinality (`reply` once, `message` repeatedly). `Generator.subscription` is the one
place that reads `docs/spec/spec.md` §"WebSocket Subscriptions" so no client re-derives it.

`paged_method` is here for the same reason: a `pagination` block is a spec fact, but the
loop it implies is a convention every client would otherwise reinvent — mexc did, by
sniffing parameter names, and bybit did not, so it shipped none. Its tests execute the
source they generate, because a loop that reads correctly and never terminates is exactly
the defect this replaces.
"""
from typing_extensions import Any, AsyncIterator, cast
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from truewire.generation.python.code import Function, HttpRequest
from truewire.generation.schema import Schema
from truewire_core import PaginatedResponse
from truewire_core.exceptions import LogicError

from truewire.codegen.python import Generator, endpoint_transport, subtree_transport
from truewire.spec import Endpoint

def endpoint(function: str, response: dict[str, Any], *, title: str | None = None) -> Endpoint:
  """Build a one-response HTTP endpoint record around a raw response schema."""
  schema = dict(response)
  if title is not None:
    schema['title'] = title
  return Endpoint.model_validate({
    'function': function,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/v5/market/orderbook',
      'method': 'GET',
      'openapi': {
        'summary': 'Get Orderbook',
        'responses': {
          '200': {
            'description': 'Success',
            'content': {'application/json': {'schema': schema}},
          },
        },
      },
    },
  })

RECORD = {'type': 'object', 'required': ['s'], 'properties': {'s': {'type': 'string'}}}

@pytest.fixture
def generator() -> Generator:
  """The shared base generator, with no client-specific behaviour."""
  return Generator()

class TestTypeNames:
  """The base generator must be able to say which names a module will define."""

  def test_titled_response_name_is_reported(self, generator: Generator):
    ep = endpoint('market.orderbook', RECORD, title='Orderbook')
    assert 'Orderbook' in generator.type_names(ep, {})

  def test_unrelated_name_is_not_reported(self, generator: Generator):
    ep = endpoint('market.orderbook', RECORD, title='Orderbook')
    assert 'ServerTime' not in generator.type_names(ep, {})

  def test_external_reference_name_is_reported(self, generator: Generator):
    ep = endpoint('market.orderbook', {
      'type': 'object',
      'required': ['b'],
      'properties': {'b': {'$ref': 'OrderbookLevel'}},
      'title': 'Orderbook',
    })
    names = generator.type_names(ep, {
      'OrderbookLevel': {'package': 'bybit.types', 'name': 'OrderbookLevel'},
    })
    assert 'OrderbookLevel' in names

class TestClassName:
  """The endpoint class name is derived, so it yields to the spec-authored type name."""

  def test_colliding_name_is_suffixed(self, generator: Generator):
    ep = endpoint('market.orderbook', RECORD, title='Orderbook')
    assert generator.class_name(ep, {}, name='Orderbook') == 'OrderbookEndpoint'

  def test_free_name_is_left_alone(self, generator: Generator):
    ep = endpoint('market.time', RECORD, title='ServerTime')
    assert generator.class_name(ep, {}, name='Time') == 'Time'

  def test_suffixed_name_that_also_collides_is_suffixed_again(self, generator: Generator):
    ep = endpoint('market.orderbook', {
      'title': 'Orderbook',
      'type': 'object',
      'required': ['inner'],
      'properties': {'inner': {
        'title': 'OrderbookEndpoint',
        'type': 'object',
        'required': ['x'],
        'properties': {'x': {'type': 'string'}},
      }},
    })
    assert generator.class_name(ep, {}, name='Orderbook') == 'OrderbookEndpointEndpoint'

def new_shape_rpc_endpoint(
  function: str, *, request: dict[str, Any] | None, response: dict[str, Any] | None,
) -> Endpoint:
  """Build a new-shape (design §7) `request`/`response` HTTP endpoint record."""
  return Endpoint.model_validate({
    'function': function,
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/v1/orderbook', 'method': 'GET',
      'request': request, 'response': response,
    },
  })

def new_shape_stream_endpoint(
  function: str, *, channel: str,
  parameters: dict[str, Any] | None, payload: dict[str, Any] | None,
) -> Endpoint:
  """Build a new-shape (design §8) `parameters`/`payload` stream endpoint record."""
  return Endpoint.model_validate({
    'function': function,
    'meta': {'public': True},
    'spec': {'kind': 'stream', 'channel': channel, 'parameters': parameters, 'payload': payload},
  })

class TestEndpointSchemasNewShape:
  """`endpoint_schemas`'s new-shape branch (Task 20's fix #2) must mirror
  `rpc_endpoint`/`stream_endpoint`'s own title-stripping on `$request`/`$parameters` --
  both of those methods force the type they actually render to the fixed name
  `Request`/`Parameters` regardless of the schema's own `title`
  (`request_schema.model_copy(update={'title': None})`), so `type_names`/`class_name`
  (run by the CLI *before* either method is called, to avoid a class-name collision) have
  to see that exact same fixed-name shape. An earlier version of this branch validated
  the schema as-is, title intact -- `type_names` then reported the schema's own title
  (`OrderbookRequest`) as an occupied name nothing in the generated module actually uses,
  while `Request`, the name genuinely used, went unreported. `$response`/`$payload` keep
  their own title, matching `rpc_endpoint`/`stream_endpoint`'s identical treatment of
  `response`/`payload` -- only `$request`/`$parameters` are forced to a fixed name.
  """

  def test_request_title_is_stripped_from_endpoint_schemas(self, generator: Generator):
    ep = new_shape_rpc_endpoint(
      'market.orderbook',
      request={'title': 'OrderbookRequest', 'type': 'object', 'required': ['symbol'],
                'properties': {'symbol': {'type': 'string', 'description': 'Pair.'}}},
      response={'title': 'Orderbook', 'type': 'object', 'required': ['bids'],
                 'properties': {'bids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Bids.'}}},
    )
    schemas = generator.endpoint_schemas(ep)
    assert schemas['$request'].title is None
    assert schemas['$response'].title == 'Orderbook'

  def test_request_title_is_not_reported_by_type_names(self, generator: Generator):
    ep = new_shape_rpc_endpoint(
      'market.orderbook',
      request={'title': 'OrderbookRequest', 'type': 'object', 'required': ['symbol'],
                'properties': {'symbol': {'type': 'string', 'description': 'Pair.'}}},
      response={'title': 'Orderbook', 'type': 'object', 'required': ['bids'],
                 'properties': {'bids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Bids.'}}},
    )
    names = generator.type_names(ep, {})
    assert 'OrderbookRequest' not in names
    assert 'Request' in names
    assert 'Orderbook' in names

  def test_parameters_title_is_stripped_from_endpoint_schemas(self, generator: Generator):
    ep = new_shape_stream_endpoint(
      'market.ticker_stream', channel='/ticker/{symbol}',
      parameters={'title': 'TickerParams', 'type': 'object', 'required': ['symbol'],
                  'properties': {'symbol': {'type': 'string', 'description': 'Pair.'}}},
      payload={'title': 'Ticker', 'type': 'object', 'required': ['price'],
               'properties': {'price': {'type': 'string', 'description': 'Price.'}}},
    )
    schemas = generator.endpoint_schemas(ep)
    assert schemas['$parameters'].title is None
    assert schemas['$payload'].title == 'Ticker'

  def test_parameters_title_is_not_reported_by_type_names(self, generator: Generator):
    ep = new_shape_stream_endpoint(
      'market.ticker_stream', channel='/ticker/{symbol}',
      parameters={'title': 'TickerParams', 'type': 'object', 'required': ['symbol'],
                  'properties': {'symbol': {'type': 'string', 'description': 'Pair.'}}},
      payload={'title': 'Ticker', 'type': 'object', 'required': ['price'],
               'properties': {'price': {'type': 'string', 'description': 'Price.'}}},
    )
    names = generator.type_names(ep, {})
    assert 'TickerParams' not in names
    assert 'Parameters' in names
    assert 'Ticker' in names

def subscription_endpoint(
  function: str, *,
  channel: str,
  message: dict[str, Any] | None = None,
  reply: dict[str, Any] | None = None,
  body: dict[str, Any] | None = None,
  parameters: list[dict[str, Any]] | None = None,
  message_content_type: str = 'application/json',
) -> Endpoint:
  """Build a WebSocket endpoint record in the adapted-OpenAPI convention."""
  responses: dict[str, Any] = {}
  if reply is not None:
    responses['reply'] = {
      'description': 'Subscription acknowledgement.',
      'content': {'application/json': {'schema': reply}},
    }
  if message is not None:
    responses['message'] = {
      'description': 'Stream message.',
      'content': {message_content_type: {'schema': message}},
    }
  openapi: dict[str, Any] = {'summary': 'Subscribe', 'responses': responses}
  if parameters is not None:
    openapi['parameters'] = parameters
  if body is not None:
    openapi['requestBody'] = {
      'required': True,
      'description': 'Subscription parameters.',
      'content': {'application/json': {'schema': body}},
    }
  spec: dict[str, Any] = (
    {'kind': 'stream', 'channel': channel, 'openapi': openapi}
    if message is not None
    else {'kind': 'rpc', 'transports': ['ws'], 'path': channel, 'openapi': openapi}
  )
  return Endpoint.model_validate({'function': function, 'spec': spec})

DEPTH_MESSAGE = {
  'title': 'Depth',
  'type': 'object',
  'required': ['bids'],
  'properties': {'bids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Bids.'}},
}
"""A titled stream message schema, named the same way an HTTP response schema is."""

SUBSCRIBE_BODY = {
  'title': 'DepthSubscribeRequest',
  'type': 'object',
  'required': ['symbol'],
  'properties': {
    'symbol': {'type': 'string', 'description': 'Contract symbol.'},
    'compress': {'type': 'boolean', 'description': 'Request merged pushes.'},
  },
}
"""A subscription `requestBody`: a bag of parameters, not one payload."""

class TestWsTypeNames:
  """A subscription's titled schemas shadow a class name exactly like an HTTP response's."""

  def test_message_type_name_is_reported(self, generator: Generator):
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    assert 'Depth' in generator.type_names(ep, {})

  def test_reply_type_name_is_reported(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth',
      message=DEPTH_MESSAGE,
      reply={'title': 'DepthReply', 'type': 'object', 'required': ['ok'],
             'properties': {'ok': {'type': 'boolean', 'description': 'Ok.'}}},
    )
    assert 'DepthReply' in generator.type_names(ep, {})

  def test_subscription_parameter_type_names_are_reported(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth',
      message=DEPTH_MESSAGE, body=SUBSCRIBE_BODY,
    )
    assert 'DepthSubscribeRequest' in generator.type_names(ep, {})

  def test_colliding_class_name_is_suffixed(self, generator: Generator):
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    assert generator.class_name(ep, {}, name='Depth') == 'DepthEndpoint'

class TestSubscription:
  """`Generator.subscription` reads the adapted-OpenAPI convention once, for every client."""

  def test_channel_template_is_reported(self, generator: Generator):
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    assert generator.subscription(ep)['channel'] == 'sub.depth'

  def test_path_parameters_become_channel_parameters(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='depth.{instrument}.{interval}',
      message=DEPTH_MESSAGE,
      parameters=[
        {'name': 'interval', 'in': 'path', 'required': True, 'description': 'Interval.',
         'schema': {'type': 'string'}},
        {'name': 'instrument', 'in': 'path', 'required': True, 'description': 'Instrument.',
         'schema': {'type': 'string'}},
      ],
    )
    channel_params = generator.subscription(ep)['channel_parameters']
    assert [param['name'] for param in channel_params] == ['instrument', 'interval']

  def test_path_parameter_absent_from_the_template_is_still_reported(self, generator: Generator):
    """dydx declares `id` and `batched` as `in: 'path'` on a channel named `v4_candles`.

    Its socket takes them as sibling fields of the subscribe frame rather than
    interpolating them, so ordering by the template alone would drop them.
    """
    ep = subscription_endpoint(
      'streams.candles', channel='v4_candles',
      message=DEPTH_MESSAGE,
      parameters=[
        {'name': 'id', 'in': 'path', 'required': True, 'description': 'Market and resolution.',
         'schema': {'type': 'string'}},
        {'name': 'batched', 'in': 'path', 'required': False, 'description': 'Batch updates.',
         'schema': {'type': 'boolean'}},
      ],
    )
    channel_params = generator.subscription(ep)['channel_parameters']
    assert [param['name'] for param in channel_params] == ['id', 'batched']

  def test_channel_parameter_carries_its_schema_id(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='depth.{instrument}',
      message=DEPTH_MESSAGE,
      parameters=[
        {'name': 'instrument', 'in': 'path', 'required': True, 'description': 'Instrument.',
         'schema': {'type': 'string'}},
      ],
    )
    sub = generator.subscription(ep)
    assert sub['channel_parameters'][0]['id'] in sub['schemas']

  def test_request_body_becomes_subscription_parameters(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth',
      message=DEPTH_MESSAGE, body=SUBSCRIBE_BODY,
    )
    params = generator.subscription(ep)['parameters']
    assert [(param['name'], param['required']) for param in params] == [
      ('symbol', True), ('compress', False),
    ]

  def test_subscription_parameter_schemas_are_addressable(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth',
      message=DEPTH_MESSAGE, body=SUBSCRIBE_BODY,
    )
    sub = generator.subscription(ep)
    types = generator.type_generator()(sub['schemas'])
    assert [types.identifiers[param['id']] for param in sub['parameters']] == ['str', 'bool']

  def test_subscription_parameter_keeps_its_description(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth',
      message=DEPTH_MESSAGE, body=SUBSCRIBE_BODY,
    )
    params = generator.subscription(ep)['parameters']
    assert params[0]['description'] == 'Contract symbol.'

  def test_reply_and_message_are_separate_ids(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth',
      message=DEPTH_MESSAGE,
      reply={'title': 'DepthReply', 'type': 'object', 'required': ['ok'],
             'properties': {'ok': {'type': 'boolean', 'description': 'Ok.'}}},
    )
    sub = generator.subscription(ep)
    types = generator.type_generator()(sub['schemas'])
    assert types.identifiers[sub['message']] == 'Depth'
    assert types.identifiers[sub['reply']] == 'DepthReply'

  def test_absent_reply_is_none(self, generator: Generator):
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    assert generator.subscription(ep)['reply'] is None

  def test_json_message_is_not_binary(self, generator: Generator):
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    assert generator.subscription(ep)['binary'] is False

  def test_non_json_message_is_binary(self, generator: Generator):
    ep = subscription_endpoint(
      'streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE,
      message_content_type='application/x-protobuf',
    )
    assert generator.subscription(ep)['binary'] is True

  def test_http_endpoint_is_rejected(self, generator: Generator):
    ep = endpoint('market.orderbook', RECORD, title='Orderbook')
    with pytest.raises(TypeError):
      generator.subscription(ep)

class TestEndpointDispatch:
  """`endpoint` routes by `kind` so a client implements one hook per call pattern."""

  def test_rpc_endpoint_is_dispatched_to_rpc_hook(self):
    class Backend(Generator):
      """A generator that only implements the rpc hook."""
      def rpc_endpoint(self, endpoint, references, *, class_name, method_name) -> str:
        """Record the dispatch."""
        return f'rpc:{endpoint.function}'
    ep = endpoint('market.orderbook', RECORD, title='Orderbook')
    assert Backend().endpoint(ep, {}, class_name='Orderbook', method_name='orderbook') == 'rpc:market.orderbook'

  def test_stream_endpoint_is_dispatched_to_stream_hook(self):
    class Backend(Generator):
      """A generator that only implements the stream hook."""
      def stream_endpoint(self, endpoint, references, *, class_name, method_name) -> str:
        """Record the dispatch."""
        return f'stream:{endpoint.function}'
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    assert Backend().endpoint(ep, {}, class_name='Depth', method_name='depth') == 'stream:streams.market.depth'

  def test_unimplemented_stream_hook_raises(self, generator: Generator):
    ep = subscription_endpoint('streams.market.depth', channel='sub.depth', message=DEPTH_MESSAGE)
    with pytest.raises(NotImplementedError):
      generator.endpoint(ep, {}, class_name='Depth', method_name='depth')

  def test_dual_transport_endpoint_reaches_the_rpc_hook_once(self):
    """
    Dispatch is keyed on `kind`, not transport, so a dual-transport rpc endpoint
    (`transports: ['http', 'ws']` -- a Deribit-style method reachable both ways) reaches
    `rpc_endpoint` exactly once, unambiguously -- unlike the old transport-keyed dispatch,
    which checked `'ws' in transports` first and so never reached `http_endpoint` at all
    for an endpoint like this. A backend serving more than one transport for the same rpc
    endpoint branches on `endpoint.transports` internally (see `bybit`'s `rpc_endpoint`
    override for a real example); this is not a second dispatch layer, just proof the
    base dispatch no longer loses a transport.
    """
    calls: list[str] = []

    class Backend(Generator):
      """A generator recording which hook it was routed to."""
      def rpc_endpoint(self, endpoint, references, *, class_name, method_name) -> str:
        calls.append('rpc')
        return 'rpc'
      def stream_endpoint(self, endpoint, references, *, class_name, method_name) -> str:
        calls.append('stream')
        return 'stream'

    ep = Endpoint.model_validate({
      'function': 'trading.get_order_state',
      'spec': {
        'kind': 'rpc', 'transports': ['http', 'ws'],
        'path': '/order', 'method': 'GET',
        'openapi': {
          'description': 'Dual-transport op.',
          'responses': {'200': {'description': 'ok'}},
        },
      },
    })
    Backend().endpoint(ep, {}, class_name='OrderState', method_name='get_order_state')
    assert calls == ['rpc']


def test_endpoint_schemas_dual_transport_only_ever_reaches_the_http_branch():
  """
  Pins today's known-incomplete behavior -- not a spec of correct behavior to keep.

  `transports: ['http', 'ws']` is representable per the design doc's Decision 1, but
  `endpoint_schemas` still checks `'http' in transports` first and returns, so
  `subscription` (the ws-derived schema source) is never consulted for a dual-transport
  endpoint. No real client declares one yet (see the design doc's Non-goals) -- see
  docs/superpowers/specs/2026-08-08-rpc-stream-transports-and-declared-envelope-design.md.
  """
  class Backend(Generator):
    """A generator that fails loudly if the ws-derived schema source is ever reached."""
    def subscription(self, endpoint):
      raise AssertionError('subscription() should not be reached for a dual-transport endpoint today')

  ep = Endpoint.model_validate({
    'function': 'trading.get_order_state',
    'spec': {
      'kind': 'rpc', 'transports': ['http', 'ws'],
      'path': '/order', 'method': 'GET',
      'openapi': {
        'description': 'Dual-transport op.',
        'responses': {'200': {'description': 'ok'}},
      },
    },
  })
  Backend().endpoint_schemas(ep)  # does not raise -- proves `subscription` was never called


def paged_endpoint(pagination: dict[str, Any], *, size_default: int | None = None) -> Endpoint:
  """Build an HTTP endpoint record carrying one pagination declaration.

  Args:
    pagination: The declaration the endpoint carries.
    size_default: Row cap the venue applies when the caller sends no page size, declared
      on the size parameter's own schema as bybit declares it. `None` leaves the operation
      silent about one, which is mexc's shape and the one that generates no guard.
  """
  ep = endpoint('market.orders', RECORD, title='Orders')
  spec = ep.spec.model_dump(by_alias=True)
  size = pagination.get('size')
  if size_default is not None and size is not None:
    spec['openapi']['parameters'] = [{
      'name': size['parameter'],
      'in': 'query',
      'required': False,
      'description': 'Page size.',
      'schema': {'type': 'integer', 'description': 'Page size.', 'default': size_default},
    }]
  return Endpoint.model_validate({
    'function': ep.function, 'spec': spec, 'pagination': pagination,
  })

def paged_header(*, args: list[str] = [], kwargs: list[str], validate: bool = True) -> Function:
  """Build the header a backend would hand `paged_method`, from bare parameter names."""
  header = Function(name='orders', asyn=True, method=True)
  header.args = [Function.Param(name=name, type='str') for name in args]
  header.kwargs = [Function.Param(name=name, type='int', required=False) for name in kwargs]
  if validate:
    header.kwargs.append(Function.Param(name='validate', type='bool | None', default='None'))
  header.return_type = 'Orders'
  return header

def walk(
  source: str, pages: list[Any], *,
  extra_namespace: dict[str, Any] | None = None, **call: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
  """Run a generated iterator against canned pages, returning yields and calls made.

  Executing the emitted source is the only way to show that a loop *terminates*; reading
  it back as a string only shows that it was written.

  Args:
    source: Generated `<method>_paged` source, unindented.
    pages: Payloads the underlying method returns, in order.
    extra_namespace: Additional names the generated source needs at exec time -- a
      nested-pagination test's constructed message type (`PageRequest`, say), which the
      default namespace has no reason to carry for every other test.
    call: Arguments passed to the iterator.

  Raises:
    IndexError: When the walk asks for more pages than were recorded.
  """
  namespace: dict[str, Any] = {
    'AsyncIterator': AsyncIterator, 'Orders': dict, 'LogicError': LogicError,
    'datetime': datetime, 'timedelta': timedelta, 'TimestampIso': datetime,
    **(extra_namespace or {}),
  }
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint class the generated iterator is mixed into."""',
    '  def __init__(self, pages):',
    '    self.pages = pages',
    '    self.calls = []',
    '  async def orders(self, *args, **kwargs):',
    '    self.calls.append(kwargs)',
    '    return self.pages[len(self.calls) - 1]',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, namespace)
  instance = namespace['Walk'](pages)

  async def collect() -> list[Any]:
    return [page async for page in getattr(instance, 'orders_paged')(**call)]

  return asyncio.run(collect()), instance.calls

def walk_partial(
  source: str, pages: list[Any], *,
  extra_namespace: dict[str, Any] | None = None, **call: Any,
) -> tuple[list[Any], list[dict[str, Any]], BaseException]:
  """Like `walk`, but for a walk expected to raise partway through: returns everything
  yielded *before* the raise, the calls made, and the exception itself, instead of letting
  `pytest.raises` swallow the partial progress a plain `walk(...)` call would have made.

  Args:
    source: See `walk`.
    pages: See `walk`.
    extra_namespace: See `walk`.
    call: See `walk`.

  Raises:
    AssertionError: When the walk does not raise at all.
  """
  namespace: dict[str, Any] = {
    'AsyncIterator': AsyncIterator, 'Orders': dict, 'LogicError': LogicError,
    'datetime': datetime, 'timedelta': timedelta, 'TimestampIso': datetime,
    **(extra_namespace or {}),
  }
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint class the generated iterator is mixed into."""',
    '  def __init__(self, pages):',
    '    self.pages = pages',
    '    self.calls = []',
    '  async def orders(self, *args, **kwargs):',
    '    self.calls.append(kwargs)',
    '    return self.pages[len(self.calls) - 1]',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, namespace)
  instance = namespace['Walk'](pages)

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

def walk_response(
  source: str, pages: list[Any], *,
  extra_namespace: dict[str, Any] | None = None, **call: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
  """Run a generated `PaginatedResponse`-shaped `<method>_paged` wrapper against canned
  pages, returning the pages yielded and the keyword arguments each underlying call made.

  Unlike `walk` (`paged_method`'s plain async-generator shape), `paged_response_method`
  generates an outer method returning a `PaginatedResponse` -- itself async-iterable --
  so the generated source's own annotations reference `PaginatedResponse` by name, which
  has to be in the exec namespace for the `def` statement itself to evaluate.

  Args:
    source: Generated `<method>_paged` source, unindented.
    pages: Payloads the underlying method returns, in order.
    extra_namespace: See `walk`.
    call: Arguments passed to the outer method.
  """
  namespace: dict[str, Any] = {
    'PaginatedResponse': PaginatedResponse, 'LogicError': LogicError,
    **(extra_namespace or {}),
  }
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint class the generated wrapper is mixed into."""',
    '  def __init__(self, pages):',
    '    self.pages = pages',
    '    self.calls = []',
    '  async def orders(self, *args, **kwargs):',
    '    self.calls.append(kwargs)',
    '    return self.pages[len(self.calls) - 1]',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, namespace)
  instance = namespace['Walk'](pages)

  async def collect() -> list[Any]:
    return [page async for page in getattr(instance, 'orders_paged')(**call)]

  return asyncio.run(collect()), instance.calls

def walk_response_partial(
  source: str, pages: list[Any], *,
  extra_namespace: dict[str, Any] | None = None, **call: Any,
) -> tuple[list[Any], list[dict[str, Any]], BaseException]:
  """Like `walk_response`, but for a walk expected to raise partway through -- see
  `walk_partial`'s identical reasoning for the plain async-generator shape."""
  namespace: dict[str, Any] = {
    'PaginatedResponse': PaginatedResponse, 'LogicError': LogicError,
    **(extra_namespace or {}),
  }
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint class the generated wrapper is mixed into."""',
    '  def __init__(self, pages):',
    '    self.pages = pages',
    '    self.calls = []',
    '  async def orders(self, *args, **kwargs):',
    '    self.calls.append(kwargs)',
    '    return self.pages[len(self.calls) - 1]',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, namespace)
  instance = namespace['Walk'](pages)

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

PAGE_TOTAL = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'total', 'path': 'data.totalPage', 'counts': 'pages'},
}
"""mexc's shape: a page number and a page count nested under an envelope key."""

PAGE_TOTAL_ROWS = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'rows'},
}
"""binance's `simple_earn/locked/list` shape: `PAGE_TOTAL` plus a declared `done.rows` --
the one `page`-strategy shape `paged_response_method` covers, unlike `paged_method`'s plain
async-generator shape, which needs no declared row field since it yields the whole
response."""

TOKEN_CURSOR = {
  'strategy': 'token',
  'cursor': {'parameter': 'cursor', 'from': 'nextPageCursor'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'absent_cursor'},
}
"""bybit's shape: an opaque token echoed back until the response stops supplying one."""

TOKEN_CURSOR_ROWS = {
  'strategy': 'token',
  'cursor': {'parameter': 'cursor', 'from': 'nextPageCursor'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'absent_cursor', 'rows': 'list'},
}
"""`TOKEN_CURSOR` plus a declared `done.rows` -- the one shape `paged_response_method`
covers (`docs/spec/authoring.md` rule 8), unlike `paged_method`'s plain async-generator
shape, which needs no declared row field since it yields the whole response."""

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
  'done': {'kind': 'total', 'path': 'total', 'counts': 'items'},
}
"""binance's `spot.http.prediction.order_history` shape: an offset walked until a
published item total is covered."""

WINDOW_DESCENDING = {
  'strategy': 'window',
  'bound': {'start': 'start', 'end': 'end'},
  'order': 'descending',
  'step': {'unit': 'ms', 'size': 1},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'empty'},
}
"""bybit's shape: a window walked into the past, past two inclusive bounds."""

SEEK_LAST_ID = {
  'strategy': 'seek',
  'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
  'size': {'parameter': 'limit'},
  'done': {'kind': 'short_page'},
}
"""mexc's shape: `fromId` read off the `id` field of the previous page's last row, not a
plain top-level field the venue hands back -- `historical_trades_paged`'s hand-written
reference (`clients/mexc/codegen/python.py`)."""

class TestPagedMethod:
  """A `pagination` declaration, and only a declaration, generates a page iterator."""

  def test_undeclared_endpoint_generates_nothing(self, generator: Generator):
    """The line between this and the heuristic it replaces: page-shaped is not paginated."""
    ep = endpoint('market.orders', RECORD, title='Orders')
    header = paged_header(kwargs=['page', 'page_size'])
    assert generator.paged_method(
      ep, method_name='orders', header=header, response_type='Orders',
    ) is None

  def test_declared_endpoint_generates_a_suffixed_iterator(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    )
    assert source is not None
    assert source.startswith('async def orders_paged(')
    assert '-> AsyncIterator[Orders]:' in source

  def test_driver_parameter_leaves_the_signature(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    signature = source.split('\n"""')[0]
    assert 'page_size' in signature
    assert 'page:' not in signature
    assert 'max_pages: int | None = None' in signature

  def test_page_total_walks_to_the_reported_page_count(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    page = {'data': {'totalPage': 3}}
    yielded, calls = walk(source, [page, page, page])
    assert len(yielded) == 3
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_page_total_raises_when_the_total_is_absent(self, generator: Generator):
    """A page that does not report the count cannot be walked past safely --
    `docs/pagination.md` §4."""
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [{}])

  def test_page_total_raises_when_the_total_disagrees_with_an_earlier_page(
    self, generator: Generator,
  ):
    """A live, growing total isn't itself a violation -- only a walk that already
    committed to one `total` value's index arithmetic then disagreeing with it mid-walk
    is (`docs/pagination.md` §4)."""
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [{'data': {'totalPage': 3}}, {'data': {'totalPage': 4}}])

  def test_page_total_yields_the_page_that_exposed_the_disagreement(self, generator: Generator):
    """The raise happens after the current page's own rows are yielded -- real data,
    valid on its own -- the same order the window truncation guard already uses."""
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    yielded, calls, error = walk_partial(
      source, [{'data': {'totalPage': 3}}, {'data': {'totalPage': 4}}],
    )
    assert isinstance(error, LogicError)
    # Both pages were yielded -- the second page's own rows are real, valid data too, and
    # the raise fires only once the walk tries to trust its (disagreeing) total.
    assert len(yielded) == 2
    assert len(calls) == 2

  def test_page_start_is_honoured(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({**PAGE_TOTAL, 'index': {'parameter': 'page', 'start': 0}}),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    _, calls = walk(source, [{'data': {'totalPage': 2}}, {'data': {'totalPage': 2}}])
    assert [call['page'] for call in calls] == [0, 1]

  def test_item_total_is_divided_by_the_page_size(self, generator: Generator):
    """`counts` exists so the loop bound is read, not guessed, from a total."""
    source = generator.paged_method(
      paged_endpoint({
        **PAGE_TOTAL,
        'done': {'kind': 'total', 'path': 'data.totalPage', 'counts': 'items'},
      }),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    page = {'data': {'totalPage': 5}}
    yielded, _ = walk(source, [page, page, page], page_size=2)
    assert len(yielded) == 3

  def test_item_total_walks_multiple_pages_when_size_is_omitted(self, generator: Generator):
    """Omitting `size` (using the endpoint's own default) must not read as "done".

    Before this test's own fix, `paged_termination` treated an omitted `size` itself as
    the terminator -- any caller relying on the documented default (`size_default`, read
    the venue's own real page size) instead of passing it explicitly got silently
    truncated to page 1, regardless of how many pages actually existed.
    """
    source = generator.paged_method(
      paged_endpoint({
        **PAGE_TOTAL,
        'done': {'kind': 'total', 'path': 'data.totalPage', 'counts': 'items'},
      }, size_default=2),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    page = {'data': {'totalPage': 5}}
    yielded, calls = walk(source, [page, page, page])
    assert len(yielded) == 3
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_short_page_counts_the_declared_collection(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'page',
        'index': {'parameter': 'page', 'start': 1},
        'size': {'parameter': 'pageSize'},
        'done': {'kind': 'short_page', 'rows': 'data'},
      }),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [{'data': [1, 2]}, {'data': [3]}], page_size=2)
    assert yielded == [{'data': [1, 2]}, {'data': [3]}]

  def test_short_page_measures_the_payload_when_no_collection_is_named(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'page',
        'index': {'parameter': 'page', 'start': 1},
        'size': {'parameter': 'pageSize'},
        'done': {'kind': 'short_page'},
      }),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [[1, 2], [3]], page_size=2)
    assert yielded == [[1, 2], [3]]

  def test_short_page_without_a_size_is_refused(self, generator: Generator):
    with pytest.raises(ValueError):
      generator.paged_method(
        paged_endpoint({
          'strategy': 'page',
          'index': {'parameter': 'page', 'start': 1},
          'done': {'kind': 'short_page'},
        }),
        method_name='orders', header=paged_header(kwargs=['page']),
        response_type='Orders',
      )

  def test_empty_page_ends_the_walk(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'page',
        'index': {'parameter': 'page', 'start': 1},
        'done': {'kind': 'empty', 'rows': 'data'},
      }),
      method_name='orders', header=paged_header(kwargs=['page']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [{'data': [1]}, {'data': []}])
    assert len(yielded) == 2

  def test_token_walk_sends_the_previous_cursor(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(TOKEN_CURSOR),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [{'nextPageCursor': 'a'}, {'nextPageCursor': ''}])
    assert len(yielded) == 2
    assert [call['cursor'] for call in calls] == [None, 'a']

  def test_token_walk_stops_on_a_missing_cursor(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(TOKEN_CURSOR),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [{}])
    assert len(yielded) == 1

  def test_token_walk_can_end_on_an_empty_page(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'token',
        'cursor': {'parameter': 'cursor', 'from': 'nextPageCursor'},
        'done': {'kind': 'empty', 'rows': 'list'},
      }),
      method_name='orders', header=paged_header(kwargs=['cursor']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [
      {'list': [1], 'nextPageCursor': 'a'},
      {'list': [], 'nextPageCursor': 'b'},
    ])
    assert len(yielded) == 2

  def test_offset_walk_advances_by_the_rows_it_received(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(OFFSET_EMPTY),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    ) or ''
    _, calls = walk(source, [[1, 2], [3, 4], []], limit=2)
    assert [call['offset'] for call in calls] == [0, 2, 4]

  def test_offset_walk_advances_when_the_page_size_is_omitted(self, generator: Generator):
    """The advance and the terminator have to agree about an optional size.

    `paged_termination` already tolerates `limit=None` — it falls back to stopping on an
    empty page — so an advance that added `None` to an integer would raise `TypeError` on
    page two of a walk the terminator considered perfectly well defined.
    """
    source = generator.paged_method(
      paged_endpoint(OFFSET_EMPTY),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[1, 2, 3], [4], []])
    assert len(yielded) == 3
    assert [call['offset'] for call in calls] == [0, 3, 4]

  def test_offset_walk_advances_by_a_nested_row_collection(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'offset',
        'offset': {'parameter': 'offset'},
        'done': {'kind': 'empty', 'rows': 'data'},
      }),
      method_name='orders', header=paged_header(kwargs=['offset']),
      response_type='Orders',
    ) or ''
    _, calls = walk(source, [{'data': [1, 2]}, {'data': [3]}, {'data': []}])
    assert [call['offset'] for call in calls] == [0, 2, 3]

  def test_offset_walk_with_nothing_to_step_by_returns_none(self, generator: Generator):
    """A total-terminated offset walk reads no rows, so an omittable size leaves no step
    for `paged_step` to compute -- `paged_method` now degrades gracefully (`None`, no
    `_paged` variant generated) rather than propagating `paged_step`'s own `ValueError`
    to its caller. Changed from raising to returning `None` once kraken's real
    `spot.account.trades_history` (codegen-mechanization migration) hit exactly this
    shape through the mechanized `rpc_endpoint` path -- every per-client hand-rolled
    backend already caught this identically at its own call site, so `paged_method`
    itself now carries that same graceful-degradation contract, narrowly scoped to only
    the `paged_step` call that can raise (see its own docstring)."""
    result = generator.paged_method(
      paged_endpoint({
        'strategy': 'offset',
        'offset': {'parameter': 'offset'},
        'size': {'parameter': 'limit'},
        'done': {'kind': 'total', 'path': 'total', 'counts': 'pages'},
      }),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    )
    assert result is None

  def test_offset_walk_steps_by_a_required_page_size_when_it_reads_no_rows(self):
    """With no rows to count, a size the method always has is the remaining step."""
    class Backend(Generator):
      """A generator whose page size is a required parameter."""
      def paged_always_set(self, size: Function.Param) -> bool:
        """Treat the page size as one the caller cannot omit."""
        return True
    source = Backend().paged_method(
      paged_endpoint({
        'strategy': 'offset',
        'offset': {'parameter': 'offset'},
        'size': {'parameter': 'limit'},
        'done': {'kind': 'total', 'path': 'total', 'counts': 'pages'},
      }),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    ) or ''
    _, calls = walk(source, [{'total': 2}, {'total': 2}], limit=5)
    assert [call['offset'] for call in calls] == [0, 5]

  def test_offset_total_walks_to_the_reported_item_count(self, generator: Generator):
    """binance's `spot.http.prediction.order_history` shape -- `docs/pagination.md` §2.2."""
    source = generator.paged_method(
      paged_endpoint(OFFSET_TOTAL, size_default=2),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    ) or ''
    page = {'total': 5}
    yielded, calls = walk(source, [page, page, page])
    assert len(yielded) == 3
    assert [call['offset'] for call in calls] == [0, 2, 4]

  def test_offset_total_raises_when_the_total_is_absent(self, generator: Generator):
    """`docs/pagination.md` §4 -- same stricter `total` as `page`'s own."""
    source = generator.paged_method(
      paged_endpoint(OFFSET_TOTAL, size_default=2),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [{}])

  def test_offset_total_raises_when_the_total_disagrees_with_an_earlier_page(
    self, generator: Generator,
  ):
    source = generator.paged_method(
      paged_endpoint(OFFSET_TOTAL, size_default=2),
      method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [{'total': 5}, {'total': 6}])

  def test_the_seek_driver_is_the_cursor_parameter(self, generator: Generator):
    pagination = paged_endpoint(SEEK_LAST_ID).pagination
    assert pagination is not None
    assert generator.pagination_driver(pagination) == 'from_id'

  def test_seek_walk_sends_the_previous_last_row_field(self, generator: Generator):
    """mexc's exact shape: `fromId` is the `id` of the last row of the previous page."""
    source = generator.paged_method(
      paged_endpoint(SEEK_LAST_ID),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[{'id': 1}, {'id': 2}], [{'id': 3}]], limit=2)
    assert len(yielded) == 2
    assert [call['from_id'] for call in calls] == [None, 2]

  def test_seek_walk_can_end_on_an_empty_page(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({**SEEK_LAST_ID, 'done': {'kind': 'empty'}}),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[{'id': 1}], []])
    assert len(yielded) == 2
    assert [call['from_id'] for call in calls] == [None, 1]

  def test_seek_walk_stops_when_no_row_carries_the_field(self, generator: Generator):
    """Mirrors mexc's own `if not ids: break` -- rows present, but none carry `id`."""
    source = generator.paged_method(
      paged_endpoint({**SEEK_LAST_ID, 'done': {'kind': 'empty'}}),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [[{'name': 'a'}], [{'name': 'b'}]])
    assert len(yielded) == 1

  def test_seek_walk_reads_a_declared_row_collection(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
        'done': {'kind': 'empty', 'rows': 'data'},
      }),
      method_name='orders', header=paged_header(kwargs=['from_id']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [{'data': [{'id': 1}, {'id': 2}]}, {'data': []}])
    assert len(yielded) == 2
    assert [call['from_id'] for call in calls] == [None, 2]

  def test_seek_walk_reads_a_nested_row_field(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'from_id', 'from': '[-1].trade.id'},
        'done': {'kind': 'empty'},
      }),
      method_name='orders', header=paged_header(kwargs=['from_id']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[{'trade': {'id': 9}}], []])
    assert len(yielded) == 2
    assert [call['from_id'] for call in calls] == [None, 9]

  def test_seek_cursor_matches_the_destination_parameter_type(self, generator: Generator):
    """kucoin's `ledgers_trade_hf`/`ledgers_margin_hf` shape: `lastId` is declared `int`
    on the wire while the row's own `id` field is a wire *string* on one of the two --
    the walk must cast to `lastId`'s own declared type, not the row field's, or the
    generated local disagrees with its own `int | None` annotation under pyright."""
    header = Function(name='orders', asyn=True, method=True, return_type='Orders')
    header.kwargs = [
      Function.Param(name='from_id', type='int', required=False),
      Function.Param(name='limit', type='int', required=False),
    ]
    source = generator.paged_method(
      paged_endpoint(SEEK_LAST_ID), method_name='orders', header=header,
      response_type='Orders',
    ) or ''
    assert 'from_id = int(from_id_values[-1])' in source
    yielded, calls = walk(source, [[{'id': '1'}, {'id': '2'}], [{'id': '3'}]], limit=2)
    assert len(yielded) == 2
    assert [call['from_id'] for call in calls] == [None, 2]

  def test_seek_cursor_skips_the_cast_for_a_non_numeric_parameter_type(
    self, generator: Generator,
  ):
    """A cursor parameter typed as something other than `str`/`int`/`float` (a rendered
    alias, say) isn't a callable constructor -- and, unlike the old `str` fallback,
    isn't cast through anything at all: the row field it's read off already comes back
    as that real type once read off an already-response-validated row (see
    `read_last_row`'s own `cast=None` docstring; `str(...)`-wrapping it would corrupt a
    real value instead of normalizing an untyped one)."""
    header = Function(name='orders', asyn=True, method=True, return_type='Orders')
    header.kwargs = [
      Function.Param(name='from_id', type='SomeAlias', required=False),
      Function.Param(name='limit', type='int', required=False),
    ]
    source = generator.paged_method(
      paged_endpoint(SEEK_LAST_ID), method_name='orders', header=header,
      response_type='Orders',
    ) or ''
    assert 'from_id = from_id_values[-1] if from_id_values else None' in source

  def test_seek_short_page_without_a_size_is_refused(self, generator: Generator):
    with pytest.raises(ValueError):
      generator.paged_method(
        paged_endpoint({
          'strategy': 'seek',
          'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
          'done': {'kind': 'short_page'},
        }),
        method_name='orders', header=paged_header(kwargs=['from_id']),
        response_type='Orders',
      )

  def test_seek_unchanged_stops_when_the_cursor_stops_advancing(self, generator: Generator):
    """dYdX's `get_historical_funding` shape: an inclusive block-height bound re-serves
    tied rows forever instead of ever emptying out. The walk has to notice the newly-read
    cursor agrees with the one just requested, not wait for a short or an empty page that
    never comes."""
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'before', 'from': '[-1].height'},
        'done': {'kind': 'unchanged'},
      }),
      method_name='orders', header=paged_header(kwargs=['before']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [
      [{'height': 5}, {'height': 5}], [{'height': 5}, {'height': 5}],
    ])
    assert len(yielded) == 2
    assert [call['before'] for call in calls] == [None, 5]

  def test_seek_unchanged_still_stops_on_an_empty_page(self, generator: Generator):
    """An empty page has no last row to read a cursor from -- `unchanged` has to treat
    that the same as "did not advance", not loop forever waiting for a comparison that can
    never happen."""
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'before', 'from': '[-1].height'},
        'done': {'kind': 'unchanged'},
      }),
      method_name='orders', header=paged_header(kwargs=['before']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[{'height': 5}, {'height': 4}], []])
    assert len(yielded) == 2
    assert [call['before'] for call in calls] == [None, 4]

  def test_seek_unchanged_walks_normally_while_the_cursor_advances(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'before', 'from': '[-1].height'},
        'done': {'kind': 'unchanged'},
      }),
      method_name='orders', header=paged_header(kwargs=['before']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [
      [{'height': 5}], [{'height': 4}], [{'height': 4}],
    ])
    assert len(yielded) == 3
    assert [call['before'] for call in calls] == [None, 5, 4]

  def test_seek_unchanged_reads_a_declared_row_collection(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'before', 'from': '[-1].height'},
        'done': {'kind': 'unchanged', 'rows': 'data'},
      }),
      method_name='orders', header=paged_header(kwargs=['before']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [
      {'data': [{'height': 5}]}, {'data': [{'height': 5}]},
    ])
    assert len(yielded) == 2
    assert [call['before'] for call in calls] == [None, 5]

  def test_seek_unchanged_needs_no_size(self, generator: Generator):
    """Unlike `short_page`, `unchanged` never measures the page against a page size, so
    it's not refused for lacking one -- mirrors `test_seek_short_page_without_a_size_is_
    refused`'s own endpoint shape, minus the size requirement."""
    source = generator.paged_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
        'done': {'kind': 'unchanged'},
      }),
      method_name='orders', header=paged_header(kwargs=['from_id']),
      response_type='Orders',
    )
    assert source is not None

  def test_window_walk_steps_past_an_inclusive_bound(self, generator: Generator):
    """The trap the strategy's step exists for: bybit bounds a window at both ends,
    inclusively, so a next window whose `end` equalled this window's `start` would
    re-read the boundary row. The declared step is what clears it -- verified against the
    live endpoint at `https://api.bybit.com/v5/market/kline`.

    Unobservable end-to-end through the full walk any more: the caller's own original
    `start` now caps the walk (below), so an ordinary, non-empty first page never actually
    reaches a second request to abut against. Checked directly against the generated
    source instead.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'upper = lower - 1' in source
    assert 'lower = upper - width' in source
    yielded, calls = walk(source, [[1]], start=100, end=140)
    assert len(yielded) == 1
    assert [(call['start'], call['end']) for call in calls] == [(100, 140)]

  def test_window_walk_stops_before_a_second_request_would_pass_the_original_bound(
    self, generator: Generator,
  ):
    """The fix: before this, a walk that only stopped on an empty page kept requesting new
    windows past the caller's own bound forever -- confirmed real, live behavior on both
    bybit's `funding_history_paged` (this shape) and binance's `funding_rate_paged`
    (ascending). Only one page is ever requested now, even though the mock below would
    happily serve a second and third non-empty page if the walk asked for them.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    # Only one page is ever consumed from this list -- a walk that regressed back to the
    # old unbounded behavior would raise `IndexError` reaching for a second/third here.
    yielded, calls = walk(source, [[1], [2], [3]], start=100, end=140)
    assert len(yielded) == 1
    assert calls == [{'start': 100, 'end': 140, 'limit': None, 'validate': None}]

  def test_an_ascending_window_walk_never_requests_past_the_original_end(
    self, generator: Generator,
  ):
    """Ascending mirror of `test_window_walk_stops_before_a_second_request_would_pass_the_
    original_bound` -- binance's `funding_rate_paged` shape. `end` is what the ascending
    walk's own original-bound cap checks against, so this is the case that check exists for
    even more directly than the descending one.
    """
    source = generator.paged_method(
      paged_endpoint({**WINDOW_DESCENDING, 'order': 'ascending'}),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[1], [2], [3]], start=100, end=140)
    assert len(yielded) == 1
    assert calls == [{'start': 100, 'end': 140, 'limit': None, 'validate': None}]

  def test_the_window_driver_is_the_bound_its_order_moves(self, generator: Generator):
    """`pagination_driver` answers for every strategy, including the two-bound one."""
    descending = paged_endpoint(WINDOW_DESCENDING).pagination
    ascending = paged_endpoint({**WINDOW_DESCENDING, 'order': 'ascending'}).pagination
    assert descending is not None and ascending is not None
    assert generator.pagination_driver(descending) == 'end'
    assert generator.pagination_driver(ascending) == 'start'

  def test_window_walk_keeps_both_bounds_in_the_signature(self, generator: Generator):
    """Unlike every other driver, a window bound is the caller's input as well as the loop's."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    signature = source.split('\n"""')[0]
    assert 'start: int | None = None' in signature
    assert 'end: int | None = None' in signature

  def test_an_ascending_window_walk_moves_the_other_bound(self, generator: Generator):
    """`order` is the only statement of direction, and the bound it moves follows from it.

    Checked against the generated source, not a multi-page walk: an ascending walk's own
    original-bound cap (below) means an ordinary, non-empty first page never reaches a
    real second request to observe the moved bound on.
    """
    source = generator.paged_method(
      paged_endpoint({**WINDOW_DESCENDING, 'order': 'ascending'}),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'lower = upper + 1' in source
    assert 'upper = lower + width' in source
    _, calls = walk(source, [[1]], start=100, end=140)
    assert [(call['start'], call['end']) for call in calls] == [(100, 140)]

  def test_a_zero_step_abuts_an_exclusive_bound(self, generator: Generator):
    """mexc's shape: `endTime` is exclusive, so a step of one would skip a row.

    The step is declared rather than assumed to be one tick precisely because the two
    venues disagree about it. Checked against the generated source for the same reason as
    the inclusive-bound cases above.
    """
    source = generator.paged_method(
      paged_endpoint({
        **WINDOW_DESCENDING, 'order': 'ascending', 'step': {'unit': 'ms', 'size': 0},
      }),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'lower = upper\n' in source
    assert 'upper = lower + width' in source
    _, calls = walk(source, [[1]], start=100, end=140)
    assert [(call['start'], call['end']) for call in calls] == [(100, 140)]

  def test_a_window_walk_without_bounds_is_refused_at_runtime(self, generator: Generator):
    """A walk chunks the width the caller states, so there is no walk without one."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    with pytest.raises(ValueError):
      walk(source, [[1]], start=100)

  def test_a_window_bound_the_method_lacks_is_refused(self, generator: Generator):
    """The declaration names spec parameters; a bound absent from the header is a dead loop."""
    with pytest.raises(ValueError):
      generator.paged_method(
        paged_endpoint(WINDOW_DESCENDING),
        method_name='orders', header=paged_header(kwargs=['start', 'limit']),
        response_type='Orders',
      )

  def test_a_window_bound_is_normalised_by_the_client(self):
    """A client whose bounds accept a `datetime` cannot subtract them until it converts.

    mexc types every timestamp parameter as `Timestamp`, so the shared loop asks the
    backend for the conversion rather than assuming the bound is already an integer.
    """
    class Backend(Generator):
      """A generator whose timestamp parameters accept a `datetime` too."""
      def paged_window_bound(
        self, expression: str, *, unit: str, param: Function.Param,
      ) -> tuple[str, bool]:
        """Normalise a bound to the venue's own unit."""
        return f'dump_{unit}({expression})', False
    source = Backend().paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'lower = dump_ms(start)' in source
    assert 'upper = dump_ms(end)' in source

  def test_a_full_window_page_stops_the_walk_rather_than_losing_rows(self, generator: Generator):
    """The defect the guard exists for: a capped window and a walk that steps past it.

    The venue answers a window wider than its page size with `size` rows and says nothing
    about the ones it withheld, so the walk would move the window past them and the caller
    would read a series with a hole in it and no way to notice.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [[1, 2], [3], []], start=100, end=140, limit=2)

  def test_the_truncation_error_names_the_endpoint_and_the_window(self, generator: Generator):
    """A caller has to know *which* window to narrow, and the bounds sent are not the ones passed.

    The truncation raise fires on the caller's own first window, not a second one -- the
    original-bound cap (above) means a window strategy never issues a second real request
    at all, so a full page is always the *first* page's.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError) as excinfo:
      walk(source, [[1, 2]], start=100, end=140, limit=2)
    message = str(excinfo.value)
    assert 'orders_paged' in message
    assert '100 to 140' in message
    assert 'allow_truncation=True' in message

  def test_allow_truncation_lets_the_walk_run_on(self, generator: Generator):
    """The opt-out is the whole reason the guard can be strict by default.

    A caller sampling a series rather than enumerating it accepts a capped first window
    instead of the raise -- but the opt-out only silences that one raise. It never grants a
    second request past the caller's own bound; that cap (above) is a separate, always-on
    rule, not a truncation-recovery mechanism. The mock below would happily serve a second
    page if asked; it never is.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(
      source, [[1, 2], [3]], start=100, end=140, limit=2, allow_truncation=True,
    )
    assert len(yielded) == 1
    assert [(call['start'], call['end']) for call in calls] == [(100, 140)]

  def test_a_short_window_page_is_not_truncation(self, generator: Generator):
    """A sparse window is the normal case the strategy refuses `short_page` to allow."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [[1]], start=100, end=140, limit=5)
    assert len(yielded) == 1

  def test_an_omitted_page_size_falls_back_to_the_declared_default(self, generator: Generator):
    """The defect this branch shipped with: the natural call passes bounds and no size.

    A guard reading `limit is not None` was silent on exactly that call, so
    `kline_paged(symbol=..., interval=..., start=..., end=...)` walked past the rows bybit
    withheld while its docstring promised a raise. The venue's own documented default is
    what the page is full relative to when the caller states nothing.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=2),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [[1, 2]], start=100, end=140)

  def test_the_opt_out_silences_the_default_page_size_too(self, generator: Generator):
    """The escape hatch has to cover the call the guard now fires on, or it is a trap."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=2),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[1, 2], [3]], start=100, end=140, allow_truncation=True)
    assert len(yielded) == 1
    assert [(call['start'], call['end']) for call in calls] == [(100, 140)]

  def test_an_explicit_page_size_still_beats_the_declared_default(self, generator: Generator):
    """The default is the fallback, not the cap: a caller asking for fewer rows fills sooner."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [[1, 2]], start=100, end=140, limit=5)
    assert len(yielded) == 1, 'five rows were asked for and two arrived, so nothing was capped'
    with pytest.raises(LogicError):
      walk(source, [[1, 2]], start=100, end=140, limit=2)

  def test_a_size_without_a_declared_default_generates_no_guard(self, generator: Generator):
    """mexc's shape: `limit` is optional and the venue publishes no default.

    A guard here could only read `limit is not None`, which is silent on the call that
    passes bounds and no size — the one that loses rows. Generating it would put a promise
    in the docstring that the common call cannot keep, so nothing is generated and the
    docstring says the walk moves past withheld rows.
    """
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'allow_truncation' not in source
    assert 'LogicError' not in source
    assert 'raises `LogicError`' not in source
    assert 'it caps a wider one' in source, 'the docstring states the loss instead'
    yielded, _ = walk(source, [[1, 2]], start=100, end=140, limit=2)
    assert len(yielded) == 1

  def test_a_window_declaring_no_size_generates_no_guard(self, generator: Generator):
    """bybit's `historical_volatility` shape: no page size in the spec, so no evidence."""
    declaration = {key: value for key, value in WINDOW_DESCENDING.items() if key != 'size'}
    source = generator.paged_method(
      paged_endpoint(declaration),
      method_name='orders', header=paged_header(kwargs=['start', 'end']),
      response_type='Orders',
    ) or ''
    assert 'allow_truncation' not in source
    assert 'raises `LogicError`' not in source
    yielded, _ = walk(source, [[1, 2]], start=100, end=140)
    assert len(yielded) == 1

  def test_a_guarded_window_promises_the_raise_it_carries(self, generator: Generator):
    """The docstring is the only statement of the guard a caller reads before hitting it."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders', header=paged_header(kwargs=['start', 'end', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'raises `LogicError`' in source
    assert '`allow_truncation=True`' in source

  def test_only_a_guarded_window_asks_for_the_exception_import(self, generator: Generator):
    """The import follows the guard, so an unguarded module does not carry a dead name."""
    header = paged_header(kwargs=['start', 'end', 'limit'])
    guarded = generator.paged_imports(
      paged_endpoint(WINDOW_DESCENDING, size_default=200), header=header,
    )
    assert guarded.get('truewire_core.exceptions') == {'LogicError'}
    assert 'truewire_core.exceptions' not in generator.paged_imports(
      paged_endpoint(WINDOW_DESCENDING), header=header,
    )
    unsized = {key: value for key, value in WINDOW_DESCENDING.items() if key != 'size'}
    assert 'truewire_core.exceptions' not in generator.paged_imports(
      paged_endpoint(unsized), header=paged_header(kwargs=['start', 'end']),
    )

  def test_a_page_total_endpoint_always_asks_for_the_exception_import(self, generator: Generator):
    """Unlike a window's truncation guard, `page`/`offset`+`total`'s own `LogicError`
    raise (`docs/pagination.md` §4) is unconditional -- every such endpoint needs the
    import, not just one whose page size happens to be resolvable."""
    assert generator.paged_imports(
      paged_endpoint(PAGE_TOTAL), header=paged_header(kwargs=['page', 'page_size']),
    ).get('truewire_core.exceptions') == {'LogicError'}
    assert generator.paged_imports(
      paged_endpoint(OFFSET_TOTAL), header=paged_header(kwargs=['offset', 'limit']),
    ).get('truewire_core.exceptions') == {'LogicError'}

  def test_a_timestamp_bounded_window_asks_for_the_timedelta_import(self, generator: Generator):
    """A step rendered as `timedelta(...)` needs `datetime.timedelta` imported to run."""
    header = paged_header(kwargs=['start', 'end', 'limit'])
    header.kwargs[0] = Function.Param(name='start', type='TimestampMillis', required=False)
    imports = generator.paged_imports(paged_endpoint(WINDOW_DESCENDING), header=header)
    assert imports.get('datetime') == {'timedelta'}

  def test_a_date_time_bounded_window_asks_for_the_timedelta_import(self, generator: Generator):
    """A `date-time`-formatted bound now renders as `TimestampIso`
    (`truewire.generation.python.types.parser.Parser.TIMESTAMP_FORMATS`), same family as an
    epoch bound -- dYdX's `get_candles`'s `fromISO`/`toISO` are this shape."""
    header = paged_header(kwargs=['start', 'end', 'limit'])
    header.kwargs[0] = Function.Param(name='start', type='TimestampIso', required=False)
    imports = generator.paged_imports(paged_endpoint(WINDOW_DESCENDING), header=header)
    assert imports.get('datetime') == {'timedelta'}

  def test_a_date_time_window_walk_steps_past_an_inclusive_bound(self, generator: Generator):
    """Runtime proof: a `TimestampIso`-bounded window actually walks, via `timedelta`
    arithmetic rather than `TypeError`-raising bare-number arithmetic on a `datetime` --
    `TimestampIso` is `Annotated[datetime, ...]`, so it's still a real `datetime` at
    runtime, same as `Timestamp`-typed epoch bounds already are.

    Only one real request is ever made (the original-bound cap, above), so this checks the
    generated step arithmetic directly and runs the walk for that one page only.
    """
    header = paged_header(kwargs=['start', 'end', 'limit'])
    header.kwargs[0] = Function.Param(name='start', type='TimestampIso', required=False)
    header.kwargs[1] = Function.Param(name='end', type='TimestampIso', required=False)
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders', header=header,
      response_type='Orders',
    ) or ''
    assert 'timedelta(milliseconds=1)' in source
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(milliseconds=40)
    yielded, calls = walk(source, [[1]], start=start, end=end)
    assert len(yielded) == 1
    assert isinstance(calls[0]['start'], datetime)
    assert (calls[0]['start'], calls[0]['end']) == (start, end)

  def test_an_int_bounded_window_does_not_ask_for_the_timedelta_import(self, generator: Generator):
    """The bare-integer step never renders `timedelta(...)`, so the import would be dead."""
    header = paged_header(kwargs=['start', 'end', 'limit'])
    imports = generator.paged_imports(paged_endpoint(WINDOW_DESCENDING), header=header)
    assert 'datetime' not in imports

  def test_a_zero_step_window_does_not_ask_for_the_timedelta_import(self, generator: Generator):
    """No step means no advance statement at all, so a `Timestamp` bound needs nothing imported."""
    header = paged_header(kwargs=['start', 'end', 'limit'])
    header.kwargs[0] = Function.Param(name='start', type='TimestampMillis', required=False)
    unstepped = {**WINDOW_DESCENDING, 'step': {'unit': 'ms', 'size': 0}}
    imports = generator.paged_imports(paged_endpoint(unstepped), header=header)
    assert 'datetime' not in imports

  def test_the_truncation_keyword_does_not_capture_a_loop_local(self, generator: Generator):
    """The guard reads a keyword, so a local the walk needed to name `allow_truncation` moves."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING, size_default=200),
      method_name='orders',
      header=paged_header(kwargs=['start', 'end', 'limit', 'rows']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[1]], start=100, end=140, limit=5, rows=7)
    assert len(yielded) == 1
    assert calls[0]['rows'] == 7

  def test_window_locals_cannot_shadow_a_parameter(self, generator: Generator):
    """A venue parameter named `width` must survive the walk that computes one."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING),
      method_name='orders',
      header=paged_header(kwargs=['start', 'end', 'limit', 'lower', 'upper', 'width']),
      response_type='Orders',
    ) or ''
    _, calls = walk(source, [[1], []], start=100, end=140, lower=1, upper=2, width=3)
    assert calls[0]['lower'] == 1
    assert calls[0]['upper'] == 2
    assert calls[0]['width'] == 3
    assert (calls[0]['start'], calls[0]['end']) == (100, 140)

  def test_max_pages_cuts_a_walk_that_would_continue(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(source, [{'data': {'totalPage': 9}}], max_pages=1)
    assert len(yielded) == 1

  def test_nested_path_read_survives_a_missing_wrapper(self, generator: Generator):
    """Every step of a dotted path is a key the venue may omit on some page --
    `read_path` tolerates it (binds `total` to `None`) rather than crashing with
    `AttributeError`. The walk still raises `LogicError` for the missing total itself
    (`docs/pagination.md` §4), but from the cleanly-resolved `None` a tolerant read
    produced, not from an exception the read failed to catch."""
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [{'data': None}])

  def test_loop_locals_cannot_shadow_a_parameter(self, generator: Generator):
    """A venue parameter named `response` must not be overwritten by the loop."""
    source = generator.paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size', 'response']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [{'data': {'totalPage': 1}}], response=7)
    assert len(yielded) == 1
    assert calls[0]['response'] == 7

  def test_default_identifier_snake_cases_a_spec_name(self, generator: Generator):
    """`pageSize` in the declaration reaches `page_size` in the header without help."""
    assert generator.identifier('pageSize') == 'page_size'

  def test_client_identifier_renaming_is_respected(self):
    """A backend that renames parameters moves the loop with them.

    The declaration names parameters as the spec writes them and the header names them as
    the client writes them, so the two are bridged by `identifier` and nothing else.
    """
    class Backend(Generator):
      """A generator that gives the page parameters unrelated Python names."""
      def identifier(self, name: str) -> str:
        """Rename the two page parameters, leaving everything else alone."""
        return {'page': 'page_num', 'pageSize': 'per_page'}.get(name, name)
    source = Backend().paged_method(
      paged_endpoint(PAGE_TOTAL),
      method_name='orders', header=paged_header(kwargs=['page_num', 'per_page']),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [{'data': {'totalPage': 1}}], per_page=5)
    assert len(yielded) == 1
    assert calls[0] == {'per_page': 5, 'page_num': 1, 'validate': None}


def test_a_callable_endpoints_iterator_is_reachable():
  """A leaf hanging off a router as a field is invoked by calling the attribute it is
  annotated under — `client.v1.trading.candles(...)` — so its method name is `__call__`.

  `__call___paged` is two leading underscores and no trailing one, which is exactly the
  form Python mangles inside a class body: the iterator would bind as
  `_Candles__call___paged`, and every caller, plus the docstring naming it, would be
  wrong. Executing it is the assertion, because reading the name back out of the source
  would not show that `getattr` finds it.
  """
  source = Generator().paged_method(
    paged_endpoint(OFFSET_EMPTY),
    method_name='__call__', header=paged_header(kwargs=['offset', 'limit']),
    response_type='Orders',
  ) or ''
  assert source.startswith('async def paged(')
  assert '__call___paged' not in source
  assert 'Yield successive pages of this endpoint.' in source

  namespace: dict[str, Any] = {'AsyncIterator': AsyncIterator, 'Orders': dict}
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint reached by calling it, the way a router field is."""',
    '  def __init__(self, pages):',
    '    self.pages = pages',
    '    self.calls = 0',
    '  async def __call__(self, *args, **kwargs):',
    '    self.calls += 1',
    '    return self.pages[self.calls - 1]',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, namespace)
  instance = namespace['Walk']([[1], []])

  async def collect() -> list[Any]:
    return [page async for page in instance.paged(limit=1)]

  assert asyncio.run(collect()) == [[1], []]


def test_a_named_methods_iterator_keeps_the_suffix():
  """Every other endpoint is unaffected: a real method name still gets `<name>_paged`."""
  source = Generator().paged_method(
    paged_endpoint(OFFSET_EMPTY),
    method_name='orders', header=paged_header(kwargs=['offset', 'limit']),
    response_type='Orders',
  ) or ''
  assert source.startswith('async def orders_paged(')
  assert 'Yield successive pages of `orders`.' in source


def test_a_datetime_window_steps_by_a_timedelta():
  """`datetime + 1` is a TypeError; the step has to carry the declared unit."""
  from truewire.codegen.python import Generator

  assert Generator().paged_step_expression(1, unit='ms', is_datetime=True) == 'timedelta(milliseconds=1)'
  assert Generator().paged_step_expression(1, unit='s', is_datetime=True) == 'timedelta(seconds=1)'
  assert Generator().paged_step_expression(5, unit='us', is_datetime=True) == 'timedelta(microseconds=5)'


class TestPaginationDriverRequired:
  """`pagination_driver_required` -- Fix 3's shared dispatch-gate question, factored out
  of `paged_method`'s own inline `driver_required` check so `rpc_endpoint`/`grpc_endpoint`
  can decide `seedable` without duplicating it."""

  def test_a_required_seek_cursor_is_required(self, generator: Generator):
    pagination = paged_endpoint(SEEK_LAST_ID).pagination
    assert pagination is not None
    header = Function(name='orders', asyn=True, method=True)
    header.kwargs = [Function.Param(name='from_id', type='int', required=True)]
    assert generator.pagination_driver_required(header, pagination) is True

  def test_an_optional_seek_cursor_is_not_required(self, generator: Generator):
    pagination = paged_endpoint(SEEK_LAST_ID).pagination
    assert pagination is not None
    header = paged_header(kwargs=['from_id', 'limit'])
    assert generator.pagination_driver_required(header, pagination) is False

  def test_a_required_token_cursor_is_required(self, generator: Generator):
    pagination = paged_endpoint(TOKEN_CURSOR).pagination
    assert pagination is not None
    header = Function(name='orders', asyn=True, method=True)
    header.kwargs = [Function.Param(name='cursor', type='str', required=True)]
    assert generator.pagination_driver_required(header, pagination) is True

  def test_page_strategy_is_never_required(self, generator: Generator):
    """`page`/`offset` seed from `index.start`/`0`, never ambiguous with "done" the way an
    absent `token`/`seek` cursor is -- this must always answer `False` for them,
    regardless of whether the index parameter itself happens to be required."""
    pagination = paged_endpoint(PAGE_TOTAL).pagination
    assert pagination is not None
    header = Function(name='orders', asyn=True, method=True)
    header.kwargs = [Function.Param(name='page', type='int', required=True)]
    assert generator.pagination_driver_required(header, pagination) is False

  def test_missing_driver_parameter_is_not_required(self, generator: Generator):
    pagination = paged_endpoint(SEEK_LAST_ID).pagination
    assert pagination is not None
    header = Function(name='orders', asyn=True, method=True)
    assert generator.pagination_driver_required(header, pagination) is False


class TestPagedResponseMethod:
  """`paged_response_method` -- the `PaginatedResponse`-shaped wrapper `token`/
  `absent_cursor` pagination generates, dYdX's only real (proto3/betterproto2, `'attr'`-
  accessor) caller before this pass. `.agents/handoff/paged_response_method_rest_bugs.md`
  found two bugs a REST/JSON-RPC (`'dict'`-accessor) caller hits that dYdX never did: a
  spurious `Optional` on the rows/state read (`response` is never `None` by construction,
  regardless of accessor), and the cursor's zero-value seed sent as a literal wrong
  cursor on the walk's first call, for a venue where the zero value isn't wire-equivalent
  to "absent" the way a proto3 field's is.
  """

  def test_rows_and_state_reads_carry_no_optional_guard(self, generator: Generator):
    """Neither read may use the `if response is not None else None` ternary: `response`
    is `await self.orders(...)`'s own always-present return value, so guarding it only
    costs pyright a spurious `list[...] | None`/`str | None` it can't prove away."""
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_CURSOR_ROWS),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      rows_type='dict', state_type='str', zero_value_is_wire_absent=False,
    ) or ''
    assert 'is not None else None' not in source
    assert 'rows = response.get(' in source
    assert 'state = response.get(' in source

  def test_rest_cursor_seed_is_coerced_to_none_on_the_first_call(self, generator: Generator):
    """`zero_value_is_wire_absent=False` (alchemy's own REST/JSON-RPC shape): the seeded
    `''` cursor must not reach the wire as a literal, wrong value on page one."""
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_CURSOR_ROWS),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      rows_type='dict', state_type='str', zero_value_is_wire_absent=False,
    ) or ''
    assert 'cursor=(cursor or None)' in source
    yielded, calls = walk_response(source, [
      {'list': [{'a': 1}], 'nextPageCursor': 'next'},
      {'list': [{'a': 2}], 'nextPageCursor': ''},
    ])
    assert [call['cursor'] for call in calls] == [None, 'next']
    assert yielded == [[{'a': 1}], [{'a': 2}]]

  def test_proto_cursor_seed_passes_through_unchanged(self, generator: Generator):
    """`zero_value_is_wire_absent=True` (dYdX's real usage): the seed is wire-correct as
    a literal value, so it must reach the first call unchanged -- `driver=driver`,
    exactly as it was before this parameter existed, not coerced to `None`."""
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_CURSOR_ROWS),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      rows_type='dict', state_type='str', zero_value_is_wire_absent=True,
    ) or ''
    assert 'cursor=cursor' in source
    assert 'cursor=(cursor or None)' not in source
    yielded, calls = walk_response(source, [
      {'list': [{'a': 1}], 'nextPageCursor': 'next'},
      {'list': [{'a': 2}], 'nextPageCursor': ''},
    ])
    assert [call['cursor'] for call in calls] == ['', 'next']
    assert yielded == [[{'a': 1}], [{'a': 2}]]

  def test_zero_value_is_wire_absent_defaults_true(self, generator: Generator):
    """Backward compatibility: every caller before this parameter existed got the
    pass-through behaviour, including alchemy's own `_guard_paged_response_driver`
    regex patch, which still expects a literal `<driver>=<driver>` call argument to
    rewrite -- so an omitted `zero_value_is_wire_absent` must not silently change that."""
    explicit = generator.paged_response_method(
      paged_endpoint(TOKEN_CURSOR_ROWS),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      rows_type='dict', state_type='str', zero_value_is_wire_absent=True,
    )
    implicit = generator.paged_response_method(
      paged_endpoint(TOKEN_CURSOR_ROWS),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      rows_type='dict', state_type='str',
    )
    assert explicit == implicit


TOKEN_REQUIRED_TIMESTAMP = {
  'strategy': 'token',
  'cursor': {'parameter': 'end_timestamp', 'from': 'continuation'},
  'done': {'kind': 'absent_cursor', 'rows': 'list'},
}
"""deribit's real, pre-existing `market_data.get_volatility_index_data` shape: a required,
non-scalar (`TimestampMillis`) cursor with no server-side default -- the very case that
motivated finding Fix 3's gap in the first place, for `paged_response_method`'s own
`token` branch (the `seek` sibling is `TestPagedResponseSeekRequiredCursor`, above)."""


def required_timestamp_token_header() -> Function:
  """Header shaped like deribit's `get_volatility_index_data`: a `TimestampMillis` cursor
  the caller must always supply, with no server-side default."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [
    Function.Param(name='end_timestamp', type='TimestampMillis', required=True),
    Function.Param(name='validate', type='bool | None', default='None'),
  ]
  header.return_type = 'Orders'
  return header


class TestPagedResponseMethodTokenRequiredCursor:
  """Fix 3 (`docs/pagination.md`): a required, non-scalar `token` cursor. Before this,
  `TimestampMillis` has no entry in `_SCALAR_ZERO_VALUES`, so `state_type not in
  _SCALAR_ZERO_VALUES` raised `ValueError` outright, and the endpoint fell to the
  plain-generator `paged_method` fallback at the dispatch site instead."""

  def test_generates_a_paginatedresponse_shaped_wrapper(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_REQUIRED_TIMESTAMP),
      method_name='orders', header=required_timestamp_token_header(),
      rows_type='dict', state_type='TimestampMillis', zero_value_is_wire_absent=False,
    ) or ''
    assert source.startswith('def orders_paged(')
    assert '-> PaginatedResponse[dict, TimestampMillis]:' in source

  def test_keeps_the_cursor_required_on_the_outer_signature(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_REQUIRED_TIMESTAMP),
      method_name='orders', header=required_timestamp_token_header(),
      rows_type='dict', state_type='TimestampMillis', zero_value_is_wire_absent=False,
    ) or ''
    signature = source.split('\n"""')[0]
    assert 'end_timestamp: TimestampMillis' in signature
    assert 'end_timestamp: TimestampMillis | None' not in signature

  def test_seeds_from_the_callers_own_argument_not_a_zero_value(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_REQUIRED_TIMESTAMP),
      method_name='orders', header=required_timestamp_token_header(),
      rows_type='dict', state_type='TimestampMillis', zero_value_is_wire_absent=False,
    ) or ''
    assert 'end_timestamp=end_timestamp' in source
    assert 'end_timestamp=(end_timestamp or None)' not in source
    assert 'return PaginatedResponse(end_timestamp, next)' in source

  def test_walks_by_the_declared_response_field(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(TOKEN_REQUIRED_TIMESTAMP),
      method_name='orders', header=required_timestamp_token_header(),
      rows_type='dict', state_type='TimestampMillis', zero_value_is_wire_absent=False,
    ) or ''
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    later = datetime.fromtimestamp(5, tz=timezone.utc)
    yielded, calls = walk_response(
      source, [
        {'list': [{'a': 1}], 'continuation': later},
        {'list': [{'a': 2}], 'continuation': None},
      ],
      extra_namespace={'TimestampMillis': datetime}, end_timestamp=start,
    )
    assert [call['end_timestamp'] for call in calls] == [start, later]
    assert yielded == [[{'a': 1}], [{'a': 2}]]


class TestPagedResponsePageTotal:
  """`paged_response_method`'s `page`-strategy dispatch, for a `total` terminator that
  also declares `done.rows` -- binance's `simple_earn/locked/list` shape. Unlike a
  `token`/`seek` cursor, the page index's own starting value is never ambiguous with "done"
  the way an absent cursor is, so none of `paged_response_method`'s zero-value-seeding
  machinery applies here.
  """

  def test_generates_a_paginatedresponse_shaped_wrapper(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    assert source.startswith('def orders_paged(')
    assert '-> PaginatedResponse[dict, int]:' in source
    assert 'page:' not in source.split('"""')[0]

  def test_counting_items_walks_to_the_reported_item_count(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    yielded, calls = walk_response(source, [
      {'rows': [{'a': 1}], 'total': 3},
      {'rows': [{'a': 2}], 'total': 3},
      {'rows': [{'a': 3}], 'total': 3},
    ], page_size=1)
    assert [call['page'] for call in calls] == [1, 2, 3]
    assert yielded == [[{'a': 1}], [{'a': 2}], [{'a': 3}]]

  def test_counting_items_walks_multiple_pages_when_size_is_omitted(
    self, generator: Generator,
  ):
    """Omitting `page_size` (using the endpoint's own declared default) must not read as
    "done" -- the real bug this guards against, confirmed live on bybit's
    `card/point/records` (`pageSize` optional, `default: 10`). Before the fix, `next`
    read `page_size is None` itself as the terminator, so any caller relying on the
    documented default instead of passing it explicitly got silently truncated to page 1
    regardless of how many pages actually existed."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS, size_default=1),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    yielded, calls = walk_response(source, [
      {'rows': [{'a': 1}], 'total': 3},
      {'rows': [{'a': 2}], 'total': 3},
      {'rows': [{'a': 3}], 'total': 3},
    ])
    assert [call['page'] for call in calls] == [1, 2, 3]
    assert yielded == [[{'a': 1}], [{'a': 2}], [{'a': 3}]]

  def test_counting_items_with_no_declared_default_falls_back_to_an_empty_page(
    self, generator: Generator,
  ):
    """No declared default (mexc's/bybit's `web3.prediction.*` shape) means an omitted
    `page_size` can't be multiplied against the total at all -- the walk can no longer
    decide by arithmetic, so it falls back to the one signal that's always safe regardless
    of `size`: an empty page really is the end. It must never fall back to "done" on the
    very first page just because `size` was omitted."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    yielded, calls = walk_response(source, [
      {'rows': [{'a': 1}], 'total': 99},
      {'rows': [{'a': 2}], 'total': 99},
      {'rows': [], 'total': 99},
    ])
    assert [call['page'] for call in calls] == [1, 2, 3]
    assert yielded == [[{'a': 1}], [{'a': 2}]]

  def test_counting_pages_walks_to_the_reported_page_count(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint({**PAGE_TOTAL_ROWS, 'done': {**PAGE_TOTAL_ROWS['done'], 'counts': 'pages'}}),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    page = {'rows': [{'a': 1}], 'total': 2}
    yielded, calls = walk_response(source, [page, page])
    assert [call['page'] for call in calls] == [1, 2]
    assert yielded == [[{'a': 1}], [{'a': 1}]]

  def test_raises_when_the_total_is_absent(self, generator: Generator):
    """`docs/pagination.md` §4 -- same stricter `total` as `paged_method`'s own."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    with pytest.raises(LogicError):
      walk_response(source, [{'rows': [{'a': 1}]}])

  def test_raises_when_the_total_disagrees_with_an_earlier_page(self, generator: Generator):
    """A live, growing total isn't itself a violation -- only a walk that already
    committed to one `total` value's index arithmetic then disagreeing with it mid-walk
    is (`docs/pagination.md` §4). The page that exposed the disagreement is still yielded
    first -- real data, valid on its own."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    yielded, calls, error = walk_response_partial(source, [
      {'rows': [{'a': 1}], 'total': 3},
      {'rows': [{'a': 2}], 'total': 4},
    ], page_size=1)
    assert isinstance(error, LogicError)
    assert yielded == [[{'a': 1}], [{'a': 2}]]
    assert len(calls) == 2

  def test_a_page_missing_its_rows_key_entirely_coerces_to_an_empty_list(
    self, generator: Generator,
  ):
    """binance's shape on several `page`+`total` endpoints: `rows` is `NotRequired` on the
    response schema, so `response.get('rows')` types as `list[T] | None`, not `list[T]`.
    Left uncoerced, that's dishonest against `next`'s own declared return type
    (`tuple[list[T], int | None]`) and fails pyright -- exactly the class of bug
    `int(...) if ... is not None else None` already guards against for `total`."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_TOTAL_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    assert 'rows = rows if rows is not None else []' in source

  def test_page_start_is_honoured_as_the_seed(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint({**PAGE_TOTAL_ROWS, 'index': {'parameter': 'page', 'start': 0}}),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    _, calls = walk_response(source, [
      {'rows': [{'a': 1}], 'total': 2}, {'rows': [{'a': 2}], 'total': 2},
    ], page_size=1)
    assert [call['page'] for call in calls] == [0, 1]

  def test_short_page_terminator_dispatches_to_the_exhausted_shape(self, generator: Generator):
    """A `short_page`-terminated `page` walk is no longer refused here: it renders through
    `paged_response_page_exhausted` (see `TestPagedResponsePageExhausted`)."""
    pagination = {**PAGE_TOTAL_ROWS, 'done': {'kind': 'short_page', 'rows': 'rows'}}
    source = generator.paged_response_method(
      paged_endpoint(pagination),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    assert '-> PaginatedResponse[dict, int]:' in source
    assert 'total' not in source


PAGE_SHORT_ROWS = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'short_page', 'rows': 'data'},
}
"""A page walk ended by a short page, rows under `data`."""

PAGE_SHORT_BARE = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'short_page'},
}
"""GitHub's shape: `page`/`per_page`, the payload is the row collection, no count anywhere."""

PAGE_EMPTY_BARE = {
  'strategy': 'page',
  'index': {'parameter': 'page', 'start': 1},
  'size': {'parameter': 'pageSize'},
  'done': {'kind': 'empty'},
}


class TestPagedResponsePageExhausted:
  """`paged_response_method`'s `page`-strategy dispatch for a `short_page` or `empty`
  terminator -- the `page`/`per_page` shape most REST APIs use, GitHub's list endpoints
  being the motivating case. Before this, only a `page` walk with a declared `total` got
  the awaitable `PaginatedResponse` shape; these fell back to a plain async generator.
  """

  def test_generates_a_paginatedresponse_shaped_wrapper(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT_BARE),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='dict',
    ) or ''
    assert source.startswith('def orders_paged(')
    assert '-> PaginatedResponse[dict, int]:' in source
    assert 'page:' not in source.split('"""')[0]

  def test_short_page_ends_the_walk_on_the_declared_collection(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT_ROWS),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='int',
    ) or ''
    yielded, calls = walk_response(source, [{'data': [1, 2]}, {'data': [3]}], page_size=2)
    assert yielded == [[1, 2], [3]]
    assert [call['page'] for call in calls] == [1, 2]

  def test_short_page_measures_the_payload_when_no_collection_is_named(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT_BARE),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='int',
    ) or ''
    yielded, calls = walk_response(source, [[1, 2, 3], [4, 5, 6], [7]], page_size=3)
    assert yielded == [[1, 2, 3], [4, 5, 6], [7]]
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_empty_page_ends_the_walk_and_is_not_yielded(self, generator: Generator):
    """An exact multiple of the page size costs one extra request; the empty page that
    answers it is the terminator, not a page."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_EMPTY_BARE),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='int',
    ) or ''
    yielded, calls = walk_response(source, [[1, 2], [3, 4], []], page_size=2)
    assert yielded == [[1, 2], [3, 4]]
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_omitted_size_ends_only_on_an_empty_page(self, generator: Generator):
    """With no size sent, nothing is short: the API's own default page size is unknown to
    the walk, so only an empty page can end it."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT_BARE),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='int',
    ) or ''
    yielded, calls = walk_response(source, [[1], [2], []])
    assert yielded == [[1], [2]]
    assert [call['page'] for call in calls] == [1, 2, 3]

  def test_documented_default_size_ends_the_walk_on_a_short_page(self, generator: Generator):
    """With `per_page` omitted but its documented default declared, a page shorter than
    that default is short (authoring rule 8), so the walk stops without an extra request."""
    source = generator.paged_response_method(
      paged_endpoint(PAGE_SHORT_BARE, size_default=3),
      method_name='orders', header=paged_header(kwargs=['page', 'page_size']),
      rows_type='int',
    ) or ''
    yielded, calls = walk_response(source, [[1, 2, 3], [4]])
    assert yielded == [[1, 2, 3], [4]]
    assert [call['page'] for call in calls] == [1, 2]

  def test_short_page_without_a_size_is_refused(self, generator: Generator):
    with pytest.raises(ValueError):
      generator.paged_response_method(
        paged_endpoint({
          'strategy': 'page',
          'index': {'parameter': 'page', 'start': 1},
          'done': {'kind': 'short_page'},
        }),
        method_name='orders', header=paged_header(kwargs=['page']),
        rows_type='int',
      )


class TestPagedResponseSeek:
  """`paged_response_method`'s plain `seek`-strategy dispatch (`docs/TODO.md` T12) --
  kucoin's `account.hf_ledgers`/`account.margin_hf_ledgers` shape (S24,
  `docs/production_standards.md`): a `PaginatedResponse`-shaped wrapper whose cursor comes
  from `read_last_row` instead of a declared response field."""

  def test_generates_a_paginatedresponse_shaped_wrapper(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(SEEK_LAST_ID),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      rows_type='dict',
    ) or ''
    assert source.startswith('def orders_paged(')
    assert '-> PaginatedResponse[dict, int]:' in source
    assert 'from_id:' not in source.split('"""')[0]

  def test_walks_by_the_previous_page_last_row_cursor(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(SEEK_LAST_ID),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      rows_type='dict',
    ) or ''
    yielded, calls = walk_response(source, [
      [{'id': 1}, {'id': 2}], [{'id': 3}],
    ], limit=2)
    assert [call['from_id'] for call in calls] == [None, 2]
    assert yielded == [[{'id': 1}, {'id': 2}], [{'id': 3}]]

  def test_cursor_seed_is_coerced_to_none_on_the_first_call(self, generator: Generator):
    """The seeded `0` cursor -- `int`'s own zero value -- must not reach the wire as a
    literal, wrong `fromId=0` on page one, same reasoning as the token case."""
    source = generator.paged_response_method(
      paged_endpoint(SEEK_LAST_ID),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      rows_type='dict',
    ) or ''
    assert 'from_id=(from_id or None)' in source

  def test_stops_on_an_empty_page(self, generator: Generator):
    """`PaginatedResponse.__aiter__` only yields a truthy page (`if page: yield page`), so
    the terminal empty page still triggers a second call -- proving the cursor advanced
    and the walk actually stopped there -- but is never itself yielded."""
    source = generator.paged_response_method(
      paged_endpoint({**SEEK_LAST_ID, 'done': {'kind': 'empty'}}),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      rows_type='dict',
    ) or ''
    yielded, calls = walk_response(source, [[{'id': 1}], []])
    assert yielded == [[{'id': 1}]]
    assert [call['from_id'] for call in calls] == [None, 1]

  def test_stops_when_no_row_carries_the_cursor_field(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint({**SEEK_LAST_ID, 'done': {'kind': 'empty'}}),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      rows_type='dict',
    ) or ''
    yielded, _ = walk_response(source, [[{'name': 'a'}]])
    assert yielded == [[{'name': 'a'}]]

  def test_reads_a_declared_row_collection(self, generator: Generator):
    """kucoin's `ledgers_trade_hf`/`ledgers_margin_hf` shape has no wrapper (`done.rows`
    unset), but a wrapped payload (mexc's `historical_trades`-style envelope) works the
    same way `paged_method`'s own plain-generator seek path already does."""
    source = generator.paged_response_method(
      paged_endpoint({
        'strategy': 'seek',
        'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
        'done': {'kind': 'empty', 'rows': 'data'},
      }),
      method_name='orders', header=paged_header(kwargs=['from_id']),
      rows_type='dict',
    ) or ''
    yielded, calls = walk_response(source, [
      {'data': [{'id': 1}, {'id': 2}]}, {'data': []},
    ])
    assert [call['from_id'] for call in calls] == [None, 2]
    assert yielded == [[{'id': 1}, {'id': 2}]]

  def test_overlap_is_refused(self, generator: Generator):
    """`overlap` needs `paged_overlap_seek`'s own bespoke walk, not this one -- see
    `TestPagedOverlapSeek` below."""
    with pytest.raises(ValueError, match='overlap'):
      generator.paged_response_method(
        paged_endpoint(SEEK_OVERLAP_TIME),
        method_name='orders', header=overlap_header(),
        rows_type='dict',
      )

  def test_missing_driver_parameter_is_refused(self, generator: Generator):
    with pytest.raises(ValueError, match='from_id'):
      generator.paged_response_method(
        paged_endpoint(SEEK_LAST_ID),
        method_name='orders', header=paged_header(kwargs=['limit']),
        rows_type='dict',
      )

  def test_non_scalar_cursor_type_is_refused(self, generator: Generator):
    header = Function(name='orders', asyn=True, method=True, return_type='Orders')
    header.kwargs = [Function.Param(name='from_id', type='SomeAlias', required=False)]
    with pytest.raises(ValueError, match='SomeAlias'):
      generator.paged_response_method(
        paged_endpoint(SEEK_LAST_ID), method_name='orders', header=header, rows_type='dict',
      )

  def test_short_page_without_a_size_is_refused(self, generator: Generator):
    with pytest.raises(ValueError):
      generator.paged_response_method(
        paged_endpoint({
          'strategy': 'seek',
          'cursor': {'parameter': 'from_id', 'from': '[-1].id'},
          'done': {'kind': 'short_page'},
        }),
        method_name='orders', header=paged_header(kwargs=['from_id']),
        rows_type='dict',
      )


SEEK_REQUIRED_TIMESTAMP = {
  'strategy': 'seek',
  'cursor': {'parameter': 'start_time', 'from': '[-1].time'},
  'done': {'kind': 'empty'},
}
"""deribit's real `market_data.get_mark_price_history`/`get_funding_rate_history` shape,
after their conversion onto `seek`: a required, non-scalar (`TimestampMillis`) cursor with
no server-side default -- Fix 3's own motivating case. Uses `timestamp_millis_header`'s
existing `start_time`/`TimestampMillis` fixture (below), required by default."""


class TestPagedResponseSeekRequiredCursor:
  """Fix 3 (`docs/pagination.md`): a required, non-scalar `seek` cursor now also qualifies
  for the `PaginatedResponse`-shaped wrapper -- deribit's `get_mark_price_history`/
  `get_funding_rate_history`. Before this, `TimestampMillis` has no entry in
  `_SCALAR_ZERO_VALUES` at all, so `paged_response_seek` raised `ValueError` outright
  rather than falling back to anything."""

  def test_timestamp_millis_genuinely_has_no_scalar_zero_value(self, generator: Generator):
    """Confirms the fix is real, not a scalar coincidence: this type was never in the
    lookup `paged_response_seek` used to seed from before Fix 3."""
    from truewire.codegen.python import _SCALAR_ZERO_VALUES
    assert 'TimestampMillis' not in _SCALAR_ZERO_VALUES

  def test_previously_raised_valueerror(self, generator: Generator):
    """Regression guard the other way: an *optional* `TimestampMillis` cursor (no
    server-side default to render required, but still non-scalar) must still raise --
    Fix 3 only ever changes behavior for a `required` cursor, never a merely non-scalar
    one, which genuinely has no zero value to seed from."""
    with pytest.raises(ValueError, match='TimestampMillis'):
      generator.paged_response_method(
        paged_endpoint(SEEK_REQUIRED_TIMESTAMP),
        method_name='orders', header=timestamp_millis_header(required=False),
        rows_type='dict',
      )

  def test_generates_a_paginatedresponse_shaped_wrapper(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(SEEK_REQUIRED_TIMESTAMP),
      method_name='orders', header=timestamp_millis_header(), rows_type='dict',
    ) or ''
    assert source.startswith('def orders_paged(')
    assert '-> PaginatedResponse[dict, TimestampMillis]:' in source

  def test_keeps_the_cursor_required_on_the_outer_signature(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(SEEK_REQUIRED_TIMESTAMP),
      method_name='orders', header=timestamp_millis_header(), rows_type='dict',
    ) or ''
    signature = source.split('\n"""')[0]
    assert 'start_time: TimestampMillis' in signature
    assert 'start_time: TimestampMillis | None' not in signature

  def test_seeds_from_the_callers_own_argument_not_a_zero_value(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(SEEK_REQUIRED_TIMESTAMP),
      method_name='orders', header=timestamp_millis_header(), rows_type='dict',
    ) or ''
    assert 'start_time=start_time' in source
    assert 'start_time=(start_time or None)' not in source
    assert 'return PaginatedResponse(start_time, next)' in source

  def test_no_constructor_call_on_the_rendered_cursor_type(self, generator: Generator):
    """The real bug Fix 3 exposed and had to fix alongside it: `read_last_row`'s own
    `cast` must not blindly call a non-scalar rendered type as a constructor
    (`TimestampMillis(...)` raises `TypeError: Annotated cannot be instantiated`)."""
    source = generator.paged_response_method(
      paged_endpoint(SEEK_REQUIRED_TIMESTAMP),
      method_name='orders', header=timestamp_millis_header(), rows_type='dict',
    ) or ''
    assert 'TimestampMillis(' not in source

  def test_walks_by_the_previous_page_last_row_cursor(self, generator: Generator):
    source = generator.paged_response_method(
      paged_endpoint(SEEK_REQUIRED_TIMESTAMP),
      method_name='orders', header=timestamp_millis_header(), rows_type='dict',
    ) or ''
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    later = datetime.fromtimestamp(2, tz=timezone.utc)
    yielded, calls = walk_response(
      source, [[{'time': later}, {'time': later}], []],
      extra_namespace={'TimestampMillis': datetime}, start_time=start,
    )
    assert [call['start_time'] for call in calls] == [start, later]
    assert yielded == [[{'time': later}, {'time': later}]]


def overlap_header(*, required: bool = True) -> Function:
  """Header shaped like hyperliquid's `funding_history`: a numeric cursor the caller must
  always supply, since an overlap walk has nowhere else to seed its first request from."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [
    Function.Param(name='start_time', type='int', required=required),
    Function.Param(name='validate', type='bool | None', default='None'),
  ]
  header.return_type = 'Orders'
  return header

SEEK_OVERLAP_TIME = {
  'strategy': 'seek',
  'cursor': {'parameter': 'start_time', 'from': '[-1].time'},
  'done': {'kind': 'empty'},
  'overlap': {'cap': 5},
}
"""hyperliquid's shape: `startTime` re-sent as the *largest* `time` collected so far, not a
plain top-level field or a unique-per-row id -- `hyperliquid.info.pagination.paginate()`'s
hand-written reference, generalized."""


class TestRowFieldExpression:
  """`Generator.row_field_expression`: reading a `docs/pagination.md` §3 dotted-key/
  bracket-index field off one row, tolerant of a missing key or an out-of-range index at
  any level -- executing the emitted expression is the only way to show it doesn't crash
  on the tolerated cases, the same reasoning `walk` itself is built on."""

  def test_a_plain_key(self, generator: Generator):
    expr = generator.row_field_expression('row', 'id')
    assert eval(expr, {'row': {'id': 7}}) == 7

  def test_a_dotted_key(self, generator: Generator):
    expr = generator.row_field_expression('row', 'trade.id')
    assert eval(expr, {'row': {'trade': {'id': 7}}}) == 7

  def test_a_positive_index(self, generator: Generator):
    """binance's candle shape: open time at tuple position 0."""
    expr = generator.row_field_expression('row', '[0]')
    assert eval(expr, {'row': [1690000000000, '100.0']}) == 1690000000000

  def test_a_negative_index(self, generator: Generator):
    expr = generator.row_field_expression('row', '[-1]')
    assert eval(expr, {'row': [1690000000000, '100.0']}) == '100.0'

  def test_composed_index_then_key(self, generator: Generator):
    expr = generator.row_field_expression('row', '[0].id')
    assert eval(expr, {'row': [{'id': 7}, {'id': 8}]}) == 7

  def test_composed_key_then_index(self, generator: Generator):
    expr = generator.row_field_expression('row', 'candle[0]')
    assert eval(expr, {'row': {'candle': [1690000000000, '100.0']}}) == 1690000000000

  def test_missing_key_is_none_not_a_crash(self, generator: Generator):
    expr = generator.row_field_expression('row', 'id')
    assert eval(expr, {'row': {}}) is None

  def test_out_of_range_positive_index_is_none_not_a_crash(self, generator: Generator):
    expr = generator.row_field_expression('row', '[5]')
    assert eval(expr, {'row': [1, 2]}) is None

  def test_out_of_range_negative_index_is_none_not_a_crash(self, generator: Generator):
    expr = generator.row_field_expression('row', '[-5]')
    assert eval(expr, {'row': [1, 2]}) is None

  def test_indexing_a_none_row_is_none_not_a_crash(self, generator: Generator):
    expr = generator.row_field_expression('row', '[0]')
    assert eval(expr, {'row': None}) is None


class TestPagedOverlapSeek:
  """`pagination.overlap`: a `seek` cursor field that is not unique per row."""

  def test_undeclared_overlap_uses_the_generic_seek_template(self, generator: Generator):
    """No `overlap` block still gets `paged_method`'s plain `seek` path, unchanged."""
    source = generator.paged_method(
      paged_endpoint(SEEK_LAST_ID),
      method_name='orders', header=paged_header(kwargs=['from_id', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'overlap' not in source

  def test_cursor_starts_from_the_callers_own_value(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    _, calls = walk(
      source, [[{'time': 1}, {'time': 1}, {'time': 2}], [{'time': 2}, {'time': 3}], []],
      start_time=0,
    )
    assert [call['start_time'] for call in calls] == [0, 2, 3]

  def test_rows_sharing_the_cursor_value_are_dropped_from_the_next_page_by_position(
    self, generator: Generator,
  ):
    """The dedup only `overlap` adds: the plain `seek` template would re-yield `{'time': 2}`."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    yielded, _ = walk(
      source, [[{'time': 1}, {'time': 1}, {'time': 2}], [{'time': 2}, {'time': 3}], []],
      start_time=0,
    )
    assert yielded == [[{'time': 1}, {'time': 1}, {'time': 2}], [{'time': 3}]]

  def test_advances_to_the_largest_field_value_not_merely_the_last_row(
    self, generator: Generator,
  ):
    """Nothing guarantees the trailing row of a page carries the largest value."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    _, calls = walk(source, [[{'time': 3}, {'time': 1}], []], start_time=0)
    assert [call['start_time'] for call in calls] == [0, 3]

  def test_empty_page_ends_the_walk_without_checking_the_prefix(self, generator: Generator):
    """A truly empty page is an unambiguous stop, even with rows still held as overlap."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    yielded, calls = walk(source, [[{'time': 1}], []], start_time=0)
    assert len(yielded) == 1
    assert len(calls) == 2

  def test_a_broken_row_order_guarantee_raises(self, generator: Generator):
    """The venue returning a different set of rows for a cursor it already answered once."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [[{'time': 1}, {'time': 1}], [{'time': 2}]], start_time=0)

  def test_a_full_page_sharing_one_cursor_value_raises_instead_of_truncating(
    self, generator: Generator,
  ):
    """The one case the walk cannot resolve: it cannot tell whether more rows exist past a
    full page that never moved the cursor at all."""
    source = generator.paged_method(
      paged_endpoint({**SEEK_OVERLAP_TIME, 'overlap': {'cap': 2}}),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [[{'time': 5}, {'time': 5}]], start_time=5)

  def test_overlap_requires_the_cursor_parameter_to_exist(self, generator: Generator):
    with pytest.raises(ValueError):
      generator.paged_method(
        paged_endpoint(SEEK_OVERLAP_TIME),
        method_name='orders', header=paged_header(kwargs=['limit']),
        response_type='Orders',
      )


SEEK_OVERLAP_MILLIS = {
  'strategy': 'seek',
  'cursor': {'parameter': 'start_time', 'from': '[-1].time'},
  'done': {'kind': 'empty'},
  'overlap': {'cap': 5},
}
"""Same shape as `SEEK_OVERLAP_TIME`, but the cursor is timestamp-formatted -- hyperliquid's
real `funding_history`/`user_funding`/`user_non_funding_ledger_updates`/`user_fills_by_time`
shape, where the generated cursor is a `datetime`, not a plain `int`."""


def timestamp_millis_header(*, required: bool = True) -> Function:
  """As `overlap_header`, but the cursor renders as `TimestampMillis` (a `datetime`)."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [
    Function.Param(name='start_time', type='TimestampMillis', required=required),
    Function.Param(name='validate', type='bool | None', default='None'),
  ]
  header.return_type = 'Orders'
  return header


class _FakeTimestampMillis:
  """Stand-in for the real `EpochConverter` helper the generated import names by id --
  `.parse` mirrors the real contract: a raw wire epoch-millis `int` *or* numeral `str`
  in (`truewire_core.times.ms.EpochConverter.parse`'s own signature -- some venues, bitget's
  candles among them, send the epoch as a wire string), a `datetime` out."""

  @staticmethod
  def parse(value: 'int | str') -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


class TestPagedOverlapSeekTimestampCursor:
  """A `seek`+`overlap` cursor declared with a timestamp `format` (rule 3/S7) renders as a
  `datetime`, not a plain `int` -- the generic `cursor += 1` fallback is not arithmetic
  there. hyperliquid's own four time-cursor endpoints are the real, motivating case:
  originally a client-local override on its now-retired `codegen/python.py` backend,
  generalized here once hyperliquid's migration onto the universal `Generator` needed the
  identical fix with no backend left to hold it in."""

  def test_advances_by_one_tick_of_the_declared_unit_not_by_one(self, generator: Generator):
    generator.core_package = 'venue.core'
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_MILLIS),
      method_name='orders', header=timestamp_millis_header(),
      response_type='Orders',
    ) or ''
    assert 'timedelta(milliseconds=1)' in source
    assert 'cursor += 1' not in source

  def test_non_timestamp_cursor_is_unaffected(self, generator: Generator):
    """`SEEK_OVERLAP_TIME`'s plain `int` cursor still gets the bare `cursor += 1` fallback,
    with no `timedelta` import pulled in for it."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(),
      response_type='Orders',
    ) or ''
    assert 'timedelta' not in source
    assert 'cursor += 1' in source

  def test_walk_advances_correctly_against_raw_wire_ints(self, generator: Generator):
    """With validation off, a response's own timestamp field is still a raw wire int (a
    `TypedDict` annotation can't enforce it) -- the normalization has to make it compare
    correctly against the caller's own already-`datetime` cursor anyway (reverting the
    `row_field_expression` normalization reproduces a `TypeError: '>' not supported
    between instances of 'int' and 'datetime'` here)."""
    generator.core_package = 'venue.core'
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_MILLIS),
      method_name='orders', header=timestamp_millis_header(),
      response_type='Orders',
    ) or ''
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    _, calls = walk(
      source, [[{'time': 1000}, {'time': 2000}], [{'time': 2000}, {'time': 3000}], []],
      extra_namespace={
        'timestamp_millis': _FakeTimestampMillis, 'cast': cast, 'TimestampMillis': datetime,
      },
      start_time=start,
    )
    assert [call['start_time'] for call in calls] == [
      start,
      _FakeTimestampMillis.parse(2000),
      _FakeTimestampMillis.parse(3000),
    ]

  def test_paged_imports_add_timedelta_and_the_helper(self, generator: Generator):
    """`datetime` (the bare class), not just `timedelta`, is needed alongside the helper --
    `row_field_expression`'s own `isinstance(x, datetime)` discriminator (Fix 2) is
    emitted the moment this same timestamp-cursor condition is met."""
    imports = generator.paged_imports(
      paged_endpoint(SEEK_OVERLAP_MILLIS), header=timestamp_millis_header(),
    )
    assert imports.get('datetime') == {'timedelta', 'datetime'}
    assert imports.get('truewire_core.types') == {'timestamp_millis'}
    assert 'cast' in imports.get('typing_extensions', set())

  def test_non_timestamp_cursor_gets_no_extra_imports(self, generator: Generator):
    imports = generator.paged_imports(
      paged_endpoint(SEEK_OVERLAP_TIME), header=overlap_header(),
    )
    assert 'datetime' not in imports


SEEK_OVERLAP_TIME_ROWS = {**SEEK_OVERLAP_TIME, 'done': {'kind': 'empty', 'rows': 'list'}}
"""Same shape as `SEEK_OVERLAP_TIME`, but the payload is wrapped in an envelope -- Fix 1's
own confirmed-latent case for `paged_overlap_seek` (no real endpoint hits this yet, unlike
the `window` sibling `TestPagedWindowOverlapEnvelope` above, but the identical bug applied
equally to both)."""


class TestPagedOverlapSeekEnvelope:
  """Fix 1 (`docs/pagination.md`): `pagination.done.rows` on a `seek`+`overlap` walk --
  confirmed latent (`paged_overlap_seek` shares only `row_field_expression` with
  `paged_window_overlap`, not the row-collection handling itself), fixed identically."""

  def test_unwraps_the_declared_rows_field(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME_ROWS),
      method_name='orders', header=overlap_header(), response_type='ShouldNotAppear',
      overlap_rows_type='dict',
    ) or ''
    assert "response.get('list')" in source
    assert '-> AsyncIterator[list[dict]]:' in source
    assert 'ShouldNotAppear' not in source

  def test_undeclared_rows_reads_the_response_directly(self, generator: Generator):
    """`paged_rows` is a pure no-op when `done.rows` is unset -- confirmed by the absence
    of any row-extraction read (not by comparing full source against a version that also
    passes `overlap_rows_type`, an independent, caller-supplied concern orthogonal to
    whether `done.rows` is declared)."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME),
      method_name='orders', header=overlap_header(), response_type='Orders',
    ) or ''
    assert "response.get(" not in source

  def test_dedup_and_advance_against_a_wrapped_payload(self, generator: Generator):
    """The same dedup-by-position and largest-value-advance behavior
    `TestPagedOverlapSeek` already covers for a bare-array payload, now proven against an
    enveloped one."""
    source = generator.paged_method(
      paged_endpoint(SEEK_OVERLAP_TIME_ROWS),
      method_name='orders', header=overlap_header(), response_type='ShouldNotAppear',
      overlap_rows_type='dict',
    ) or ''
    yielded, calls = walk(
      source, [
        {'list': [{'time': 1}, {'time': 1}, {'time': 2}]},
        {'list': [{'time': 2}, {'time': 3}]},
        {'list': []},
      ],
      start_time=0,
    )
    assert yielded == [[{'time': 1}, {'time': 1}, {'time': 2}], [{'time': 3}]]
    assert [call['start_time'] for call in calls] == [0, 2, 3]

  def test_a_full_wrapped_page_sharing_one_value_raises(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({**SEEK_OVERLAP_TIME_ROWS, 'overlap': {'cap': 2}}),
      method_name='orders', header=overlap_header(), response_type='ShouldNotAppear',
      overlap_rows_type='dict',
    ) or ''
    with pytest.raises(LogicError):
      walk(source, [{'list': [{'time': 5}, {'time': 5}]}], start_time=5)


def test_an_integer_window_steps_by_a_plain_number():
  from truewire.codegen.python import Generator

  assert Generator().paged_step_expression(1, unit='ms', is_datetime=False) == '1'


def test_a_normalising_backend_gets_an_integer_step_not_a_timedelta():
  """Regression: `paged_step_expression` must read what `paged_window_bound` did, not
  the parameter's declared type — otherwise a backend that normalises a `Timestamp`
  bound to an integer before the loop still gets a `timedelta` step, and `int - timedelta`
  is a `TypeError` at runtime. This is exactly the shape of mexc's `paged_window_bound`
  override, reproduced here without depending on mexc's own backend.
  """
  from truewire.codegen.python import Generator
  from truewire.generation.python.code import Function

  class NormalisingBackend(Generator):
    """A generator that reduces every `TimestampMillis` bound to an integer before the loop."""
    def paged_window_bound(
      self, expression: str, *, unit: str, param: Function.Param,
    ) -> tuple[str, bool]:
      """Normalise a `TimestampMillis` bound to an integer; every other bound is untouched."""
      if param.type != 'TimestampMillis':
        return expression, False
      return f'dump_{unit}({expression})', False

  header = paged_header(kwargs=['start', 'end', 'limit'])
  header.kwargs[0] = Function.Param(name='start', type='TimestampMillis', required=False)
  header.kwargs[1] = Function.Param(name='end', type='TimestampMillis', required=False)
  source = NormalisingBackend().paged_method(
    paged_endpoint(WINDOW_DESCENDING),
    method_name='orders', header=header, response_type='Orders',
  ) or ''
  assert 'upper = lower - 1' in source
  assert 'timedelta' not in source

  source = Generator().paged_method(
    paged_endpoint(WINDOW_DESCENDING),
    method_name='orders', header=header, response_type='Orders',
  ) or ''
  assert 'upper = lower - timedelta(milliseconds=1)' in source


def window_overlap_header(*, kwargs: tuple = ('start', 'end', 'limit')) -> Function:
  """Header for a `window`+`overlap` endpoint -- a page IS its own row collection
  (`list[dict]`), per `overlap`'s own restriction that the payload be one."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [Function.Param(name=name, type='int', required=False) for name in kwargs]
  header.kwargs.append(Function.Param(name='validate', type='bool | None', default='None'))
  header.return_type = 'list[dict]'
  return header


WINDOW_OVERLAP = {
  'strategy': 'window',
  'bound': {'start': 'start', 'end': 'end'},
  'order': 'ascending',
  'step': {'unit': 'ms', 'size': 1},
  'size': {'parameter': 'limit'},
  'overlap': {'field': '[-1][0]'},
  'done': {'kind': 'empty'},
}
"""binance's candle shape: the row's own open time sits at tuple position 0. No declared
`chunk` -- `Δt` defaults to the caller's own `end - start`, degrading to a single chunk
with narrow-and-retry instead of `docs/pagination.md` §5's old unconditional raise."""

WINDOW_OVERLAP_CHUNK = {
  **WINDOW_OVERLAP,
  'overlap': {'field': '[-1][0]', 'chunk': {'parameter': 'chunk_span', 'default': 5}},
}
"""Same shape, with a declared chunk width -- genuine multi-chunk coverage across a wide
caller range, rather than one all-or-nothing request."""


def dense_rows(start: int, end: int, *, limit: int, step: int = 1) -> list[list]:
  """Every multiple of `step` in `[start, end]`, capped at `limit` -- a stand-in row
  collection dense enough to force narrow-and-retry within a single chunk."""
  rows = [[t, str(t)] for t in range(start, end + 1, step)]
  return rows[:limit]


class TestPagedWindowOverlap:
  """`pagination.overlap` on a `window` walk: a per-row timestamp field that isn't
  unique per chunk -- `docs/pagination.md` §5."""

  def test_undeclared_overlap_uses_the_plain_window_template(self, generator: Generator):
    """No `overlap` block still gets `paged_window`'s plain truncating template."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_DESCENDING), method_name='orders',
      header=paged_header(kwargs=['start', 'end', 'limit']), response_type='Orders',
    ) or ''
    assert 'overlap' not in source

  def test_dispatches_to_the_overlap_template(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    assert source.startswith('async def orders_paged(')
    assert 'overlap' in source

  def test_narrows_and_retries_a_full_chunk_with_real_variety(self, generator: Generator):
    """A chunk denser than `limit` narrows to the largest value seen and retries, rather
    than raising -- the behavior change this task's own test is meant to pin, replacing
    the old unconditional raise."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    yielded, calls = walk_window_overlap(
      source, lambda start, end, limit: dense_rows(start, end, limit=limit or 3),
      start=0, end=20,
    )
    values = [row[0] for row in yielded]
    assert values == list(range(21))
    assert min(call['start'] for call in calls) >= 0
    assert max(call['end'] for call in calls) <= 20

  def test_never_requests_outside_the_callers_own_bounds(self, generator: Generator):
    """The property the original window-truncation fix guarantees, and this redesign
    must never regress -- checked across every narrow-and-retry/chunk-transition
    iteration, not just the first request."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP_CHUNK, size_default=3),
      method_name='orders', header=window_overlap_header(),
      response_type='list[dict]',
    ) or ''
    _, calls = walk_window_overlap(
      source, lambda start, end, limit: dense_rows(start, end, limit=limit or 3),
      start=0, end=20,
    )
    assert all(call['start'] >= 0 for call in calls)
    assert all(call['end'] <= 20 for call in calls)

  def test_a_declared_chunk_produces_genuine_multi_chunk_coverage(self, generator: Generator):
    """`Δt` (`overlap.chunk`) turns a wide caller range into several real requests, each
    spanning roughly `chunk_span` -- not one all-or-nothing call."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP_CHUNK, size_default=3),
      method_name='orders', header=window_overlap_header(),
      response_type='list[dict]',
    ) or ''
    yielded, calls = walk_window_overlap(
      source, lambda start, end, limit: dense_rows(start, end, limit=limit or 3, step=3),
      start=0, end=20,
    )
    values = [row[0] for row in yielded]
    assert values == [t for t in range(0, 21, 3)]
    assert len(calls) > 1
    assert min(call['start'] for call in calls) >= 0
    assert max(call['end'] for call in calls) <= 20

  def test_an_explicit_chunk_override_is_still_capped_at_the_callers_bound(
    self, generator: Generator,
  ):
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP_CHUNK, size_default=3),
      method_name='orders', header=window_overlap_header(),
      response_type='list[dict]',
    ) or ''
    _, calls = walk_window_overlap(
      source, lambda start, end, limit: dense_rows(start, end, limit=limit or 3),
      start=0, end=20, chunk_span=1000,
    )
    assert calls[0] == {'start': 0, 'end': 20, 'limit': None, 'validate': None}

  def test_degrades_to_a_single_chunk_when_undeclared(self, generator: Generator):
    """No declared `chunk` -- `Δt` is the caller's own `end - start`, so the very first
    request already spans the whole range: one chunk, narrow-and-retry within it."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    _, calls = walk_window_overlap(
      source, lambda start, end, limit: dense_rows(start, end, limit=limit or 3),
      start=0, end=20,
    )
    assert calls[0] == {'start': 0, 'end': 20, 'limit': None, 'validate': None}

  def test_a_full_chunk_sharing_one_value_raises(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    def stuck(start: int, end: int, limit: int | None) -> list[list]:
      return [[start, 'a'], [start, 'b'], [start, 'c']]
    with pytest.raises(LogicError):
      walk_window_overlap(source, stuck, start=0, end=20)

  def test_a_broken_row_order_guarantee_raises(self, generator: Generator):
    """A chunk that narrows and retries expects the previously-seen rows back as an exact
    prefix of the next response -- a venue that reorders rows across requests breaks
    that guarantee."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    calls_made = {'n': 0}
    def unstable(start: int, end: int, limit: int | None) -> list[list]:
      calls_made['n'] += 1
      if calls_made['n'] == 1:
        return [[0, 'a'], [0, 'b'], [5, 'c']]
      return [[6, 'z'], [7, 'w']]
    with pytest.raises(LogicError):
      walk_window_overlap(source, unstable, start=0, end=20)

  def test_descending_order_walks_backwards_and_stays_bounded(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint({**WINDOW_OVERLAP, 'order': 'descending'}, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    def descending_rows(start: int, end: int, limit: int | None) -> list[list]:
      rows = [[t, str(t)] for t in range(end, start - 1, -1)]
      return rows[: (limit or 3)]
    yielded, calls = walk_window_overlap(source, descending_rows, start=0, end=20)
    values = [row[0] for row in yielded]
    assert values == list(range(20, -1, -1))
    assert min(call['start'] for call in calls) >= 0
    assert max(call['end'] for call in calls) <= 20


def walk_window_overlap(
  source: str, responder, *,
  extra_namespace: dict[str, Any] | None = None, **call: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
  """Run a generated `window`+`overlap` iterator against a row-generating callable,
  returning every row yielded (flattened across chunks) and the keyword arguments each
  underlying call made.

  Unlike `walk`, the underlying single-request method's response depends on the request
  bounds it was called with (a real venue would answer differently per window), so this
  takes a `responder(start, end, limit) -> list[list]` callable instead of a canned list
  of pages.

  Args:
    source: Generated `<method>_paged` source, unindented.
    responder: Computes the page a call with these bounds/limit would return.
    extra_namespace: Additional names the generated source needs at exec time -- a
      timestamp-typed bound's own `datetime`/`timedelta`/helper/`cast` names, which the
      default namespace has no reason to carry for every other test. See `walk`.
    call: Arguments passed to the iterator.
  """
  namespace: dict[str, Any] = {
    'AsyncIterator': AsyncIterator, 'LogicError': LogicError,
    'datetime': datetime, 'timedelta': timedelta,
    **(extra_namespace or {}),
  }
  code = '\n'.join([
    'class Walk:',
    '  """Stub endpoint class the generated iterator is mixed into."""',
    '  def __init__(self, responder):',
    '    self.responder = responder',
    '    self.calls = []',
    '  async def orders(self, *args, **kwargs):',
    '    self.calls.append(kwargs)',
    '    return self.responder(kwargs["start"], kwargs["end"], kwargs.get("limit"))',
    *(f'  {line}' if line else '' for line in source.splitlines()),
  ])
  exec(code, namespace)
  instance = namespace['Walk'](responder)

  async def collect() -> list[Any]:
    out: list[Any] = []
    async for page in getattr(instance, 'orders_paged')(**call):
      out.extend(page)
    return out

  return asyncio.run(collect()), instance.calls

WINDOW_OVERLAP_ROWS = {**WINDOW_OVERLAP, 'done': {'kind': 'empty', 'rows': 'list'}}
"""Same shape as `WINDOW_OVERLAP`, but the payload is wrapped in an envelope -- bybit's
`{category, symbol, list}`, kucoin's `{dataList, hasMore}`, coinbase's `{candles: [...]}`
-- confirmed real, otherwise-qualifying endpoints Fix 1 unblocks."""


class TestPagedWindowOverlapEnvelope:
  """Fix 1 (`docs/pagination.md`): `pagination.done.rows` on a `window`+`overlap` walk --
  the payload is a wrapper, not the row collection itself. Before this, every
  prefix-check/dedup/cap/value-extraction step read the *envelope* directly, so a wrapped
  endpoint either crashed (indexing a dict) or silently misbehaved."""

  def test_unwraps_the_declared_rows_field(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP_ROWS, size_default=3),
      method_name='orders', header=window_overlap_header(),
      response_type='ShouldNotAppear', overlap_rows_type='dict',
    ) or ''
    assert "response.get('list')" in source
    assert '-> AsyncIterator[list[dict]]:' in source
    assert 'ShouldNotAppear' not in source

  def test_undeclared_rows_reads_the_response_directly(self, generator: Generator):
    """`paged_rows` is a pure no-op when `done.rows` is unset -- confirmed by the absence
    of any row-extraction read. `WINDOW_OVERLAP`'s own existing tests (above) already
    confirm the full generated source is unaffected by this task's change for every
    currently-shipped bare-array endpoint (binance's klines, mexc's candles)."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_header(), response_type='list[dict]',
    ) or ''
    assert "response.get(" not in source

  def test_narrows_and_retries_against_a_wrapped_payload(self, generator: Generator):
    """The same dense-row narrow-and-retry behavior `TestPagedWindowOverlap` already
    covers for a bare-array payload, now proven against an enveloped one."""
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP_ROWS, size_default=3),
      method_name='orders', header=window_overlap_header(),
      response_type='ShouldNotAppear', overlap_rows_type='dict',
    ) or ''
    def wrapped(start: int, end: int, limit: int | None) -> dict:
      return {'list': dense_rows(start, end, limit=limit or 3)}
    yielded, calls = walk_window_overlap(source, wrapped, start=0, end=20)
    values = [row[0] for row in yielded]
    assert values == list(range(21))
    assert min(call['start'] for call in calls) >= 0
    assert max(call['end'] for call in calls) <= 20

  def test_a_full_wrapped_chunk_sharing_one_value_raises(self, generator: Generator):
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP_ROWS, size_default=3),
      method_name='orders', header=window_overlap_header(),
      response_type='ShouldNotAppear', overlap_rows_type='dict',
    ) or ''
    def stuck(start: int, end: int, limit: int | None) -> dict:
      return {'list': [[start, 'a'], [start, 'b'], [start, 'c']]}
    with pytest.raises(LogicError):
      walk_window_overlap(source, stuck, start=0, end=20)


def window_overlap_timestamp_header() -> Function:
  """As `window_overlap_header`, but the bounds render as `TimestampMillis` (a `datetime`)
  -- bitget's real candle shape."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [
    Function.Param(name='start', type='TimestampMillis', required=False),
    Function.Param(name='end', type='TimestampMillis', required=False),
    Function.Param(name='limit', type='int', required=False),
    Function.Param(name='validate', type='bool | None', default='None'),
  ]
  header.return_type = 'list[dict]'
  return header


class TestPagedWindowOverlapWireStringTimestamp:
  """Fix 2 (`docs/pagination.md`): a wire-*string* timestamp field (bitget's candles,
  confirmed live) must still be parsed for comparison against an already-`datetime`
  bound. The old discriminator (`isinstance(x, int)`) fell through unparsed for a wire
  string and raised `TypeError` comparing `str > datetime` -- this is why bitget withheld
  `overlap` on its 7 candle endpoints until this fix landed."""

  def test_generated_discriminator_checks_datetime_not_int(self, generator: Generator):
    generator.core_package = 'venue.core'
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_timestamp_header(),
      response_type='list[dict]',
    ) or ''
    assert 'not isinstance(' in source
    assert ', datetime)' in source

  def test_wire_string_rows_are_parsed_and_compared_correctly(self, generator: Generator):
    """Reproduces bitget's real shape: every row's own timestamp comes back as a JSON
    *string* (`\"0\"`, `\"3\"`, ...), never a raw `int` -- the old discriminator would
    leave it as a bare string and blow up comparing it against the already-`datetime`
    walk position with `TypeError: '>' not supported between instances of 'str' and
    'datetime'`."""
    generator.core_package = 'venue.core'
    source = generator.paged_method(
      paged_endpoint(WINDOW_OVERLAP, size_default=3),
      method_name='orders', header=window_overlap_timestamp_header(),
      response_type='list[dict]',
    ) or ''

    def wire_string_rows(start: datetime, end: datetime, limit: int | None) -> list[list]:
      # Row values are wire epoch-*milliseconds* strings (bitget's real shape,
      # `_FakeTimestampMillis.parse`'s own contract) -- inverting it recovers the same
      # small integer range `dense_rows` uses for the non-timestamp version of this test.
      lo = round(start.timestamp() * 1000)
      hi = round(end.timestamp() * 1000)
      return [[str(t), str(t)] for t in range(lo, hi + 1)][: (limit or 3)]

    start = _FakeTimestampMillis.parse(0)
    end = _FakeTimestampMillis.parse(20)
    yielded, calls = walk_window_overlap(
      source, wire_string_rows, start=start, end=end,
      extra_namespace={
        'timestamp_millis': _FakeTimestampMillis, 'cast': cast, 'TimestampMillis': datetime,
      },
    )
    values = [row[0] for row in yielded]
    assert values == [str(t) for t in range(21)]
    assert min(call['start'] for call in calls) == start
    assert max(call['end'] for call in calls) == end

TOKEN_NESTED = {
  'strategy': 'token',
  'cursor': {'parameter': 'pagination.key', 'from': 'pagination.next_key'},
  'size': {'parameter': 'pagination.limit'},
  'done': {'kind': 'absent_cursor'},
}
"""dYdX's shape: a gRPC unary request wrapping Cosmos-SDK pagination in one nested
`pagination: PageRequest | None` message parameter -- `key`/`limit` are fields of that
message, not flat top-level parameters of the single-request method."""


def nested_header() -> Function:
  """Build the header a grpc backend would hand `paged_method` for `TOKEN_NESTED`: one
  `pagination: PageRequest | None` parameter, not flat `key`/`limit` ones."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [Function.Param(name='pagination', type='PageRequest', required=False)]
  header.return_type = 'Orders'
  return header


class FakePageRequest:
  """Stand-in for a betterproto2-generated nested pagination message, for `walk()`."""

  def __init__(self, *, key: bytes = b'', limit: int | None = None):
    self.key = key
    self.limit = limit

  def __repr__(self) -> str:
    return f'FakePageRequest(key={self.key!r}, limit={self.limit!r})'


class TestPagedMethodNestedPagination:
  """A `token` pagination whose cursor/size are dotted paths into one nested
  request-message parameter (dYdX's Cosmos-SDK-style `PageRequest`) generates and
  executes the same as the flat case, once a backend supplies `nested_fields`."""

  NESTED_FIELDS = {'pagination': {'key': 'bytes', 'limit': 'int'}}

  def test_flat_token_pagination_is_unaffected_by_the_new_parameter(
    self, generator: Generator,
  ):
    """`nested_fields` defaulting to None -- every existing client's call sites --
    leaves the ordinary flat case completely unchanged."""
    source = generator.paged_method(
      paged_endpoint(TOKEN_CURSOR),
      method_name='orders', header=paged_header(kwargs=['cursor', 'limit']),
      response_type='Orders',
    ) or ''
    assert 'async def orders_paged(' in source
    assert 'cursor:' not in source.split(') -> AsyncIterator[Orders]:')[0]

  def test_nested_driver_leaves_the_outer_parameter_out_of_the_signature(
    self, generator: Generator,
  ):
    source = generator.paged_method(
      paged_endpoint(TOKEN_NESTED),
      method_name='orders', header=nested_header(), response_type='Orders',
      nested_fields=self.NESTED_FIELDS,
    ) or ''
    # Isolated to the parameter list only -- both the docstring prose ("Passes each
    # page's token back as `key`") and the loop body ("key: bytes | None = None")
    # legitimately mention these names; only the *signature* must not.
    signature = source.split(') -> AsyncIterator[Orders]:')[0]
    assert 'pagination' not in signature
    assert 'key' not in signature
    assert 'limit: int | None = None' in signature

  def test_nested_driver_walks_and_reconstructs_the_message_each_call(
    self, generator: Generator,
  ):
    source = generator.paged_method(
      paged_endpoint(TOKEN_NESTED),
      method_name='orders', header=nested_header(), response_type='Orders',
      nested_fields=self.NESTED_FIELDS,
    ) or ''
    pages = [
      {'pagination': {'next_key': b'page2'}},
      {'pagination': {'next_key': None}},
    ]
    yielded, calls = walk(
      source, pages, extra_namespace={'PageRequest': FakePageRequest}, limit=5,
    )
    assert len(yielded) == 2
    assert list(calls[0].keys()) == ['pagination']
    assert isinstance(calls[0]['pagination'], FakePageRequest)
    # The loop-local seeds `None` on the first call (matching the flat case's own
    # convention), but `key`'s declared type in NESTED_FIELDS is bare `bytes`, not
    # `bytes | None` -- betterproto2's real constructor never accepts `None` for a
    # field like this (it uses default_factory, not Optional) -- so the generated call
    # must coerce `None` to the field's own zero value (`b''`) before construction,
    # not pass it through. This is exactly the bug pyright caught on the real generated
    # code that this synthetic FakePageRequest, with no runtime type enforcement of its
    # own, would otherwise silently let through.
    assert calls[0]['pagination'].key == b''
    assert calls[0]['pagination'].limit == 5
    assert calls[1]['pagination'].key == b'page2'
    assert calls[1]['pagination'].limit == 5

  def test_genuinely_optional_nested_field_is_passed_through_uncoerced(
    self, generator: Generator,
  ):
    """A nested field the message itself declares `X | None` needs no zero-value
    coercion -- its real constructor already accepts `None` -- unlike a bare-typed one
    like `key`/`limit`, which never does."""
    source = generator.paged_method(
      paged_endpoint(TOKEN_NESTED),
      method_name='orders', header=nested_header(), response_type='Orders',
      nested_fields={'pagination': {'key': 'bytes | None', 'limit': 'int'}},
    ) or ''
    assert 'key=key,' in source or 'key=key)' in source
    assert 'if key is not None else' not in source

  def test_unsupported_nested_field_type_raises(self, generator: Generator):
    """A nested field typed something other than a basic scalar has no known zero
    value to substitute -- raising beats silently emitting `x if x is not None else
    <nothing>`, which is what a blind string-format would otherwise produce."""
    with pytest.raises(ValueError, match='SomeEnum'):
      generator.paged_method(
        paged_endpoint(TOKEN_NESTED),
        method_name='orders', header=nested_header(), response_type='Orders',
        nested_fields={'pagination': {'key': 'SomeEnum', 'limit': 'int'}},
      )

  def test_unknown_outer_parameter_raises(self, generator: Generator):
    header = Function(name='orders', asyn=True, method=True)
    header.kwargs = [Function.Param(name='address', type='str')]
    with pytest.raises(ValueError, match='pagination'):
      generator.paged_method(
        paged_endpoint(TOKEN_NESTED),
        method_name='orders', header=header, response_type='Orders',
        nested_fields=self.NESTED_FIELDS,
      )

  def test_unknown_nested_field_raises(self, generator: Generator):
    with pytest.raises(ValueError, match='key'):
      generator.paged_method(
        paged_endpoint(TOKEN_NESTED),
        method_name='orders', header=nested_header(), response_type='Orders',
        nested_fields={'pagination': {'limit': 'int'}},
      )


PAGE_NESTED = {
  'strategy': 'page',
  'index': {'parameter': 'pagination.page', 'start': 1},
  'size': {'parameter': 'pagination.limit'},
  'done': {'kind': 'short_page'},
}
"""bitget's shape: a REST POST body bundles `page`/`limit` inside one JSON object
parameter rather than exposing them as flat query-role parameters -- same "the driver
isn't a flat parameter" problem `TOKEN_NESTED` has for a gRPC message, one layer down
(a JSON object field instead of a protobuf message field)."""


def page_nested_header() -> Function:
  """Build the header a REST backend would hand `paged_method` for `PAGE_NESTED`: one
  `pagination: PageParams` parameter, not flat `page`/`limit` ones."""
  header = Function(name='orders', asyn=True, method=True)
  header.kwargs = [Function.Param(name='pagination', type='PageParams', required=False)]
  header.return_type = 'Orders'
  return header


class FakePageParams:
  """Stand-in for a generated request-body `TypedDict`, for `walk()`."""

  def __init__(self, *, page: int | None = None, limit: int | None = None):
    self.page = page
    self.limit = limit

  def __repr__(self) -> str:
    return f'FakePageParams(page={self.page!r}, limit={self.limit!r})'


class TestPagedMethodNestedPagePagination:
  """`flatten_nested_pagination` covers `page`/`offset`/`seek` too, not just `token` --
  the identical nested-parameter shape shows up under a REST body object exactly as it
  does under a gRPC message, and the walk logic (advance an index, stop on a short page)
  doesn't care which strategy supplied the driver.
  """

  NESTED_FIELDS = {'pagination': {'page': 'int', 'limit': 'int'}}

  def test_nested_driver_leaves_the_outer_parameter_out_of_the_signature(
    self, generator: Generator,
  ):
    source = generator.paged_method(
      paged_endpoint(PAGE_NESTED),
      method_name='orders', header=page_nested_header(), response_type='Orders',
      nested_fields=self.NESTED_FIELDS,
    ) or ''
    # Isolated to the parameter list only -- the generated method name itself
    # (`orders_paged`) legitimately contains "page"; only a real `page:`/`page=`
    # parameter must not.
    signature = source.split(') -> AsyncIterator[Orders]:')[0]
    assert 'pagination' not in signature
    assert 'page:' not in signature and 'page=' not in signature
    assert 'limit: int | None = None' in signature

  def test_nested_driver_walks_and_reconstructs_the_message_each_call(
    self, generator: Generator,
  ):
    source = generator.paged_method(
      paged_endpoint(PAGE_NESTED),
      method_name='orders', header=page_nested_header(), response_type='Orders',
      nested_fields=self.NESTED_FIELDS,
    ) or ''
    pages = [[1, 2, 3], [4]]  # second page short of a full `limit` -- the walk stops there
    yielded, calls = walk(
      source, pages, extra_namespace={'PageParams': FakePageParams}, limit=3,
    )
    assert yielded == pages
    assert list(calls[0].keys()) == ['pagination']
    assert isinstance(calls[0]['pagination'], FakePageParams)
    # `page` starts the walk at 1 (PageIndex.start), advancing by 1 each turn -- `int`
    # has a real zero value (0) in `_SCALAR_ZERO_VALUES`, but the walk seeds it from the
    # loop counter directly, never from `None`, so no coercion is needed here the way
    # `TOKEN_NESTED`'s byte cursor needs one.
    assert calls[0]['pagination'].page == 1
    assert calls[0]['pagination'].limit == 3
    assert calls[1]['pagination'].page == 2
    assert calls[1]['pagination'].limit == 3


class TestTransportClassification:
  """A router learns which transports its children need, so one root can compose both.

  A venue serving its public and private channels on two sockets used to leak that as two
  extra top-level classes, because a channel handed to an HTTP `Router` would receive a
  `base_url` and an HTTP client it cannot use. Once the root owns both transports, the
  only thing a backend still needs is to know which children are which — that is what
  `RouterChild['transport']` carries, and these two helpers are where it comes from.
  """

  def test_an_http_endpoint_is_http(self):
    assert endpoint_transport(endpoint('market.orderbook', RECORD, title='Orderbook')) == 'http'

  def test_a_subscription_is_ws(self):
    ep = subscription_endpoint('ws.ticker', channel='ticker', message=DEPTH_MESSAGE)
    assert endpoint_transport(ep) == 'ws'

  def test_a_subtree_of_channels_is_ws(self):
    """The `ws` namespace merging two sockets' channels is still one WebSocket subtree."""
    transports = {
      ('api', ('ws', 'ticker')): 'ws',
      ('api', ('ws', 'order')): 'ws',
      ('api', ('market', 'time')): 'http',
    }
    assert subtree_transport(transports, 'api', ('ws',)) == 'ws'

  def test_a_subtree_of_endpoints_is_http(self):
    transports = {
      ('api', ('market', 'time')): 'http',
      ('api', ('market', 'tickers')): 'http',
      ('api', ('ws', 'ticker')): 'ws',
    }
    assert subtree_transport(transports, 'api', ('market',)) == 'http'

  def test_a_root_carrying_both_is_mixed(self):
    """The root of a client with a WebSocket surface owns a socket as well as an HTTP client."""
    transports = {
      ('api', ('market', 'time')): 'http',
      ('api', ('ws', 'ticker')): 'ws',
    }
    assert subtree_transport(transports, 'api', ()) == 'mixed'

  def test_a_client_with_no_channels_stays_http(self):
    """A venue with no WebSocket surface must not be told it needs a socket."""
    transports = {('api', ('market', 'time')): 'http'}
    assert subtree_transport(transports, 'api', ()) == 'http'

  def test_another_output_base_is_not_counted(self):
    """Leaves under a different output base belong to a different tree entirely."""
    transports = {
      ('api', ('market', 'time')): 'http',
      ('streams', ('market', 'time')): 'ws',
    }
    assert subtree_transport(transports, 'api', ('market',)) == 'http'

class TestRequestImports:
  """The helper an epoch parameter is dumped through has to be imported by somebody.

  `HttpRequest` emits `timestamp.dump(...)` and no import for it, because nothing in
  `truewire.generation` knows where a client keeps its core. `Generator` does, so this is
  where the two halves meet — and a backend that hand-rolls the condition instead is one
  edit away from shipping a module that raises `NameError` on import.
  """

  def test_an_epoch_parameter_pulls_the_helper_from_the_runtime(self, generator: Generator):
    request = HttpRequest(method='GET', path='/candles')
    request.query_params.append(
      HttpRequest.Param(name='startTime', required=True, type='TimestampMillis'),
    )
    assert generator.request_imports(request) == {'truewire_core.types': {'timestamp_millis'}}

  def test_a_request_without_an_epoch_parameter_imports_nothing(self, generator: Generator):
    request = HttpRequest(method='GET', path='/candles')
    request.query_params.append(HttpRequest.Param(name='limit', required=False, type='int'))
    assert generator.request_imports(request) == {}


class TestNestedPropType:
  """`_nested_prop_type` (design §7's `_rpc_request_params`/`_stream_parameters_params`
  own per-property type resolver) recurses into an array/tuple/anyOf to find a record
  `Unnest.records()` already extracted as its own named type, rather than re-deriving the
  *whole* property via one un-normalized `renderer.parser(prop, id=None)` call -- which
  raises `ValueError('Found nested record type')` the moment a record sits anywhere
  inside an array/tuple/union, confirmed against hyperliquid's real
  `perpDeploy.setDeployerFees` (`deployerFees: list[tuple[str, DeployerFee]]`, a real
  crash this suite did not previously cover) and alchemy's real `nft.get_nft_metadata_
  batch` (`tokens: list[NftMetadataBatchToken]`, array-of-record)."""

  def test_array_of_tuple_with_a_record_element_resolves_without_raising(
    self, generator: Generator,
  ):
    """hyperliquid's real `perpDeploy.setDeployerFees` shape: a list of `(coin, fee
    scale settings)` pairs, the second element a titled record -- reproduces `ValueError:
    Found nested record type` if `_nested_prop_type`'s array/tuple recursion is bypassed."""
    fee_scale = Schema.model_validate({
      'title': 'PerpDeployFeeScale',
      'type': 'object',
      'properties': {'scale': {'type': 'string'}, 'growth_mode': {'type': 'boolean'}},
      'required': ['scale', 'growth_mode'],
    })
    row = Schema.model_validate({
      'title': 'PerpDeployDeployerFeeRow',
      'type': 'array',
      'prefixItems': [{'title': 'coin', 'type': 'string'}, fee_scale.model_dump(exclude_none=True)],
      'minItems': 2,
      'maxItems': 2,
    })
    deployer_fees = Schema.model_validate({
      'title': 'PerpDeployDeployerFees',
      'type': 'array',
      'items': row.model_dump(exclude_none=True),
    })
    request_schema = Schema.model_validate({
      'type': 'object',
      'properties': {'deployerFees': deployer_fees.model_dump(exclude_none=True)},
      'required': ['deployerFees'],
    })

    type_generator = generator.type_generator()
    types = type_generator({'$request': request_schema.model_copy(update={'title': None})}, inline=True)

    type_name = generator._nested_prop_type(
      deployer_fees, prefix='$request/deployerFees',
      type_generator=type_generator, identifiers=types.identifiers,
    )
    assert type_name == 'list[tuple[str, PerpDeployFeeScale]]'

  def test_rpc_request_params_builds_the_field_without_raising(self, generator: Generator):
    """The real call path `rpc_endpoint` uses (`_rpc_request_params`, not `_nested_prop_
    type` standalone) -- confirms the fix reaches the actual generated-parameter derivation,
    not just the helper in isolation."""
    fee_scale = Schema.model_validate({
      'title': 'PerpDeployFeeScale',
      'type': 'object',
      'properties': {'scale': {'type': 'string'}, 'growth_mode': {'type': 'boolean'}},
      'required': ['scale', 'growth_mode'],
    })
    row = Schema.model_validate({
      'title': 'PerpDeployDeployerFeeRow',
      'type': 'array',
      'prefixItems': [{'title': 'coin', 'type': 'string'}, fee_scale.model_dump(exclude_none=True)],
      'minItems': 2,
      'maxItems': 2,
    })
    deployer_fees = Schema.model_validate({
      'title': 'PerpDeployDeployerFees',
      'type': 'array',
      'items': row.model_dump(exclude_none=True),
    })
    request_schema = Schema.model_validate({
      'type': 'object',
      'properties': {'deployerFees': deployer_fees.model_dump(exclude_none=True)},
      'required': ['deployerFees'],
    })

    type_generator = generator.type_generator()
    stripped = request_schema.model_copy(update={'title': None})
    types = type_generator({'$request': stripped}, inline=True)
    request_type = types.identifiers.get('$request')

    positional, keyword, decl_lines, value_expr, docs = generator._rpc_request_params(
      request_schema, type_generator=type_generator, request_type=request_type,
      identifiers=types.identifiers,
    )
    names = [p.name for p in [*positional, *keyword]]
    assert 'deployer_fees' in names
    param = next(p for p in [*positional, *keyword] if p.name == 'deployer_fees')
    assert param.type == 'list[tuple[str, PerpDeployFeeScale]]'

  def test_reproduces_the_original_crash_via_the_unnormalized_direct_call(
    self, generator: Generator,
  ):
    """Pins the bug this class exists to guard: calling the un-normalized single
    `renderer.parser(prop, id=None)` directly on a property with a record nested inside
    an array/tuple -- what `_rpc_request_params` did before `_nested_prop_type` existed --
    genuinely raises `ValueError('Found nested record type')`, confirming the fix is a
    real behavior change, not a no-op refactor."""
    fee_scale = Schema.model_validate({
      'title': 'PerpDeployFeeScale',
      'type': 'object',
      'properties': {'scale': {'type': 'string'}},
      'required': ['scale'],
    })
    row = Schema.model_validate({
      'type': 'array',
      'prefixItems': [{'type': 'string'}, fee_scale.model_dump(exclude_none=True)],
      'minItems': 2,
      'maxItems': 2,
    })
    deployer_fees = Schema.model_validate({'type': 'array', 'items': row.model_dump(exclude_none=True)})

    type_generator = generator.type_generator()
    renderer = type_generator.render
    with pytest.raises(ValueError, match='Found nested record type'):
      renderer.code(renderer.parser(deployer_fees, id=None))
