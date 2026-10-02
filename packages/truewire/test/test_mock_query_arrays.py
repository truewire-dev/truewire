"""
`match.query_arrays` (ADR 0019): how `truewire mock` expects a list in the query string.

`fixtures/mock_server_query_arrays/` is owned by this suite alone. `stations/list_stations`
declares `comma`, the way api.weather.gov reads its filters (`?id=KSEA,KPDX`; a repeated key
keeps only the last value there). `stations/list_repeated` declares nothing and is the
control: a list still travels as one key per value.
"""

import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from truewire.mock import _join_query_arrays, load_http_examples, running_mock_servers
from truewire.spec.endpoint import MatchSpec


ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_query_arrays'


def _get(base: str, query: str) -> tuple[int, dict]:
  try:
    with urlopen(f'{base}{query}') as response:
      return response.status, json.loads(response.read())
  except HTTPError as error:
    return error.code, json.loads(error.read())


def test_join_renders_items_like_a_repeated_key_and_drops_an_empty_list():
  assert _join_query_arrays(
    {'id': ['KSEA', 'KPDX'], 'on': [True, False], 'n': [1, 2.5], 'none': [], 'limit': 2}
  ) == {
    'id': 'KSEA,KPDX',
    'on': 'true,false',
    'n': '1,2.5',
    'limit': 2,
  }


def test_match_query_arrays_takes_repeat_or_comma_only():
  assert MatchSpec.model_validate({'query_arrays': 'comma'}).query_arrays == 'comma'
  assert MatchSpec.model_validate({'query_arrays': 'repeat'}).ignore is None
  assert MatchSpec.model_validate({'ignore': ['sign']}).query_arrays == 'repeat'
  with pytest.raises(ValidationError):
    MatchSpec.model_validate({'query_arrays': 'pipe'})


def test_match_refuses_a_block_with_no_rule():
  with pytest.raises(ValidationError, match='declares no rule'):
    MatchSpec.model_validate({})
  with pytest.raises(ValidationError, match='declares no rule'):
    MatchSpec.model_validate({'ignore': None})


def test_loader_preserves_lists_and_the_declared_wire_form():
  examples = {example.function: example for example in load_http_examples(ROOT)}
  assert examples['acme.list_stations'].expected_query == {
    'id': ['KSEA', 'KPDX'],
    'active': [True],
    'state': [],
    'limit': 2,
  }
  assert examples['acme.list_stations'].query_arrays == 'comma'
  assert examples['acme.list_repeated'].query_arrays == 'repeat'
  assert examples['acme.list_repeated'].expected_query == {'id': ['KSEA', 'KPDX']}


def test_mock_answers_one_comma_separated_item_and_refuses_repeated_keys():
  with running_mock_servers(root=ROOT) as servers:
    base = servers.http_base_url
    joined = _get(base, '/stations?id=KSEA%2CKPDX&active=true&limit=2')
    unencoded = _get(base, '/stations?id=KSEA,KPDX&active=true&limit=2')
    repeated = _get(base, '/stations?id=KSEA&id=KPDX&active=true&limit=2')
    reordered = _get(base, '/stations?id=KPDX,KSEA&active=true&limit=2')
    control_repeated = _get(base, '/repeated?id=KSEA&id=KPDX')
    control_joined = _get(base, '/repeated?id=KSEA,KPDX')

  assert joined == (200, {'features': [{'id': 'KPDX'}, {'id': 'KSEA'}]})
  assert unencoded == joined
  assert repeated[0] == 422 and repeated[1]['error'] == 'unexpected_parameters'
  assert reordered[0] == 422
  assert control_repeated == (200, {'features': [{'id': 'KPDX'}, {'id': 'KSEA'}]})
  assert control_joined[0] == 422


def _endpoint(root: Path, name: str, method: str, properties: dict, match: dict | None, request: dict) -> None:
  folder = root / 'spec' / 'endpoints' / 'probe' / name
  (folder / 'examples').mkdir(parents=True)
  endpoint = {
    'meta': {},
    'function': f'acme.{name}',
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': f'/{name}',
      'method': method,
      'request': {'title': f'{name}Request', 'type': 'object', 'properties': properties, 'required': []},
      'response': {'type': 'object'},
      'description': name,
    },
  }
  if match is not None:
    endpoint['match'] = match
  (folder / 'endpoint.json').write_text(json.dumps(endpoint))
  (folder / 'examples' / 'one.request.json').write_text(json.dumps(request))
  (folder / 'examples' / 'one.response.json').write_text(json.dumps({'status': 200, 'payload': {'ok': 1}}))


def _call(
  base: str, path: str, body: bytes | None = None, *, content_type: str = 'application/json'
) -> tuple[int, dict]:
  headers = {'Content-Type': content_type} if body is not None else {}
  request = Request(base + path, data=body, headers=headers, method='POST' if body is not None else 'GET')
  try:
    with urlopen(request) as response:
      return response.status, json.loads(response.read())
  except HTTPError as error:
    return error.code, json.loads(error.read())


COMMA = {'query_arrays': 'comma'}
NUMBERS = {'n': {'type': 'array', 'items': {'type': 'number'}}}
FLAGS = {'on': {'type': 'array', 'items': {'type': 'boolean'}}}
IDS = {'id': {'type': 'array', 'items': {'type': 'string'}}}


def test_comma_items_keep_the_number_and_bool_tolerance(tmp_path):
  """Both query forms keep the existing numeric and boolean scalar tolerance."""
  _endpoint(tmp_path, 'n_repeat', 'GET', NUMBERS, None, {'request': {'n': [1, 2.5]}})
  _endpoint(tmp_path, 'n_comma', 'GET', NUMBERS, COMMA, {'request': {'n': [1, 2.5]}})
  _endpoint(tmp_path, 'on_comma', 'GET', FLAGS, COMMA, {'request': {'on': [True, False]}})
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    assert _call(base, '/n_repeat?n=1.0&n=2.5')[0] == 200  # control: tolerated today
    assert _call(base, '/n_comma?n=1.0,2.5')[0] == 200
    assert _call(base, '/on_comma?on=True,False')[0] == 200


def test_a_wrong_form_422_names_the_expected_wire_form(tmp_path):
  """A wrong-form diagnostic explains why matching values were refused."""
  _endpoint(tmp_path, 'ids', 'GET', IDS, COMMA, {'request': {'id': ['KSEA', 'KPDX']}})
  with running_mock_servers(root=tmp_path) as servers:
    status, body = _call(servers.http_base_url, '/ids?id=KSEA&id=KPDX')
  assert status == 422
  assert 'KSEA,KPDX' in json.dumps(body) or 'comma' in json.dumps(body)


def test_a_json_body_list_is_not_a_query_string_list(tmp_path):
  """Could: a query-role list pooled from a JSON body is a JSON array, not `a,b` text."""
  _endpoint(tmp_path, 'post_repeat', 'POST', IDS, None, {'parameters': {'id': ['KSEA', 'KPDX']}})
  _endpoint(tmp_path, 'post_comma', 'POST', IDS, COMMA, {'parameters': {'id': ['KSEA', 'KPDX']}})
  body = json.dumps({'id': ['KSEA', 'KPDX']}).encode()
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    assert _call(base, '/post_repeat', body)[0] == 200  # control
    assert _call(base, '/post_comma', body)[0] == 200


def test_comma_lists_preserve_order_multiplicity_and_scalar_commas(tmp_path):
  properties = {**NUMBERS, **FLAGS, 'label': {'type': 'string'}}
  _endpoint(tmp_path, 'items', 'GET', properties, COMMA,
            {'request': {'n': [2.5, 1, 1], 'on': [True, False], 'label': '1,2'}})
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    valid = '/items?n=2.5,1.0,1&on=True,FALSE&label=1,2'
    assert _call(base, valid)[0] == 200
    for invalid in (
      valid.replace('2.5,1.0,1', '1,1,2.5'),  # same values, different order
      valid.replace('2.5,1.0,1', '2.5,1'),  # missing duplicate item
      valid.replace('2.5,1.0,1', '2.5,1,1,1'),  # extra duplicate item
      valid.replace('2.5,1.0,1', '2.5,1,1,'),  # trailing empty item
      valid.replace('2.5,1.0,1', '2.5,1,1.1'),  # tolerance is per item, not loose
      valid.replace('2.5,1.0,1', '2.5&n=1&n=1'),  # repeated keys
      valid.replace('2.5,1.0,1', '2.5,1,1&n=2.5,1,1'),
      valid.replace('True,FALSE', 'false,true'),
      valid.replace('label=1,2', 'label=1&label=2'),
      valid.replace('label=1,2', 'label=1.0,2'),  # scalar string stays whole
    ):
      assert _call(base, invalid)[0] == 422, invalid


def test_empty_and_singleton_comma_lists(tmp_path):
  _endpoint(tmp_path, 'empty', 'GET', IDS, COMMA, {'request': {'id': []}})
  _endpoint(tmp_path, 'single', 'GET', IDS, COMMA, {'request': {'id': ['a']}})
  _endpoint(tmp_path, 'embedded', 'GET', IDS, COMMA, {'request': {'id': ['a,b']}})
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    for path in ('/empty', '/single?id=a', '/embedded?id=a%2Cb'):
      assert _call(base, path)[0] == 200
    for path in ('/empty?id=', '/single', '/single?id=a&id=a', '/single?id=a,a'):
      assert _call(base, path)[0] == 422


def test_json_pooling_preserves_structure_and_rejects_query_duplicates(tmp_path):
  properties = {**IDS, 'limit': {'type': 'integer'}, 'objects': {'type': 'array', 'items': {'type': 'object'}}}
  recorded = {'id': ['b', 'a', 'a'], 'limit': 2, 'objects': [{'x': 1, 'y': [2, 3]}]}
  _endpoint(tmp_path, 'pooled', 'POST', properties, COMMA, {'request': recorded})
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    body = {'id': recorded['id'], 'objects': [{'y': [2, 3], 'x': 1}]}
    assert _call(base, '/pooled?limit=2', json.dumps(body).encode())[0] == 200
    for ids in (['a', 'a', 'b'], ['b', 'a'], ['b', 'a', 'a', 'a'], [['b', 'a', 'a']]):
      assert _call(base, '/pooled?limit=2', json.dumps({**body, 'id': ids}).encode())[0] == 422
    for query in ('&id=b&id=a&id=a', '&id=b,a,a'):
      assert _call(base, '/pooled?limit=2' + query, json.dumps(body).encode())[0] == 422
    # The query string still has to use comma form when it carries the array itself.
    objects_only = json.dumps({'objects': body['objects']}).encode()
    assert _call(base, '/pooled?limit=2&id=b,a,a', objects_only)[0] == 200
    assert _call(base, '/pooled?limit=2&id=b&id=a&id=a', objects_only)[0] == 422


@pytest.mark.parametrize('request_key', ['request', 'parameters'])
@pytest.mark.parametrize('ambiguous', [False, True])
def test_diagnostics_explain_form_without_disclosing_redacted_fields(tmp_path, request_key, ambiguous):
  _endpoint(tmp_path, 'private', 'GET', {**IDS, 'credential': {'type': 'array', 'items': {'type': 'string'}}},
            COMMA, {request_key: {'id': ['a', 'b'], 'credential': ['recorded-secret-placeholder']}})
  folder = tmp_path / 'spec' / 'endpoints' / 'probe' / 'private'
  endpoint_file = folder / 'endpoint.json'
  endpoint = json.loads(endpoint_file.read_text())
  endpoint['redacted'] = ['credential']
  endpoint_file.write_text(json.dumps(endpoint))
  if ambiguous:
    for kind in ('request', 'response'):
      (folder / 'examples' / f'two.{kind}.json').write_text(
        (folder / 'examples' / f'one.{kind}.json').read_text())
  with running_mock_servers(root=tmp_path) as servers:
    query = 'id=a,b' if ambiguous else 'id=a&id=b'
    status, body = _call(servers.http_base_url, '/private?' + query + '&credential=incoming-secret-placeholder')
  assert status == (409 if ambiguous else 422)
  assert all(candidate['query_arrays'] == 'comma' for candidate in body['candidates'])
  assert all(candidate['expected_request'][request_key]['id'] == ['a', 'b'] for candidate in body['candidates'])
  diagnostic = json.dumps(body)
  assert 'credential' not in diagnostic
  assert 'recorded-secret-placeholder' not in diagnostic
  assert 'incoming-secret-placeholder' not in diagnostic


def test_json_body_bool_list_vs_number_recording(tmp_path):
  """comma JSON path uses Python == : True == 1."""
  _endpoint(tmp_path, 'pr', 'POST', NUMBERS, None, {'parameters': {'n': [1, 0]}})
  _endpoint(tmp_path, 'pc', 'POST', NUMBERS, COMMA, {'parameters': {'n': [1, 0]}})
  body = json.dumps({'n': [True, False]}).encode()
  with running_mock_servers(root=tmp_path) as s:
    b = s.http_base_url
    r, c = _call(b, '/pr', body)[0], _call(b, '/pc', body)[0]
  assert (r, c) == (422, 422), (r, c)


def test_json_body_comma_string_for_recorded_array(tmp_path):
  """ADR 0019: JSON arrays are compared structurally, not as comma text."""
  _endpoint(tmp_path, 'pr', 'POST', IDS, None, {'parameters': {'id': ['A', 'B']}})
  _endpoint(tmp_path, 'pc', 'POST', IDS, COMMA, {'parameters': {'id': ['A', 'B']}})
  body = json.dumps({'id': 'A,B'}).encode()
  with running_mock_servers(root=tmp_path) as s:
    b = s.http_base_url
    r, c = _call(b, '/pr', body)[0], _call(b, '/pc', body)[0]
  assert (r, c) == (422, 422), (r, c)


def test_form_body_comma_string_matches_recorded_array(tmp_path):
  _endpoint(tmp_path, 'form', 'POST', IDS, COMMA, {'parameters': {'id': ['A', 'B']}})
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    form = {'content_type': 'application/x-www-form-urlencoded; charset=utf-8'}
    assert _call(base, '/form', b'id=A%2CB', **form)[0] == 200
    assert _call(base, '/form?id=A,B', b'id=A%2CB', **form)[0] == 422
    assert _call(base, '/form?id=A&id=B', b'id=A%2CB', **form)[0] == 422
    assert _call(base, '/form', b'id=B%2CA', **form)[0] == 422


@pytest.mark.parametrize(('recorded', 'valid', 'invalid'), [
  ([1, 0], [1.0, 0.0], [True, False]),
  ([True, False], [True, False], [1, 0]),
  ([{'x': [1, {'y': False}], 'z': None}],
   [{'z': None, 'x': [1.0, {'y': False}]}],
   [{'z': None, 'x': [1, {'y': 0}]}]),
  ([[{'x': True}]], [[{'x': True}]], [[{'x': 1}]]),
  ([[{'x': 1}]], [[{'x': 1.0}]], [[{'x': True}]]),
])
def test_pooled_json_arrays_distinguish_booleans_recursively(tmp_path, recorded, valid, invalid):
  _endpoint(tmp_path, 'nested', 'POST', {'items': {'type': 'array', 'items': {}}},
            COMMA, {'request': {'items': recorded}})
  with running_mock_servers(root=tmp_path) as servers:
    base = servers.http_base_url
    assert _call(base, '/nested', json.dumps({'items': valid}).encode())[0] == 200
    assert _call(base, '/nested', json.dumps({'items': invalid}).encode())[0] == 422


@pytest.mark.parametrize(('recorded', 'invalid'), [([], ''), (['A'], 'A'), (['A', 'B'], 'A,B')])
def test_pooled_json_array_requires_an_array_even_for_zero_or_one_item(tmp_path, recorded, invalid):
  _endpoint(tmp_path, 'array', 'POST', IDS, COMMA, {'request': {'id': recorded}})
  with running_mock_servers(root=tmp_path) as servers:
    assert _call(servers.http_base_url, '/array', json.dumps({'id': invalid}).encode())[0] == 422
