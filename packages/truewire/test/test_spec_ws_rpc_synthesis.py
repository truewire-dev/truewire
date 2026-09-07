"""`truewire.spec.repo.ws_examples`'s synthesis of a WS-RPC example from a captured HTTP
one, for a dual-transport (`spec.transports: ['http', 'ws']`) `kind: 'rpc'` endpoint with
no native `.parameters.json`/`.reply.json` capture.

This is the real gap deribit exposed: ~123 of its dual-transport RPC endpoints were
captured only over HTTP, so `client_ws_examples`/`build_ws_rpc_replay_test`
(`docs/production_standards.md` S19) had 0 examples to replay for any of them, even though
the venue's own JSON-RPC method+params+result is identical over either transport (see
`clients/deribit/spec/endpoints/combo_books/create_combo/endpoint.json`'s own `notes`).
Never a real client's files -- a small synthetic fixture, matching every other suite in
this directory.
"""

import json
from pathlib import Path

from truewire.spec import client_ws_examples
from truewire.spec.repo import http_examples, load_endpoint, ws_examples

DUAL_TRANSPORT_ENDPOINT = {
  'function': 'combos.create',
  'spec': {
    'kind': 'rpc',
    'transports': ['http', 'ws'],
    'method': 'POST',
    'path': 'private/create_combo',
    'openapi': {
      'description': 'Create a combo.',
      'parameters': [
        {
          'name': 'trades',
          'in': 'query',
          'required': True,
          'description': 'Legs to combine.',
          'schema': {'type': 'array', 'items': {'type': 'object'}},
        },
      ],
      'responses': {
        '200': {
          'description': 'The created combo.',
          'content': {
            'application/json': {
              'schema': {
                'title': 'Combo',
                'type': 'object',
                'description': 'A combo.',
                'properties': {'id': {'type': 'string', 'description': 'Combo id.'}},
              }
            }
          },
        },
      },
    },
  },
  'envelope': {'payload': 'result', 'correlate': 'id'},
}


def _write_endpoint(root: Path, spec: dict, *, subdir: str = 'combos/create') -> Path:
  endpoint_dir = root / 'spec' / 'endpoints' / subdir
  (endpoint_dir / 'examples').mkdir(parents=True)
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(spec))
  return endpoint_dir


def _write_http_example(endpoint_dir: Path, example_id: str, *, parameters: dict, result: dict):
  (endpoint_dir / 'examples' / f'{example_id}.request.json').write_text(
    json.dumps({'description': f'{example_id} request', 'parameters': parameters})
  )
  (endpoint_dir / 'examples' / f'{example_id}.response.json').write_text(
    json.dumps(
      {'status': 200, 'payload': {'jsonrpc': '2.0', 'id': 0, 'result': result}}
    )
  )


def test_a_dual_transport_endpoint_with_only_http_capture_gets_a_synthesized_ws_example(
  tmp_path: Path,
):
  endpoint_dir = _write_endpoint(tmp_path, DUAL_TRANSPORT_ENDPOINT)
  _write_http_example(
    endpoint_dir, '01',
    parameters={'trades': [{'instrument_name': 'BTC-1', 'amount': 1, 'direction': 'buy'}]},
    result={'id': 'BTC-COMBO-1', 'state': 'active'},
  )

  [example] = client_ws_examples(tmp_path)

  assert example.example_id == '01'
  assert example.parameters.parameters == {
    'trades': [{'instrument_name': 'BTC-1', 'amount': 1, 'direction': 'buy'}]
  }
  # `payload` is a real outgoing JSON-RPC frame, keyed by the endpoint's own wire method
  # (`endpoint.channel`, `spec.path` for a dual-transport rpc) -- what
  # `WsMockRegistry.match_rpc` actually compares against.
  assert example.parameters.payload['method'] == 'private/create_combo'
  assert example.parameters.payload['params'] == example.parameters.parameters
  # The whole recorded HTTP response frame becomes the synthetic reply verbatim -- the
  # same shape a native `.reply.json` capture already carries.
  assert example.reply == {
    'jsonrpc': '2.0', 'id': 0, 'result': {'id': 'BTC-COMBO-1', 'state': 'active'},
  }
  assert example.messages == []
  assert example.message_frames == []


def test_a_native_ws_capture_is_never_overridden_by_a_synthesized_one(tmp_path: Path):
  endpoint_dir = _write_endpoint(tmp_path, DUAL_TRANSPORT_ENDPOINT)
  _write_http_example(
    endpoint_dir, '01', parameters={'trades': []}, result={'id': 'http-captured'},
  )
  (endpoint_dir / 'examples' / '01.parameters.json').write_text(
    json.dumps({'parameters': {'trades': []}, 'payload': {'method': 'private/create_combo', 'params': {'trades': []}, 'id': 0}})
  )
  (endpoint_dir / 'examples' / '01.reply.json').write_text(
    json.dumps({'jsonrpc': '2.0', 'id': 0, 'result': {'id': 'ws-captured'}})
  )

  [example] = client_ws_examples(tmp_path)

  # The native WS capture's own reply wins, not a synthesized re-derivation from the HTTP
  # capture's own (different) recorded result.
  assert example.reply == {'jsonrpc': '2.0', 'id': 0, 'result': {'id': 'ws-captured'}}


def test_a_second_example_id_present_only_over_http_still_gets_synthesized_alongside_a_native_one(
  tmp_path: Path,
):
  endpoint_dir = _write_endpoint(tmp_path, DUAL_TRANSPORT_ENDPOINT)
  (endpoint_dir / 'examples' / '01.parameters.json').write_text(
    json.dumps({'parameters': {'trades': []}, 'payload': {'method': 'private/create_combo', 'params': {'trades': []}, 'id': 0}})
  )
  (endpoint_dir / 'examples' / '01.reply.json').write_text(
    json.dumps({'jsonrpc': '2.0', 'id': 0, 'result': {'id': 'native'}})
  )
  _write_http_example(
    endpoint_dir, '02', parameters={'trades': []}, result={'id': 'synthesized'},
  )

  examples = ws_examples(endpoint_dir / 'endpoint.json', load_endpoint(endpoint_dir / 'endpoint.json'))
  by_id = {example.example_id: example for example in examples}

  assert by_id.keys() == {'01', '02'}
  assert by_id['01'].reply == {'jsonrpc': '2.0', 'id': 0, 'result': {'id': 'native'}}
  assert by_id['02'].reply['result'] == {'id': 'synthesized'}


def test_http_only_endpoint_never_synthesizes_anything(tmp_path: Path):
  spec = {**DUAL_TRANSPORT_ENDPOINT, 'spec': {**DUAL_TRANSPORT_ENDPOINT['spec'], 'transports': ['http']}}
  endpoint_dir = _write_endpoint(tmp_path, spec)
  _write_http_example(endpoint_dir, '01', parameters={'trades': []}, result={'id': 'x'})

  assert client_ws_examples(tmp_path) == []


def test_ws_only_endpoint_synthesizes_nothing_since_there_is_no_http_capture(tmp_path: Path):
  spec = {**DUAL_TRANSPORT_ENDPOINT, 'spec': {**DUAL_TRANSPORT_ENDPOINT['spec'], 'transports': ['ws']}}
  endpoint_dir = _write_endpoint(tmp_path, spec)
  # No `.request.json`/`.response.json` at all -- nothing for `http_examples` to find, so
  # this endpoint's ws examples stay whatever was natively captured (nothing, here).
  assert ws_examples(endpoint_dir / 'endpoint.json', load_endpoint(endpoint_dir / 'endpoint.json')) == []


def test_a_stream_endpoint_is_never_synthesized_even_if_it_somehow_carried_http_files(
  tmp_path: Path,
):
  # `kind: 'stream'` never declares `transports` at all (WS-only, per `Endpoint.transports`),
  # so `http_examples` (which requires `'http' in endpoint.transports`) always returns `[]`
  # for one -- confirming the `endpoint.spec.kind == 'rpc'` guard in `ws_examples` is not
  # the only thing standing between a stream endpoint and synthesis.
  spec = {
    'function': 'streams.ticker',
    'spec': {'kind': 'stream', 'channel': '/market/ticker:{symbol}', 'openapi': {
      'description': 'Ticker pushes.',
      'parameters': [{'name': 'symbol', 'in': 'path', 'required': True, 'description': 'Symbol.', 'schema': {'type': 'string'}}],
      'responses': {'message': {'description': 'Push.'}},
    }},
  }
  endpoint_dir = _write_endpoint(tmp_path, spec, subdir='streams/ticker')
  (endpoint_dir / 'examples' / '01.parameters.json').write_text(
    json.dumps({'parameters': {'symbol': 'BTC-USDT'}})
  )
  (endpoint_dir / 'examples' / '01.messages.json').write_text(json.dumps([{'price': '1'}]))

  examples = ws_examples(endpoint_dir / 'endpoint.json', load_endpoint(endpoint_dir / 'endpoint.json'))
  assert len(examples) == 1
  assert examples[0].messages == [{'price': '1'}]


def test_http_examples_helper_still_reports_the_underlying_capture_unaffected_by_synthesis(
  tmp_path: Path,
):
  """Sanity check that synthesis is additive to `client_ws_examples` only -- `http_examples`
  itself is untouched and still reports the same HTTP example it always did."""
  endpoint_dir = _write_endpoint(tmp_path, DUAL_TRANSPORT_ENDPOINT)
  _write_http_example(endpoint_dir, '01', parameters={'trades': []}, result={'id': 'x'})

  endpoint = load_endpoint(endpoint_dir / 'endpoint.json')
  [http_example] = http_examples(endpoint_dir / 'endpoint.json', endpoint)
  assert http_example.example_id == '01'
  assert http_example.request.parameters == {'trades': []}
