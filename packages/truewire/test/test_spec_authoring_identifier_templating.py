"""
Exercise `check_identifier_templating` (ADR 0011) against synthetic fixtures, never `clients/`.

Every `in: 'path'` parameter on a `kind: 'rpc'` or `kind: 'stream'` endpoint must have a
matching `{name}` placeholder in the operation's identifier (`path` for rpc, `channel` for
stream), and every placeholder in that identifier must have a matching declared parameter.
Fixtures live entirely under `tmp_path`, per the isolation contract `common/lib/test/
test_mock.py`'s module docstring records.
"""
import json
from pathlib import Path

import pytest
import typer

from truewire.cli.check import check as spec_test
from truewire.spec.authoring import check_identifier_templating, operation_json
from truewire.spec.endpoint import Endpoint


def write_rpc_endpoint(root: Path, *, function: str, path: str, parameters: list) -> None:
  """Write one minimal http `endpoint.json` with a given identifier and `parameters` list."""
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': path,
      'openapi': {
        'description': f'Get {function}.',
        'parameters': parameters,
        'responses': {
          '200': {
            'description': 'ok',
            'content': {'application/json': {'schema': {
              'title': 'Widget', 'type': 'object', 'description': 'A widget.',
              'properties': {'name': {'type': 'string', 'description': 'Widget name.'}},
            }}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def write_stream_endpoint(root: Path, *, function: str, channel: str, parameters: list) -> None:
  """Write one minimal ws `endpoint.json` (`kind: 'stream'`) with a given channel template."""
  endpoint_dir = root / 'spec' / 'endpoints' / 'ws' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'spec': {
      'kind': 'stream',
      'channel': channel,
      'openapi': {
        'description': f'Subscribe to {function}.',
        'parameters': parameters,
        'responses': {
          'message': {
            'description': 'push',
            'content': {'application/json': {'schema': {
              'title': 'Tick', 'type': 'object', 'description': 'A tick.',
              'properties': {'price': {'type': 'string', 'description': 'Price.'}},
            }}},
          },
        },
      },
    },
    'envelope': {
      'payload': '',
      'verb': {'path': 'type', 'subscribe': 'subscribe', 'unsubscribe': 'unsubscribe'},
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


PATH_PARAM = {
  'name': 'address', 'in': 'path', 'required': True,
  'description': 'Wallet address.', 'schema': {'type': 'string'},
}


def test_a_matching_in_path_parameter_is_clean(tmp_path, capsys):
  write_rpc_endpoint(
    tmp_path, function='portfolio.get', path='/portfolio/{address}', parameters=[PATH_PARAM],
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_hyphenated_parameter_name_is_matched_verbatim(tmp_path, capsys):
  """kucoin's `order-id` regression: codegen substitutes by the raw declared name, hyphens
  and all (`HttpRequest.request_call`), so `{order-id}` must not read as no-placeholder."""
  write_rpc_endpoint(
    tmp_path, function='orders.get_by_order_id', path='/api/v1/orders/{order-id}',
    parameters=[{
      'name': 'order-id', 'in': 'path', 'required': True,
      'description': 'Order id.', 'schema': {'type': 'string'},
    }],
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_an_in_path_parameter_with_no_placeholder_is_flagged(tmp_path, capsys):
  write_rpc_endpoint(
    tmp_path, function='portfolio.get', path='/portfolio', parameters=[PATH_PARAM],
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert 'Result: FAILED' in captured.out
  assert 'address' in captured.err


def test_a_placeholder_with_no_declared_in_path_parameter_is_flagged(tmp_path, capsys):
  write_rpc_endpoint(
    tmp_path, function='portfolio.get', path='/portfolio/{address}', parameters=[],
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert 'Result: FAILED' in captured.out
  assert '{address}' in captured.err


def test_a_stream_channel_is_checked_the_same_way(tmp_path, capsys):
  write_stream_endpoint(
    tmp_path, function='streams.subaccount', channel='v4_subaccounts',
    parameters=[{
      'name': 'id', 'in': 'path', 'required': True,
      'description': 'Subaccount id.', 'schema': {'type': 'string'},
    }],
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=True)

  assert exc_info.value.exit_code == 1
  captured = capsys.readouterr()
  assert 'violations 1\n  warnings   0' in captured.out
  assert 'Result: FAILED' in captured.out
  assert 'id' in captured.err


def test_a_matching_stream_channel_template_is_clean(tmp_path, capsys):
  write_stream_endpoint(
    tmp_path, function='streams.subaccount', channel='v4_subaccounts/{id}',
    parameters=[{
      'name': 'id', 'in': 'path', 'required': True,
      'description': 'Subaccount id.', 'schema': {'type': 'string'},
    }],
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_identifier_templating_new_shape_placeholder_matches_property():
  """New shape (design §7): there is no `in: 'path'` at all -- a path parameter is simply
  a `request` property whose name matches a `{placeholder}` in `path`/`channel`. A declared
  `orderId` property matching the `{orderId}` placeholder is clean."""
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {'kind': 'rpc', 'transports': ['http'], 'path': '/v1/order/{orderId}', 'method': 'GET',
              'request': {'type': 'object', 'properties': {'orderId': {'type': 'string', 'description': 'Order id.'}}},
              'response': {'type': 'object', 'properties': {}}},
  })
  assert check_identifier_templating(endpoint, operation_json(endpoint)) == []


def test_identifier_templating_new_shape_flags_undeclared_placeholder():
  """A `{orderId}` placeholder with no matching `request` property is flagged -- the one
  remaining direction under the new shape, since nothing is explicitly labeled `path`."""
  endpoint = Endpoint.model_validate({
    'meta': {'public': True},
    'spec': {'kind': 'rpc', 'transports': ['http'], 'path': '/v1/order/{orderId}', 'method': 'GET',
              'request': {'type': 'object', 'properties': {}},
              'response': {'type': 'object', 'properties': {}}},
  })
  violations = check_identifier_templating(endpoint, operation_json(endpoint))
  assert any('orderId' in v['message'] for v in violations)
