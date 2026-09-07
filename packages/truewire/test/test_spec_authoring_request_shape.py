"""
Exercise `operation_json`'s synthesis of a plain-JSON operation from the new
`spec.request`/`spec.response` shape (Task 2), against a synthetic fixture.

`operation_json` previously read only `endpoint.openapi` (the legacy shape). Every generic,
`nodes()`-based spec-test check (`check_titles`, `check_enums`, etc.) consumes whatever this
function returns, so teaching it to synthesize an equivalent plain-JSON operation from
`request`/`response` makes those checks work against the new shape for free -- no changes
needed to the checks themselves.
"""
from truewire.spec.authoring import nodes, operation_json
from truewire.spec.endpoint import Endpoint


def test_operation_json_synthesizes_from_request_response():
  """`operation_json` builds a plain-JSON operation from `request`/`response` when set."""
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
      'description': 'Get an X.',
      'request': {'title': 'XRequest', 'type': 'object', 'properties': {
        'symbol': {'type': 'string', 'description': 'Pair.'}
      }},
      'response': {'title': 'XResponse', 'type': 'object', 'properties': {}},
    },
  })
  operation = operation_json(endpoint)
  assert operation is not None
  assert operation['description'] == 'Get an X.'
  # nodes()-based checks (titles, enums, timestamp-format, positional-rows, unions) need to
  # see both the request and response schema in the walk, unchanged:
  found_titles = {node.get('title') for _, _, node in nodes(operation) if node.get('title')}
  assert found_titles == {'XRequest', 'XResponse'}


def test_operation_json_synthesizes_stream_request_and_payload():
  """A `StreamEndpointSpec` synthesizes from `request` (subscribe side) and `payload`
  (pushed-message side) -- not `response`, which `StreamEndpointSpec` has no field for."""
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'stream', 'channel': 'trades',
      'description': 'Trade pushes.',
      'request': {'title': 'TradeSubscribeRequest', 'type': 'object', 'properties': {
        'symbol': {'type': 'string', 'description': 'Trading pair.'}
      }},
      'payload': {'title': 'Trade', 'type': 'object', 'properties': {
        'price': {'type': 'string', 'description': 'Trade price.'}
      }},
    },
  })
  operation = operation_json(endpoint)
  assert operation is not None
  assert operation['description'] == 'Trade pushes.'
  # both the subscribe-side request schema and the pushed-message payload schema must be
  # visible to nodes()-based checks:
  found_titles = {node.get('title') for _, _, node in nodes(operation) if node.get('title')}
  assert found_titles == {'TradeSubscribeRequest', 'Trade'}


def test_operation_json_synthesizes_push_only_stream_with_no_request():
  """A push-only stream endpoint (rule 11: only `payload` set, no `request`/`parameters`)
  still synthesizes a checkable operation -- it must not fall through to the legacy
  `openapi` path (which is `None` for any new-shape endpoint) and return `None`."""
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {
      'kind': 'stream', 'channel': 'trades',
      'payload': {'title': 'Trade', 'type': 'object', 'properties': {
        'price': {'type': 'string', 'description': 'Trade price.'}
      }},
    },
  })
  operation = operation_json(endpoint)
  assert operation is not None
  found_titles = {node.get('title') for _, _, node in nodes(operation) if node.get('title')}
  assert found_titles == {'Trade'}
