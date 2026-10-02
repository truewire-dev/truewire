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


def test_migrate_takes_an_unrecorded_endpoints_frame_from_its_core(tmp_path: Path, monkeypatch):
  """With no `--template`, the recordings of endpoints sharing the core and the payload
  path stand in; the report says where each frame came from."""
  project = make_project(tmp_path, monkeypatch)
  write_endpoint(project, 'list', endpoint_json(PET), frames=[{'error': [], 'result': {'id': 1}}])
  write_endpoint(project, 'find', endpoint_json(PET), frames=[{'error': ['x'], 'result': {'id': 2}}])
  other = write_endpoint(project, 'other', endpoint_json(PET, payload='data'))
  bare = write_endpoint(project, 'create', endpoint_json({**PET, 'title': 'Created'}))
  report = tmp_path / 'report.json'

  result = CliRunner().invoke(app, ['migrate', '--project', str(project), '--report', str(report)])

  assert result.exit_code == 1
  assert 'pets.create  CreatedFrame (error, result)  [frame from recorded endpoints of the same core]' in result.output
  assert 'Migrated 3 endpoint(s);' in result.output
  assert 'Frames: 2 from their own recordings, 0 from --template, 1 from recorded endpoints of the same core.' in result.output
  assert 'pets.other: no recording to derive the frame from, and no recorded endpoint of core `default` declares `envelope.payload` `data`' in result.output
  response = response_of(bare)
  assert response['properties']['error'] == {'type': 'array', 'items': {'type': 'string'}, 'description': WIRE}
  assert response['required'] == ['error', 'result']
  assert 'properties' not in response_of(other) or 'data' not in response_of(other)['properties']
  outcome = json.loads(report.read_text())
  assert [entry['source'] for entry in outcome['migrated']] == ['core', 'recordings', 'recordings']
  assert outcome['refused'][0]['function'] == 'pets.other'


def test_migrate_wraps_an_old_schema_whose_own_property_shares_the_payload_key(tmp_path: Path, monkeypatch):
  """`result` resolves inside an old schema that happens to hold a `result` of its own;
  the recordings show it is still the unwrapped value, so it is wrapped all the same."""
  project = make_project(tmp_path, monkeypatch)
  page = {
    'title': 'Page', 'type': 'object', 'description': 'A page.', 'required': ['result', 'cursor'],
    'properties': {
      'result': {'type': 'array', 'items': {'type': 'integer'}, 'description': 'Rows.'},
      'cursor': {'type': 'string', 'description': 'Next cursor.'},
    },
  }
  endpoint_dir = write_endpoint(
    project, 'list', endpoint_json(page),
    frames=[{'retCode': 0, 'result': {'result': [1, 2], 'cursor': 'c'}}],
  )
  runner = CliRunner()

  result = runner.invoke(app, ['migrate', '--project', str(project)])

  assert result.exit_code == 0, result.output
  response = response_of(endpoint_dir)
  assert response['title'] == 'PageFrame'
  assert response['properties']['result']['title'] == 'Page'
  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
  again = runner.invoke(app, ['migrate', '--project', str(project)])
  assert 'Migrated 0 endpoint(s)' in again.output


def test_migrate_renames_a_shared_schema_a_router_group_collides_with(tmp_path: Path, monkeypatch):
  """Rule 18 refuses a group class named like a shared schema; `--rename-schema` renames
  the schema's id, title and every `$ref`, and a second run changes nothing."""
  project = make_project(tmp_path, monkeypatch)
  (project / 'spec' / 'schemas.json').write_text(json.dumps({
    'Pets': {'title': 'Pets', 'type': 'object', 'description': 'Some pets.', 'properties': {'ids': {'type': 'array', 'items': {'type': 'integer'}, 'description': 'Ids.'}}},
  }, indent=2) + '\n')
  wire = {'title': 'PetsFrame', 'type': 'object', 'description': 'Frame.', 'required': ['result'], 'properties': {'result': {'$ref': 'Pets'}}}
  endpoint_dir = write_endpoint(project, 'list', endpoint_json(wire), frames=[{'result': {'ids': [1]}}])
  runner = CliRunner()

  flagged = runner.invoke(app, ['migrate', '--project', str(project)])
  assert flagged.exit_code == 0, flagged.output
  assert "collision  spec/endpoints/pets/router.json: the router group 'pets' renders the class 'Pets'" in flagged.output
  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 1

  result = runner.invoke(app, ['migrate', '--project', str(project), '--rename-schema', 'Pets=PetList'])
  assert result.exit_code == 0, result.output
  assert 'renamed    schema Pets -> PetList (1 $ref(s), 2 file(s))' in result.output
  assert 'collision' not in result.output
  shared = json.loads((project / 'spec' / 'schemas.json').read_text())
  assert list(shared) == ['PetList'] and shared['PetList']['title'] == 'PetList'
  assert response_of(endpoint_dir)['properties']['result'] == {'$ref': 'PetList'}
  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0

  again = runner.invoke(app, ['migrate', '--project', str(project), '--rename-schema', 'Pets=PetList'])
  assert again.exit_code == 0 and '(already done)' in again.output
  missing = runner.invoke(app, ['migrate', '--project', str(project), '--rename-schema', 'Nope=Other'])
  assert missing.exit_code == 1 and 'no shared schema is called `Nope`' in missing.output
