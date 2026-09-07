"""`truewire migrate`: wrap a response schema written for the unwrapped value into the wire
frame its recordings show (ADR 0010), refusing to guess a frame nobody recorded, and
changing nothing on a second run."""
import json
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app

WIRE = 'Wire envelope field; see the core.'

PET = {
  'title': 'Pet', 'type': 'object', 'description': 'One pet.', 'required': ['id'],
  'properties': {'id': {'type': 'integer', 'description': 'Pet id.'}},
}


def endpoint_json(response: dict, *, payload: str = 'result', transports: list | None = None, request: dict | None = None) -> dict:
  spec = {
    'kind': 'rpc', 'transports': transports or ['http'], 'path': '/pets', 'method': 'GET',
    'description': 'List pets.',
    'request': request or {
      'title': 'ListPetsRequest', 'type': 'object',
      'properties': {'kind': {'type': 'string', 'description': 'Kind of pet.'}},
    },
    'response': response,
  }
  if transports == ['ws']:
    spec['path'] = 'list_pets'
    del spec['method']
  return {
    'docs': 'https://example.com/docs/pets', 'meta': {'public': True}, 'spec': spec,
    'envelope': {'payload': payload},
  }


def make_project(tmp_path: Path, monkeypatch) -> Path:
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'demo']).exit_code == 0
  group = tmp_path / 'demo' / 'spec' / 'endpoints' / 'pets'
  group.mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Pets.', 'upstream': 'https://example.com/docs', 'core': 'default',
  }))
  return tmp_path / 'demo'


def write_endpoint(project: Path, name: str, data: dict, *, frames: list | None = None, ws: bool = False) -> Path:
  endpoint_dir = project / 'spec' / 'endpoints' / 'pets' / name
  (endpoint_dir / 'examples').mkdir(parents=True)
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(data, indent=2) + '\n')
  for index, frame in enumerate(frames or []):
    if ws:
      (endpoint_dir / 'examples' / f'e{index}.parameters.json').write_text(json.dumps({'parameters': {}}))
      (endpoint_dir / 'examples' / f'e{index}.reply.json').write_text(json.dumps(frame))
    else:
      (endpoint_dir / 'examples' / f'e{index}.request.json').write_text(json.dumps({'request': {}}))
      (endpoint_dir / 'examples' / f'e{index}.response.json').write_text(json.dumps({'status': 200, 'payload': frame}))
  return endpoint_dir


def response_of(endpoint_dir: Path) -> dict:
  return json.loads((endpoint_dir / 'endpoint.json').read_text())['spec']['response']


def test_migrate_wraps_an_old_schema_from_its_recordings_and_is_idempotent(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  endpoint_dir = write_endpoint(project, 'list', endpoint_json(PET), frames=[
    {'error': [], 'result': {'id': 1}, 'time': 5, 'meta': {'a': 1}, 'flag': True},
    {'error': [], 'result': {'id': 2}, 'time': 6.5, 'meta': {}},
  ])
  runner = CliRunner()

  result = runner.invoke(app, ['migrate', '--project', str(project)])
  assert result.exit_code == 0, result.output
  assert 'migrated   pets.list  PetFrame (error, result, time, meta, flag)' in result.output
  assert 'Migrated 1 endpoint(s); 0 left as they are' in result.output
  assert '`error` (1)' in result.output

  response = response_of(endpoint_dir)
  assert response['title'] == 'PetFrame'
  assert response['type'] == 'object'
  assert response['description']
  assert list(response['properties']) == ['error', 'result', 'time', 'meta', 'flag']
  assert response['properties']['error'] == {'type': 'array', 'items': {'type': 'string'}, 'description': WIRE}
  assert response['properties']['result'] == PET
  assert response['properties']['time'] == {'anyOf': [{'type': 'integer'}, {'type': 'number'}], 'description': WIRE}
  assert response['properties']['meta'] == {'type': 'object', 'additionalProperties': True, 'description': WIRE}
  assert response['properties']['flag'] == {'type': 'boolean', 'description': WIRE}
  assert response['required'] == ['error', 'result', 'time', 'meta']

  checked = runner.invoke(app, ['check', '--project', str(project)])
  assert checked.exit_code == 0, checked.output

  before = (endpoint_dir / 'endpoint.json').read_bytes()
  again = runner.invoke(app, ['migrate', '--project', str(project)])
  assert again.exit_code == 0, again.output
  assert 'Migrated 0 endpoint(s); 1 left as they are' in again.output
  assert (endpoint_dir / 'endpoint.json').read_bytes() == before


def test_migrate_refuses_an_endpoint_with_no_recording(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  endpoint_dir = write_endpoint(project, 'list', endpoint_json(PET))
  before = (endpoint_dir / 'endpoint.json').read_bytes()

  result = CliRunner().invoke(app, ['migrate', '--project', str(project)])

  assert result.exit_code == 1
  assert 'Refused 1 endpoint(s):' in result.output
  assert 'pets.list: no recording to derive the frame from' in result.output
  assert '--template' in result.output
  assert (endpoint_dir / 'endpoint.json').read_bytes() == before


def test_migrate_template_stands_in_for_an_unrecorded_endpoint(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  write_endpoint(project, 'list', endpoint_json(PET), frames=[{'error': [], 'result': {'id': 1}}])
  bare = write_endpoint(project, 'create', endpoint_json({**PET, 'title': 'Created'}))
  runner = CliRunner()

  missing = runner.invoke(app, ['migrate', '--project', str(project), '--template', 'pets.nope'])
  assert missing.exit_code == 1
  assert 'no such endpoint' in missing.output

  result = runner.invoke(app, ['migrate', '--project', str(project), '--template', 'pets.list'])
  assert result.exit_code == 0, result.output
  assert 'Migrated 2 endpoint(s)' in result.output
  response = response_of(bare)
  assert response['title'] == 'CreatedFrame'
  assert list(response['properties']) == ['error', 'result']
  assert response['properties']['result']['title'] == 'Created'
  assert response['required'] == ['error', 'result']
  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0


def test_migrate_reads_a_ws_reply_frame(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  endpoint_dir = write_endpoint(
    project, 'list', endpoint_json(PET, transports=['ws']),
    frames=[{'jsonrpc': '2.0', 'id': 1, 'result': {'id': 1}}], ws=True,
  )
  result = CliRunner().invoke(app, ['migrate', '--project', str(project)])
  assert result.exit_code == 0, result.output
  response = response_of(endpoint_dir)
  assert list(response['properties']) == ['jsonrpc', 'id', 'result']
  assert response['properties']['id'] == {'type': 'integer', 'description': WIRE}
  assert response['required'] == ['jsonrpc', 'id', 'result']


def test_migrate_titles_a_map_response_from_the_request(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  ticker = {'type': 'object', 'description': 'Pair to ticker.', 'additionalProperties': {'type': 'string'}}
  request = {'title': 'TickerRequest', 'type': 'object', 'properties': {'pair': {'type': 'string', 'description': 'Pair.'}}}
  endpoint_dir = write_endpoint(
    project, 'ticker', endpoint_json(ticker, request=request), frames=[{'error': [], 'result': {'XBTUSD': '1'}}],
  )
  result = CliRunner().invoke(app, ['migrate', '--project', str(project)])
  assert result.exit_code == 0, result.output
  assert response_of(endpoint_dir)['title'] == 'TickerFrame'


def test_migrate_nests_a_deeper_payload_path(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  endpoint_dir = write_endpoint(
    project, 'list', endpoint_json(PET, payload='data.result'),
    frames=[{'ok': True, 'data': {'result': {'id': 1}, 'took': 3}}],
  )
  runner = CliRunner()
  result = runner.invoke(app, ['migrate', '--project', str(project)])
  assert result.exit_code == 0, result.output
  response = response_of(endpoint_dir)
  assert list(response['properties']) == ['ok', 'data']
  data = response['properties']['data']
  assert data['title'] == 'PetFrameData'
  assert list(data['properties']) == ['result', 'took']
  assert data['properties']['result'] == PET
  assert data['required'] == ['result', 'took']
  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0


def test_migrate_refuses_a_frame_that_lacks_the_declared_path(tmp_path: Path, monkeypatch):
  project = make_project(tmp_path, monkeypatch)
  write_endpoint(project, 'list', endpoint_json(PET), frames=[{'error': [], 'data': {'id': 1}}])
  result = CliRunner().invoke(app, ['migrate', '--project', str(project)])
  assert result.exit_code == 1
  assert 'no recorded frame carries `result`' in result.output
