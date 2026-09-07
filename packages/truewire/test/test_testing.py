"""Pin `build_http_replay_test`/`build_ws_replay_test`/`build_ws_rpc_replay_test` against
synthetic specs, decoupled from any real client."""

import asyncio
import json
from pathlib import Path

import pytest
from truewire_core.util.streams import Stream, StreamManager

from truewire.spec import client_http_examples, client_ws_examples
from truewire.testing import build_http_replay_test, build_ws_replay_test, build_ws_rpc_replay_test


def _write_widget_get_spec(root: Path, *, function: str = 'widgets.get'):
  spec_dir = root / 'spec' / 'endpoints' / 'widgets' / 'get'
  (spec_dir / 'examples').mkdir(parents=True)
  (spec_dir / 'endpoint.json').write_text(
    json.dumps(
      {
        'function': function,
        'spec': {
          'kind': 'rpc',
          'transports': ['http'],
          'method': 'GET',
          'path': '/widgets/{id}',
          'openapi': {
            'description': 'Get one widget by id.',
            'parameters': [
              {
                'name': 'id',
                'in': 'path',
                'required': True,
                'description': 'Widget id.',
                'schema': {'type': 'string'},
              },
              {
                'name': 'extraDetail',
                'in': 'query',
                'required': False,
                'description': 'Include extra detail fields.',
                'schema': {'type': 'boolean'},
              },
            ],
            'responses': {
              '200': {
                'description': 'The widget.',
                'content': {
                  'application/json': {
                    'schema': {
                      'title': 'Widget',
                      'type': 'object',
                      'description': 'One widget.',
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
    )
  )
  (spec_dir / 'examples' / 'w1.request.json').write_text(
    json.dumps({'parameters': {'id': 'w1', 'extraDetail': True}})
  )
  (spec_dir / 'examples' / 'w1.response.json').write_text(
    json.dumps({'status': 200, 'payload': {'name': 'Widget One'}})
  )


class FakeWidgets:
  """A fake sub-router standing in for a generated client's own, e.g. `client.http.market`."""

  def __init__(self):
    self.calls = []

  async def get(self, *, id: str, extra_detail: bool | None = None):
    self.calls.append({'id': id, 'extra_detail': extra_detail})


class FakeClient:
  """A fake client standing in for what a `client` pytest fixture would yield."""

  def __init__(self, *, widgets: FakeWidgets | None = None):
    self.widgets = widgets if widgets is not None else FakeWidgets()
    self.entered = False

  async def __aenter__(self):
    self.entered = True
    return self

  async def __aexit__(self, *exc_info):
    return False


class FakeFixtureRequest:
  """Stands in for pytest's `request` fixture, resolving exactly one named fixture."""

  def __init__(self, fixtures: dict):
    self._fixtures = fixtures

  def getfixturevalue(self, name: str):
    return self._fixtures[name]


def test_replays_an_example_with_its_api_named_key_translated(tmp_path: Path):
  _write_widget_get_spec(tmp_path)
  client = FakeClient()
  test_fn = build_http_replay_test(root=tmp_path)
  [example] = client_http_examples(tmp_path)

  asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))

  assert client.entered
  assert client.widgets.calls == [{'id': 'w1', 'extra_detail': True}]


def test_resolve_root_descends_into_the_given_attribute_before_binding(tmp_path: Path):
  # `resolve_root` names a namespace `endpoint.function` never itself repeats, mirroring
  # bybit's `client.http.market...` where `endpoint.function` is only `market...`.
  _write_widget_get_spec(tmp_path, function='get')
  widgets = FakeWidgets()
  client = FakeClient(widgets=widgets)
  test_fn = build_http_replay_test(root=tmp_path, resolve_root='widgets')
  [example] = client_http_examples(tmp_path)

  asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))

  assert widgets.calls == [{'id': 'w1', 'extra_detail': True}]


def test_an_endpoint_with_no_generated_method_is_skipped_not_failed(tmp_path: Path):
  _write_widget_get_spec(tmp_path)
  client = FakeClient(widgets=None)
  client.widgets = object()  # has no `.get`, unlike a real generated router
  test_fn = build_http_replay_test(root=tmp_path)
  [example] = client_http_examples(tmp_path)

  with pytest.raises(pytest.skip.Exception):
    asyncio.run(
      test_fn(example=example, request=FakeFixtureRequest({'client': client}))
    )


def _write_ticker_stream_spec(root: Path, *, with_message: bool = True):
  """A `kind: 'stream'` endpoint, kucoin-shaped: `endpoint.function` already carries its
  own `streams.` namespace, reachable straight off the client root."""
  spec_dir = root / 'spec' / 'endpoints' / 'streams' / 'ticker'
  (spec_dir / 'examples').mkdir(parents=True)
  (spec_dir / 'endpoint.json').write_text(
    json.dumps(
      {
        'function': 'streams.ticker',
        'spec': {
          'kind': 'stream',
          'channel': '/market/ticker:{symbol}',
          'openapi': {
            'description': 'Ticker pushes for one symbol.',
            'parameters': [
              {
                'name': 'symbol',
                'in': 'path',
                'required': True,
                'description': 'Trading pair symbol.',
                'schema': {'type': 'string'},
              },
            ],
            'responses': {'message': {'description': 'Ticker update.'}},
          },
        },
      }
    )
  )
  (spec_dir / 'examples' / 'w1.parameters.json').write_text(
    json.dumps({'parameters': {'symbol': 'BTC-USDT'}})
  )
  if with_message:
    (spec_dir / 'examples' / 'w1.messages.json').write_text(
      json.dumps([{'price': '100.5'}])
    )
  else:
    # Paired via a recorded subscribe ack alone -- no push ever captured, mirroring
    # kucoin's `unverified`/`requires_state` streams (`streams.futures.order` and
    # siblings): `client_ws_examples` still loads the example, since it pairs on
    # `parameters` + (`reply` | `messages` | `message_frames`), not `messages` alone.
    (spec_dir / 'examples' / 'w1.reply.json').write_text(json.dumps({'type': 'ack'}))


class FakeStreams:
  """A fake stream router standing in for a generated client's own, e.g.
  `client.streams`."""

  def __init__(self):
    self.subscribed = []
    self.unsubscribed = []

  def ticker(self, symbol: str) -> StreamManager:
    self.subscribed.append(symbol)

    async def connect() -> Stream:
      async def notifications():
        yield {'price': '100.5'}

      async def unsubscribe():
        self.unsubscribed.append(symbol)
        return {'type': 'ack'}

      return Stream(reply={'type': 'ack'}, stream=notifications(), unsubscribe=unsubscribe)

    return StreamManager(connect=connect)


class FakeWsClient:
  """A fake client standing in for what a `client` pytest fixture would yield, for
  `build_ws_replay_test`."""

  def __init__(self, *, streams: 'FakeStreams | object | None' = None):
    self.streams = streams if streams is not None else FakeStreams()
    self.entered = False

  async def __aenter__(self):
    self.entered = True
    return self

  async def __aexit__(self, *exc_info):
    return False


def test_ws_replays_an_example_with_its_recorded_push(tmp_path: Path):
  _write_ticker_stream_spec(tmp_path)
  streams = FakeStreams()
  client = FakeWsClient(streams=streams)
  test_fn = build_ws_replay_test(root=tmp_path)
  [example] = client_ws_examples(tmp_path)

  asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))

  assert client.entered
  assert streams.subscribed == ['BTC-USDT']
  assert streams.unsubscribed == ['BTC-USDT']


def test_ws_an_endpoint_with_no_generated_method_is_skipped_not_failed(tmp_path: Path):
  _write_ticker_stream_spec(tmp_path)
  client = FakeWsClient(streams=object())  # has no `.ticker`, unlike a real stream router
  test_fn = build_ws_replay_test(root=tmp_path)
  [example] = client_ws_examples(tmp_path)

  with pytest.raises(pytest.skip.Exception):
    asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))


def test_ws_an_example_with_no_recorded_push_is_skipped_not_failed(tmp_path: Path):
  _write_ticker_stream_spec(tmp_path, with_message=False)
  streams = FakeStreams()
  client = FakeWsClient(streams=streams)
  test_fn = build_ws_replay_test(root=tmp_path)
  [example] = client_ws_examples(tmp_path)

  with pytest.raises(pytest.skip.Exception):
    asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))

  # Skipped before ever subscribing -- nothing to replay past the subscribe step.
  assert streams.subscribed == []


def _write_rpc_echo_spec(root: Path, *, transports: tuple = ('ws',)) -> Path:
  """A `kind: 'rpc'` endpoint over `transports`, e.g. `('ws',)` for a native WS-RPC
  capture or `('http', 'ws')` for a dual-transport endpoint synthesis can build a WS
  example for (`truewire.spec.repo.ws_examples`)."""
  spec_dir = root / 'spec' / 'endpoints' / 'rpc' / 'echo'
  (spec_dir / 'examples').mkdir(parents=True)
  (spec_dir / 'endpoint.json').write_text(
    json.dumps(
      {
        'function': 'rpc.echo',
        'spec': {
          'kind': 'rpc',
          'transports': list(transports),
          'method': 'POST',
          'path': 'public/echo',
          'openapi': {
            'description': 'Echo one message back.',
            'parameters': [
              {
                'name': 'message',
                'in': 'query',
                'required': True,
                'description': 'Message to echo.',
                'schema': {'type': 'string'},
              },
            ],
            'responses': {
              '200': {
                'description': 'Echoed message.',
                'content': {
                  'application/json': {
                    'schema': {
                      'title': 'EchoResult',
                      'type': 'object',
                      'description': 'Echo result.',
                      'properties': {
                        'message': {'type': 'string', 'description': 'Echoed message.'}
                      },
                    }
                  }
                },
              },
            },
          },
        },
        'envelope': {'payload': 'result', 'correlate': 'id'},
      }
    )
  )
  return spec_dir


class FakeRpc:
  """A fake RPC sub-router standing in for a generated client's own, e.g. `client.rpc`."""

  def __init__(self):
    self.calls = []

  async def echo(self, *, message: str):
    self.calls.append({'message': message})
    return {'message': message}


class FakeRpcClient:
  """A fake client standing in for what a `client` pytest fixture would yield, for
  `build_ws_rpc_replay_test`."""

  def __init__(self, *, rpc: 'FakeRpc | object | None' = None):
    self.rpc = rpc if rpc is not None else FakeRpc()
    self.entered = False

  async def __aenter__(self):
    self.entered = True
    return self

  async def __aexit__(self, *exc_info):
    return False


def _sole_rpc_example(root: Path):
  [example] = [e for e in client_ws_examples(root) if e.endpoint.spec.kind == 'rpc']
  return example


def test_ws_rpc_replays_a_natively_captured_example(tmp_path: Path):
  spec_dir = _write_rpc_echo_spec(tmp_path, transports=('ws',))
  (spec_dir / 'examples' / '01.parameters.json').write_text(
    json.dumps(
      {
        'parameters': {'message': 'hi'},
        'payload': {'jsonrpc': '2.0', 'id': 0, 'method': 'public/echo', 'params': {'message': 'hi'}},
      }
    )
  )
  (spec_dir / 'examples' / '01.reply.json').write_text(
    json.dumps({'jsonrpc': '2.0', 'id': 0, 'result': {'message': 'hi'}})
  )

  rpc = FakeRpc()
  client = FakeRpcClient(rpc=rpc)
  test_fn = build_ws_rpc_replay_test(root=tmp_path)
  example = _sole_rpc_example(tmp_path)

  asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))

  assert client.entered
  assert rpc.calls == [{'message': 'hi'}]


def test_ws_rpc_replays_an_example_synthesized_from_an_http_only_capture(tmp_path: Path):
  """The promoted builder benefits from `ws_examples`' HTTP-to-WS synthesis for free --
  this endpoint has no native `.parameters.json`/`.reply.json` at all, only an HTTP
  capture, and still replays correctly through `build_ws_rpc_replay_test`."""
  spec_dir = _write_rpc_echo_spec(tmp_path, transports=('http', 'ws'))
  (spec_dir / 'examples' / '01.request.json').write_text(
    json.dumps({'parameters': {'message': 'hi'}})
  )
  (spec_dir / 'examples' / '01.response.json').write_text(
    json.dumps({'status': 200, 'payload': {'jsonrpc': '2.0', 'id': 0, 'result': {'message': 'hi'}}})
  )

  rpc = FakeRpc()
  client = FakeRpcClient(rpc=rpc)
  test_fn = build_ws_rpc_replay_test(root=tmp_path)
  example = _sole_rpc_example(tmp_path)

  asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))

  assert rpc.calls == [{'message': 'hi'}]


def test_ws_rpc_an_endpoint_with_no_generated_method_is_skipped_not_failed(tmp_path: Path):
  spec_dir = _write_rpc_echo_spec(tmp_path, transports=('ws',))
  (spec_dir / 'examples' / '01.parameters.json').write_text(
    json.dumps({'parameters': {'message': 'hi'}})
  )
  (spec_dir / 'examples' / '01.reply.json').write_text(
    json.dumps({'jsonrpc': '2.0', 'id': 0, 'result': {'message': 'hi'}})
  )

  client = FakeRpcClient(rpc=object())  # has no `.echo`, unlike a real generated router
  test_fn = build_ws_rpc_replay_test(root=tmp_path)
  example = _sole_rpc_example(tmp_path)

  with pytest.raises(pytest.skip.Exception):
    asyncio.run(test_fn(example=example, request=FakeFixtureRequest({'client': client})))
