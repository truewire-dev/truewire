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

The page-method renderers (`paged_response_*`) live in `test_codegen_paged.py`.
"""
from typing_extensions import Any, Literal, overload

import pytest

from truewire.generation.python.code import HttpRequest
from truewire.generation.schema import Schema

from truewire.codegen.python import Generator, endpoint_transport, subtree_transport
from truewire.spec import Endpoint

OVERLOAD_NAMESPACE: dict[str, Any] = {'overload': overload, 'Literal': Literal, 'Any': Any}
"""What a generated walker's `validate` overload stubs (`validate_overloads`) name."""


def _implementation(source: str) -> str:
  """`source` past its `@overload` stubs (`validate_overloads`): the implementation a
  walker's header assertions read, since the stubs now come first."""
  lines = source.splitlines()
  last_stub = max((i for i, line in enumerate(lines) if line.endswith(': ...')), default=-1)
  return '\n'.join(lines[last_stub + 1:])

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
