"""`truewire.spec.endpoint`'s `EndpointSpec` union: `kind: 'rpc' | 'stream'` + `transports`."""

import pytest
from pydantic import ValidationError

from truewire.spec import Endpoint

RPC_HTTP = {
  'function': 'widgets.get',
  'spec': {
    'kind': 'rpc',
    'transports': ['http'],
    'path': '/widgets/{id}',
    'method': 'GET',
    'openapi': {
      'description': 'Get a widget.',
      'responses': {'200': {'description': 'ok'}},
    },
  },
}

RPC_WS = {
  'function': 'trading.buy',
  'spec': {
    'kind': 'rpc',
    'transports': ['ws'],
    'path': 'private/buy',
    'openapi': {
      'description': 'Place an order.',
      'responses': {'reply': {'description': 'ack'}},
    },
  },
}

RPC_DUAL = {
  'function': 'trading.get_order_state',
  'spec': {
    'kind': 'rpc',
    'transports': ['http', 'ws'],
    'path': 'private/get_order_state',
    'method': 'POST',
    'openapi': {
      'description': 'Order state.',
      'responses': {'200': {'description': 'ok'}},
    },
  },
}

STREAM = {
  'function': 'streams.trades',
  'spec': {
    'kind': 'stream',
    'channel': 'stream_trades',
    'openapi': {
      'description': 'Trades.',
      'responses': {'message': {'description': 'trade'}},
    },
  },
}


def test_rpc_http_parses():
  endpoint = Endpoint.model_validate(RPC_HTTP)
  assert endpoint.transports == ['http']
  assert endpoint.path == '/widgets/{id}'
  assert endpoint.method == 'GET'
  assert endpoint.channel is None


def test_rpc_ws_parses():
  endpoint = Endpoint.model_validate(RPC_WS)
  assert endpoint.transports == ['ws']
  assert endpoint.path == 'private/buy'
  assert endpoint.channel == 'private/buy'
  assert endpoint.method is None


def test_rpc_dual_transport_parses():
  endpoint = Endpoint.model_validate(RPC_DUAL)
  assert endpoint.transports == ['http', 'ws']
  assert endpoint.path == 'private/get_order_state'
  assert endpoint.channel == 'private/get_order_state'


def test_stream_parses_and_implies_ws_transport():
  endpoint = Endpoint.model_validate(STREAM)
  assert endpoint.transports == ['ws']
  assert endpoint.channel == 'stream_trades'


def test_stream_rejects_a_transports_field():
  bad = {**STREAM, 'spec': {**STREAM['spec'], 'transports': ['ws']}}
  with pytest.raises(ValidationError):
    Endpoint.model_validate(bad)


def test_rpc_no_longer_accepts_channel():
  """ADR 0011: `channel` folded into `path`; `RpcEndpointSpec` no longer has the field."""
  bad = {**RPC_WS, 'spec': {**RPC_WS['spec'], 'channel': 'private/buy'}}
  with pytest.raises(ValidationError):
    Endpoint.model_validate(bad)


def test_rpc_requires_path_regardless_of_transport():
  """ADR 0011: `path` is the operation identifier for every rpc endpoint, http or ws alike."""
  bad = {**RPC_WS, 'spec': {k: v for k, v in RPC_WS['spec'].items() if k != 'path'}}
  with pytest.raises(ValidationError):
    Endpoint.model_validate(bad)


def test_rpc_method_is_optional_even_over_http():
  """ADR 0011: whether an http-transported rpc endpoint states its verb is venue-specific."""
  endpoint = Endpoint.model_validate(
    {**RPC_HTTP, 'spec': {k: v for k, v in RPC_HTTP['spec'].items() if k != 'method'}}
  )
  assert endpoint.method is None
  assert endpoint.transports == ['http']


def test_rpc_requires_at_least_one_transport():
  bad = {**RPC_HTTP, 'spec': {**RPC_HTTP['spec'], 'transports': []}}
  with pytest.raises(ValidationError):
    Endpoint.model_validate(bad)


@pytest.mark.parametrize('function', [
  'streams.spot_margin.ticker',
  'ticker',
])
def test_a_clean_dotted_function_is_accepted(function):
  endpoint = Endpoint.model_validate({**RPC_HTTP, 'function': function})
  assert endpoint.function == function


@pytest.mark.parametrize('function', [
  'streams..ticker',                     # empty segment
  'streams.spot-margin.ticker',          # hyphen, not a Python identifier
  'streams/spot_margin/ticker',          # wrong separator
  'streams.spot_margin.ticker:extra',    # colon -- that's a symbol, not a function
  '',                                    # empty
  '.ticker',                             # leading dot
  'ticker.',                             # trailing dot
])
def test_a_malformed_function_is_rejected(function):
  with pytest.raises(ValidationError, match='function'):
    Endpoint.model_validate({**RPC_HTTP, 'function': function})


def test_rpc_endpoint_spec_accepts_request_schema():
  endpoint = Endpoint.model_validate({
    'function': 'market.orderbook',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/v5/market/orderbook',
      'method': 'GET',
      'request': {
        'title': 'OrderbookRequest',
        'type': 'object',
        'properties': {'symbol': {'type': 'string', 'description': 'Trading pair.'}},
        'required': ['symbol'],
      },
      'response': {
        'title': 'Orderbook',
        'type': 'object',
        'properties': {},
      },
    },
  })
  assert endpoint.spec.request == {
    'title': 'OrderbookRequest',
    'type': 'object',
    'properties': {'symbol': {'type': 'string', 'description': 'Trading pair.'}},
    'required': ['symbol'],
  }
  assert endpoint.spec.response == {
    'title': 'Orderbook',
    'type': 'object',
    'properties': {},
  }


def test_rpc_endpoint_spec_still_accepts_openapi_shape():
  """Dual-shape support: existing clients' `openapi.parameters`/`requestBody` keep validating
  unchanged until each client migrates (design's resolved Open Questions: no client gates another)."""
  endpoint = Endpoint.model_validate({
    'function': 'market.orderbook',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/v5/market/orderbook',
      'method': 'GET',
      'openapi': {'summary': 'Get Orderbook', 'responses': {'200': {
        'description': 'Success',
        'content': {'application/json': {'schema': {'title': 'Orderbook', 'type': 'object', 'properties': {}}}},
      }}},
    },
  })
  assert endpoint.spec.request is None
  assert endpoint.spec.openapi is not None


def test_rpc_endpoint_spec_rejects_both_openapi_and_request():
  """Regression test: dual-shape validator must reject openapi + request without response."""
  with pytest.raises(ValidationError, match='exactly one'):
    Endpoint.model_validate({
      'function': 'market.orderbook',
      'meta': {'public': True},
      'spec': {
        'kind': 'rpc',
        'transports': ['http'],
        'path': '/v5/market/orderbook',
        'method': 'GET',
        'openapi': {'summary': 'Get Orderbook', 'responses': {'200': {'description': 'Success'}}},
        'request': {'title': 'OrderbookRequest', 'type': 'object', 'properties': {}},
      },
    })


def test_stream_endpoint_spec_accepts_request_parameters_payload():
  """StreamEndpointSpec accepts new-shape fields: request, parameters, payload."""
  endpoint = Endpoint.model_validate({
    'function': 'streams.trades',
    'meta': {'public': True},
    'spec': {
      'kind': 'stream',
      'channel': 'trades',
      'request': {
        'title': 'TradeSubscribeRequest',
        'type': 'object',
        'properties': {'symbol': {'type': 'string', 'description': 'Trading pair.'}},
        'required': ['symbol'],
      },
      'parameters': {
        'title': 'TradeSubscribeParameters',
        'type': 'object',
        'properties': {'symbol': {'type': 'string'}},
      },
      'payload': {
        'title': 'Trade',
        'type': 'object',
        'properties': {'symbol': {'type': 'string'}, 'price': {'type': 'string'}},
      },
    },
  })
  assert endpoint.spec.request == {
    'title': 'TradeSubscribeRequest',
    'type': 'object',
    'properties': {'symbol': {'type': 'string', 'description': 'Trading pair.'}},
    'required': ['symbol'],
  }
  assert endpoint.spec.parameters is not None
  assert endpoint.spec.payload is not None
  assert endpoint.spec.openapi is None


def test_stream_endpoint_spec_still_accepts_openapi_shape():
  """StreamEndpointSpec still accepts legacy openapi shape."""
  endpoint = Endpoint.model_validate({
    'function': 'streams.trades',
    'spec': {
      'kind': 'stream',
      'channel': 'trades',
      'openapi': {
        'summary': 'Trades',
        'responses': {'message': {'description': 'trade'}},
      },
    },
  })
  assert endpoint.spec.request is None
  assert endpoint.spec.parameters is None
  assert endpoint.spec.payload is None
  assert endpoint.spec.openapi is not None


def test_stream_endpoint_spec_rejects_both_openapi_and_parameters():
  """Regression test: dual-shape validator must reject openapi + parameters without payload."""
  with pytest.raises(ValidationError, match='exactly one'):
    Endpoint.model_validate({
      'function': 'streams.trades',
      'spec': {
        'kind': 'stream',
        'channel': 'trades',
        'openapi': {'summary': 'Trades', 'responses': {'message': {'description': 'trade'}}},
        'parameters': {'title': 'TradeSubscribeParameters', 'type': 'object', 'properties': {}},
      },
    })


def test_directory_function_derives_from_path():
  from pathlib import Path
  from truewire.spec.endpoint import directory_function
  spec_root = Path('/tmp/fake_client/spec')
  endpoint_path = spec_root / 'endpoints' / 'market' / 'orderbook' / 'endpoint.json'
  assert directory_function(endpoint_path, spec_root) == 'market.orderbook'


def test_endpoint_function_is_optional():
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/x',
      'method': 'GET',
      'request': {'type': 'object', 'properties': {}},
      'response': {'type': 'object', 'properties': {}},
    },
  })
  assert endpoint.function is None


def test_resolved_function_returns_authored_when_set():
  from pathlib import Path
  endpoint = Endpoint.model_validate({
    'function': 'market.orderbook',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/x',
      'method': 'GET',
      'request': {'type': 'object', 'properties': {}},
      'response': {'type': 'object', 'properties': {}},
    },
  })
  spec_root = Path('/tmp/fake_client/spec')
  endpoint_path = spec_root / 'endpoints' / 'market' / 'orderbook' / 'endpoint.json'
  assert endpoint.resolved_function(endpoint_path, spec_root) == 'market.orderbook'


def test_resolved_function_derives_when_not_set():
  from pathlib import Path
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/x',
      'method': 'GET',
      'request': {'type': 'object', 'properties': {}},
      'response': {'type': 'object', 'properties': {}},
    },
  })
  spec_root = Path('/tmp/fake_client/spec')
  endpoint_path = spec_root / 'endpoints' / 'market' / 'orderbook' / 'endpoint.json'
  assert endpoint.resolved_function(endpoint_path, spec_root) == 'market.orderbook'


def test_endpoint_requires_meta():
  """New-shape endpoints require `meta` to be set."""
  with pytest.raises(ValidationError):
    Endpoint.model_validate({
      'function': 'market.orderbook',
      'spec': {
        'kind': 'rpc',
        'transports': ['http'],
        'path': '/x',
        'method': 'GET',
        'request': {'type': 'object', 'properties': {}},
        'response': {'type': 'object', 'properties': {}},
      },
    })


def test_legacy_rpc_without_meta_still_loads():
  """Legacy openapi-shaped RpcEndpointSpec with no meta still loads (regression guard)."""
  endpoint = Endpoint.model_validate({
    'function': 'market.orderbook',
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/v5/market/orderbook',
      'method': 'GET',
      'openapi': {'summary': 'Get Orderbook', 'responses': {'200': {
        'description': 'Success',
        'content': {'application/json': {'schema': {'title': 'Orderbook', 'type': 'object', 'properties': {}}}},
      }}},
    },
  })
  assert endpoint.spec.openapi is not None
  assert endpoint.meta is None


def test_grpc_endpoint_without_meta_still_loads():
  """Existing (not-yet-migrated) GrpcEndpointSpec endpoint with no meta still loads cleanly.

  This is the regression test the isinstance guard exists to prevent: without it, a bare
  getattr check would see GrpcEndpointSpec.request (always a str, never None) and think
  every gRPC endpoint has already migrated to the new shape, requiring meta immediately.
  """
  endpoint = Endpoint.model_validate({
    'spec': {
      'kind': 'grpc',
      'service': 'cosmos.bank.v1beta1.Query',
      'rpc': 'Balance',
      'request': 'cosmos.bank.v1beta1.QueryBalanceRequest',
      'response': 'cosmos.bank.v1beta1.QueryBalanceResponse',
      'proto': 'cosmos/bank/v1beta1/query.proto',
      'description': 'Query balance.',
    },
  })
  assert endpoint.spec.kind == 'grpc'
  assert endpoint.meta is None


def test_stream_endpoint_with_only_payload_requires_meta():
  """Stream endpoint migrated with only payload set (push-only shape) must have meta.

  Regression test for the dual-shape field set bug: StreamEndpointSpec has no response
  field, only request/parameters/payload. A bare getattr check for response would falsely
  skip the meta requirement for a stream endpoint with only payload set, the legitimate
  push-only-stream shape per docs/spec/authoring.md rule 11. Each spec type must check
  its own field set, not the sibling's."""
  with pytest.raises(ValidationError):
    Endpoint.model_validate({
      'function': 'streams.push_only',
      'spec': {
        'kind': 'stream',
        'channel': 'trades',
        'payload': {
          'title': 'Trade',
          'type': 'object',
          'properties': {'symbol': {'type': 'string'}, 'price': {'type': 'string'}},
        },
      },
    })


def test_rpc_endpoint_spec_description_is_optional_and_round_trips():
  """`RpcEndpointSpec.description` defaults to `None`, and a set value round-trips.

  Mirrors `GrpcEndpointSpec.description` for the new-shape path, since neither
  `RpcEndpointSpec` nor `StreamEndpointSpec` has an `openapi.description` to reuse.
  """
  no_description = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
      'request': {'type': 'object', 'properties': {}},
      'response': {'type': 'object', 'properties': {}},
    },
  })
  assert no_description.spec.description is None

  with_description = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
      'request': {'type': 'object', 'properties': {}},
      'response': {'type': 'object', 'properties': {}},
      'description': 'Get a widget.',
    },
  })
  assert with_description.spec.description == 'Get a widget.'


def test_stream_endpoint_spec_description_is_optional_and_round_trips():
  """`StreamEndpointSpec.description` defaults to `None`, and a set value round-trips."""
  no_description = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'stream', 'channel': 'trades',
      'payload': {'type': 'object', 'properties': {}},
    },
  })
  assert no_description.spec.description is None

  with_description = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'stream', 'channel': 'trades',
      'payload': {'type': 'object', 'properties': {}},
      'description': 'Trade pushes.',
    },
  })
  assert with_description.spec.description == 'Trade pushes.'
