"""
`match.ignore` (ADR 0018): located request paths `truewire mock` leaves out of a comparison.

`fixtures/mock_server_match_ignore/` is owned by this suite alone. `ws/login` is a
Bitget-shaped WS login -- `{"op": "login", "args": [{apiKey, timestamp, sign}]}` -- whose
per-connection `timestamp`/`sign` sit inside a positional array, out of reach of `redacted`'s
flat names. `rpc/place_order` is a signed JSON-RPC call over HTTP carrying a per-call `nonce`
and `signature` inside `params[0]`.
"""

import json
from pathlib import Path

import pytest
import websockets
from pydantic import ValidationError

from truewire.mock import (
  UnexpectedSubscriptionParameters,
  WsMockRegistry,
  _ignore_paths,
  _request_match,
  load_http_examples,
  load_ws_examples,
  running_mock_servers,
)
from truewire.spec.endpoint import MatchSpec


ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_match_ignore'


def _login(api_key: str = 'FAKE-KEY', timestamp: str = '1757930000', sign: str = 'fresh') -> dict:
  return {'op': 'login', 'args': [{'apiKey': api_key, 'timestamp': timestamp, 'sign': sign}]}


def test_ignore_paths_drops_keys_and_elements_without_mutating():
  frame = {'op': 'login', 'args': [{'sign': 's', 'apiKey': 'k'}, 'tail'], 'id': 3}
  assert _ignore_paths(frame, ('args[0].sign',)) == {'op': 'login', 'args': [{'apiKey': 'k'}, 'tail'], 'id': 3}
  assert _ignore_paths(frame, ('args[-1]',)) == {'op': 'login', 'args': [{'sign': 's', 'apiKey': 'k'}, None], 'id': 3}
  assert _ignore_paths(frame, ('id', 'args[0].apiKey')) == {'op': 'login', 'args': [{'sign': 's'}, 'tail']}
  assert frame == {'op': 'login', 'args': [{'sign': 's', 'apiKey': 'k'}, 'tail'], 'id': 3}


def test_ignore_paths_keeps_positions_for_later_indices():
  frame = {'op': 'auth', 'args': ['key', 1757930000, 'signature']}
  assert _ignore_paths(frame, ('args[1]', 'args[2]')) == {'op': 'auth', 'args': ['key', None, None]}


def test_ignore_paths_skips_a_path_that_does_not_resolve():
  frame = {'args': [{'sign': 's'}]}
  assert _ignore_paths(frame, ('args[3].sign', 'params.sign', 'args[0].sign.deeper')) == frame
  assert _ignore_paths(['a'], ('key',)) == ['a']


@pytest.mark.parametrize('path', ['$.args[0].sign', 'args[*].sign', 'args[0:1]', 'args..sign'])
def test_match_ignore_refuses_anything_beyond_the_response_path_grammar(path):
  with pytest.raises(ValidationError):
    MatchSpec.model_validate({'ignore': [path]})


def test_match_ignore_refuses_an_empty_list_and_unknown_keys():
  with pytest.raises(ValidationError):
    MatchSpec.model_validate({'ignore': []})
  with pytest.raises(ValidationError):
    MatchSpec.model_validate({'ignore': ['sign'], 'order': 'any'})


def test_ws_loader_carries_the_declared_paths():
  (example,) = load_ws_examples(ROOT)
  assert example.ignored_paths == ('args[0].timestamp', 'args[0].sign')


def test_ws_rpc_matches_a_freshly_signed_positional_login():
  registry = WsMockRegistry(ROOT)
  match = registry.match_rpc(_login())
  assert match is not None and match.example.function == 'acme.ws_login'


def test_ws_rpc_still_compares_every_field_not_ignored():
  registry = WsMockRegistry(ROOT)
  with pytest.raises(UnexpectedSubscriptionParameters):
    registry.match_rpc(_login(api_key='OTHER-KEY'))
  with pytest.raises(UnexpectedSubscriptionParameters):
    registry.match_rpc({'op': 'login', 'args': []})


@pytest.mark.asyncio
async def test_ws_mock_answers_a_login_signed_at_run_time():
  with running_mock_servers(root=ROOT) as servers:
    assert servers.ws_server is not None
    async with websockets.connect(servers.ws_server.url) as ws:
      await ws.send(json.dumps(_login(timestamp='1757939999', sign='signed-just-now')))
      reply = json.loads(await ws.recv())
      await ws.send(json.dumps(_login(api_key='OTHER-KEY')))
      error = json.loads(await ws.recv())

  assert reply == {'event': 'login', 'code': 0}
  assert error['message'] == 'unexpected_parameters'


def _http_example():
  (example,) = load_http_examples(ROOT)
  return example


def _place_order(**fields) -> dict:
  params = {'symbol': 'ACME', 'nonce': '98765', 'signature': 'signed-just-now', **fields}
  return {'jsonrpc': '2.0', 'id': 42, 'method': 'acme_placeOrder', 'params': [params]}


def test_http_rpc_matches_a_freshly_signed_body():
  example = _http_example()
  assert example.ignored_paths == ('params[0].nonce', 'params[0].signature')
  assert _request_match(example, '/', [], _place_order())


def test_http_rpc_matches_when_the_volatile_fields_are_absent():
  body = _place_order()
  del body['params'][0]['nonce'], body['params'][0]['signature']
  assert _request_match(_http_example(), '/', [], body)


def test_http_rpc_still_compares_every_field_not_ignored():
  assert not _request_match(_http_example(), '/', [], _place_order(symbol='OTHER'))
