"""`truewire conform`: every recorded request called once, the answer judged three ways.

The "live API" is `truewire mock` serving a copy of the project whose recordings have been
mutated, the way an API drifts: a key added or removed, a field retyped, an enum value
the spec never heard of, a new status code. The project under test keeps its original
recordings, so each mutation must come back as exactly the finding it is. The client-bug
case is the other way round: the served body is the recorded one, and the generated
client is the thing that changed.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import shutil
from pathlib import Path
import threading

from typer.testing import CliRunner
from typing_extensions import Any, Callable

from truewire.cli import app
from truewire.conform.schema import SchemaView
from truewire.conform.shape import shape_findings
from truewire.mock import running_mock_servers

from test_capture import patch_core, quickstart_project

GET_PET = 'pets/get_pet'


def served_copy(project: Path, tmp_path: Path) -> Path:
  """A copy of the project for the mock to serve, so its recordings can drift."""
  served = tmp_path / 'served'
  shutil.copytree(project, served, ignore=shutil.ignore_patterns('src'))
  return served


def mutate(served: Path, endpoint: str, change: Callable[[dict[str, Any]], None], example: str = 'default') -> None:
  """Edit one served response (`{"status": ..., "payload": ...}`) in place."""
  path = served / 'spec' / 'endpoints' / endpoint / 'examples' / f'{example}.response.json'
  response = json.loads(path.read_text())
  change(response)
  path.write_text(json.dumps(response, indent=2) + '\n')


def run_conform(project: Path, served: Path, state: Path, day: str, *extra: str):
  with running_mock_servers(served) as servers:
    return CliRunner().invoke(app, [
      'conform', '--project', str(project), '--state', str(state), '--date', day,
      '--new', f'base_url={servers.http_base_url}', '--interval', '0', *extra,
    ])


def report(state: Path, day: str) -> dict[str, Any]:
  return json.loads((state / 'petstore' / f'{day}.json').read_text())


def endpoint(data: dict[str, Any], function: str) -> dict[str, Any]:
  return next(e for e in data['endpoints'] if e['function'] == function)


def findings(data: dict[str, Any], function: str) -> list[dict[str, Any]]:
  return [f for example in endpoint(data, function).get('examples', []) for f in example.get('findings', [])]


def checks(data: dict[str, Any], function: str) -> set[tuple[str, str, str]]:
  return {(f['kind'], f['check'], f['pointer']) for f in findings(data, function)}


def test_an_unchanged_api_is_ok_and_every_skip_says_why(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'
  spec_before = sorted((p.relative_to(project), p.read_bytes()) for p in (project / 'spec').rglob('*') if p.is_file())

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 0, result.output
  data = report(state, '2026-10-01')
  statuses = {e['function']: (e['status'], e.get('reason')) for e in data['endpoints']}
  assert statuses == {
    'pets.create_pet': ('skipped', 'not_a_read'),
    'pets.delete_pet': ('skipped', 'no_recording'),
    'pets.get_pet': ('ok', None),
    'pets.get_pet_owner': ('ok', None),
    'pets.list_pets': ('ok', None),
    'store.get_inventory': ('ok', None),
    'store.get_order': ('skipped', 'no_recording'),
    'store.place_order': ('skipped', 'no_recording'),
  }
  assert data['counts'] == {'ok': 4, 'drift': 0, 'client': 0, 'error': 0, 'skipped': 4}
  assert endpoint(data, 'pets.get_pet')['examples'] == [
    {'id': 'default', 'status': 'ok', 'http_status': 200, 'recorded_status': 200, 'client': 'accepted'},
  ]
  markdown = (state / 'petstore' / '2026-10-01.md').read_text()
  assert '| 4 | 0 | 0 | 0 | 4 |' in markdown
  assert json.loads((state / 'petstore' / 'ledger.json').read_text())['findings'] == {}
  spec_after = sorted((p.relative_to(project), p.read_bytes()) for p in (project / 'spec').rglob('*') if p.is_file())
  assert spec_after == spec_before


def test_a_post_is_called_only_when_allowed(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'

  skipped = run_conform(project, served, state, '2026-09-30', '--only', 'pets.create_pet')
  assert skipped.exit_code == 0, skipped.output
  assert endpoint(report(state, '2026-09-30'), 'pets.create_pet')['detail'] == (
    'POST: phase 1 calls GET only; --allow names a read the API sends as a POST'
  )

  result = run_conform(project, served, state, '2026-10-01', '--only', 'pets.create_pet', '--allow', 'pets.create_pet')

  assert result.exit_code == 0, result.output
  data = report(state, '2026-10-01')
  assert [e['function'] for e in data['endpoints']] == ['pets.create_pet']
  assert endpoint(data, 'pets.create_pet')['status'] == 'ok'


def test_an_added_key_is_drift_against_the_recording(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  [finding] = findings(data, 'pets.get_pet')
  assert (finding['kind'], finding['check'], finding['pointer']) == ('drift', 'key_added', '/nickname')
  assert (finding['expected'], finding['actual'], finding['against']) == (None, ['string'], 'recording')
  assert endpoint(data, 'pets.get_pet')['examples'][0]['client'] == 'accepted'
  assert data['counts']['drift'] == 1


def test_a_removed_required_key_is_drift_and_an_optional_one_is_not(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)

  def drop(response):
    del response['payload']['created_at']  # required
    del response['payload']['category']  # optional: the schema allows its absence
  mutate(served, GET_PET, drop)
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  assert checks(data, 'pets.get_pet') == {('drift', 'key_removed', '/created_at')}
  [finding] = findings(data, 'pets.get_pet')
  assert finding['expected'] == ['string'] and finding['actual'] is None
  # The client rejects the body too, and that is the API's doing, not a client finding.
  assert endpoint(data, 'pets.get_pet')['examples'][0]['client'] == 'rejected (the body fails the schema)'


def test_a_retyped_field_is_drift(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r['payload'].update(id='42'))
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  [finding] = findings(data, 'pets.get_pet')
  assert (finding['check'], finding['pointer'], finding['expected'], finding['actual']) == (
    'type_changed', '/id', ['number'], ['string'],
  )
  assert finding['against'] == 'recording'


def test_a_retyped_array_element_is_one_finding_for_every_element(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, 'pets/list_pets', lambda r: [item.update(id=str(item['id'])) for item in r['payload']['items']],
         example='first_page')
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  [finding] = findings(data, 'pets.list_pets')
  assert (finding['check'], finding['pointer']) == ('type_changed', '/items/*/id')
  assert finding['count'] == 2


def test_a_new_enum_value_is_drift_and_names_the_value(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r['payload'].update(status='adopted'))
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  [finding] = findings(data, 'pets.get_pet')
  assert (finding['kind'], finding['check'], finding['pointer']) == ('drift', 'enum_value', '/status')
  assert finding['expected'] == ['available', 'pending', 'sold']
  assert finding['actual'] == 'adopted'
  assert finding['against'] == 'schema'


def test_a_new_status_code_is_drift(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)

  def gone(response):
    response['status'] = 404
    response['payload'] = {'code': 404, 'message': 'no such pet'}
  mutate(served, GET_PET, gone)
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  [finding] = findings(data, 'pets.get_pet')
  assert (finding['check'], finding['expected'], finding['actual']) == ('status', 200, 404)
  example = endpoint(data, 'pets.get_pet')['examples'][0]
  assert (example['status'], example['http_status']) == ('drift', 404)
  assert 'no such pet' not in json.dumps(data)


def test_a_body_the_schema_accepts_and_the_client_rejects_is_a_client_finding(tmp_path: Path, monkeypatch):
  """The API is as specced; the generated Python is what is wrong."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  schemas = project / 'src' / 'petstore' / 'schemas.py'
  source = schemas.read_text()
  wrong = source.replace(
    "  status: Literal['available', 'pending', 'sold']\n  \"\"\"Availability status.\"\"\"",
    "  status: Literal['available', 'pending']\n  \"\"\"Availability status.\"\"\"",
  )
  assert wrong != source
  schemas.write_text(wrong)
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  assert result.exit_code == 1, result.output
  data = report(state, '2026-10-01')
  [finding] = findings(data, 'pets.get_pet')
  assert (finding['kind'], finding['check'], finding['pointer']) == ('client:python', 'client', '/status')
  assert finding['actual'] == 'literal_error'
  assert endpoint(data, 'pets.get_pet')['status'] == 'client'
  assert data['counts']['client'] == 1
  assert "'sold'" not in json.dumps(finding)


def test_first_seen_carries_forward_and_a_finding_resolves(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  state = tmp_path / 'state'
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))

  first = run_conform(project, served, state, '2026-10-02')
  assert first.exit_code == 1, first.output
  [added] = findings(report(state, '2026-10-02'), 'pets.get_pet')
  assert added['first_seen'] == '2026-10-02'

  mutate(served, 'store/get_inventory', lambda r: r['payload'].update(sold='7'))
  second = run_conform(project, served, state, '2026-10-03')
  assert second.exit_code == 1, second.output
  data = report(state, '2026-10-03')
  [still] = findings(data, 'pets.get_pet')
  assert (still['fingerprint'], still['first_seen']) == (added['fingerprint'], '2026-10-02')
  [inventory] = findings(data, 'store.get_inventory')
  assert (inventory['check'], inventory['pointer'], inventory['first_seen']) == ('type_changed', '/*', '2026-10-03')
  assert '| 2026-10-02 |' in (state / 'petstore' / '2026-10-03.md').read_text()

  # A run that does not reach an endpoint proves nothing about it: its finding stays open.
  narrow = run_conform(project, served, state, '2026-10-04', '--only', 'store.*')
  assert narrow.exit_code == 1, narrow.output
  assert report(state, '2026-10-04')['resolved'] == []

  healed = served_copy(project, tmp_path / 'healed')
  third = run_conform(project, healed, state, '2026-10-05')
  assert third.exit_code == 0, third.output
  data = report(state, '2026-10-05')
  resolved = {(r['function'], r['check'], r['first_seen'], r['resolved']) for r in data['resolved']}
  assert resolved == {
    ('pets.get_pet', 'key_added', '2026-10-02', '2026-10-05'),
    ('store.get_inventory', 'type_changed', '2026-10-03', '2026-10-05'),
  }
  ledger = json.loads((state / 'petstore' / 'ledger.json').read_text())['findings']
  assert ledger[added['fingerprint']]['last_seen'] == '2026-10-03'
  assert ledger[added['fingerprint']]['resolved'] == '2026-10-05'
  assert 'Resolved today (2)' in (state / 'petstore' / '2026-10-05.md').read_text()

  # Back again: a new episode, dated from its return, remembering the last one.
  fourth = run_conform(project, served, state, '2026-10-06', '--only', 'pets.get_pet')
  assert fourth.exit_code == 1
  [again] = findings(report(state, '2026-10-06'), 'pets.get_pet')
  assert again['first_seen'] == '2026-10-06'
  ledger = json.loads((state / 'petstore' / 'ledger.json').read_text())['findings']
  assert ledger[added['fingerprint']]['previously_resolved'] == '2026-10-05'


def test_a_night_that_did_not_read_the_body_resolves_no_body_finding(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))
  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')
  [added] = findings(report(state, '2026-10-01'), 'pets.get_pet')

  # A 404 where the recording has 200 is a status finding; the body was never judged.
  gone = served_copy(project, tmp_path / 'gone')
  mutate(gone, GET_PET, lambda r: r.update(status=404, payload={'code': 404, 'message': 'x'}))
  run_conform(project, gone, state, '2026-10-02', '--only', 'pets.get_pet')
  data = report(state, '2026-10-02')
  assert [f['check'] for f in findings(data, 'pets.get_pet')] == ['status']
  assert data['resolved'] == []

  # The body is back: the status finding resolves, the body finding kept its date.
  run_conform(project, served, state, '2026-10-03', '--only', 'pets.get_pet')
  data = report(state, '2026-10-03')
  [still] = findings(data, 'pets.get_pet')
  assert (still['fingerprint'], still['first_seen']) == (added['fingerprint'], '2026-10-01')
  assert [r['check'] for r in data['resolved']] == ['status']


def test_a_body_that_is_not_json_resolves_no_body_finding(tmp_path: Path, monkeypatch):
  class Html(BaseHTTPRequestHandler):
    def do_GET(self):
      body = b'<html>maintenance</html>'
      self.send_response(200)
      self.send_header('Content-Type', 'text/html')
      self.send_header('Content-Length', str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, *args):
      pass

  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))
  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  server = HTTPServer(('127.0.0.1', 0), Html)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    CliRunner().invoke(app, [
      'conform', '--project', str(project), '--state', str(state), '--date', '2026-10-02', '--interval', '0',
      '--only', 'pets.get_pet', '--new', f'base_url=http://127.0.0.1:{server.server_port}',
    ])
  finally:
    server.shutdown()

  data = report(state, '2026-10-02')
  assert [f['check'] for f in findings(data, 'pets.get_pet')] == ['not_json']
  assert data['resolved'] == []


def test_a_client_finding_does_not_resolve_on_a_night_the_client_was_not_judged(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  schemas = project / 'src' / 'petstore' / 'schemas.py'
  schemas.write_text(schemas.read_text().replace(
    "  status: Literal['available', 'pending', 'sold']\n", "  status: Literal['available', 'pending']\n",
  ))
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'
  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')
  [client] = findings(report(state, '2026-10-01'), 'pets.get_pet')
  assert client['kind'] == 'client:python'

  # The body fails the schema tonight, so the client's rejection says nothing about it.
  mutate(served, GET_PET, lambda r: r['payload'].pop('created_at'))
  run_conform(project, served, state, '2026-10-02', '--only', 'pets.get_pet')

  assert report(state, '2026-10-02')['resolved'] == []


def test_a_call_the_core_refuses_for_want_of_credentials_is_skipped(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  toml = project / 'truewire.toml'
  source = toml.read_text()
  assert '[secrets]\nrequired = []\n' in source
  toml.write_text(source.replace('[secrets]\nrequired = []\n', '[secrets]\nrequired = ["PETSTORE_API_KEY"]\n'))
  monkeypatch.delenv('PETSTORE_API_KEY', raising=False)
  patch_core(project, before=(
    '    if not public:\n'
    '      from truewire_core.exceptions import AuthError\n'
    "      raise AuthError('no key')\n"
  ))
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet', '--only', 'store.get_inventory')

  assert result.exit_code == 0, result.output
  data = report(state, '2026-10-01')
  pet = endpoint(data, 'pets.get_pet')
  assert (pet['status'], pet['reason']) == ('skipped', 'missing_credentials')
  assert pet['detail'] == 'missing_credentials: PETSTORE_API_KEY not set'
  assert endpoint(data, 'store.get_inventory')['status'] == 'ok'  # public: no key needed


def test_state_inside_the_spec_tree_is_refused(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  result = CliRunner().invoke(app, ['conform', '--project', str(project), '--state', str(project / 'spec')])
  assert result.exit_code == 1
  assert 'must not be inside the spec tree' in result.output


def view(schema: dict[str, Any]) -> SchemaView:
  return SchemaView.root({**schema, '$defs': {}})


def test_map_keys_are_data_and_tuples_are_positional():
  """Kraken's shape: `result` maps a pair name to a ticker whose levels are tuples."""
  schema = {
    'type': 'object',
    'properties': {'result': {'type': 'object', 'additionalProperties': {
      'type': 'object', 'properties': {'a': {'type': 'array', 'prefixItems': [{'type': 'string'}, {'type': 'number'}]}},
    }}},
  }
  recorded = {'result': {'XXBTZUSD': {'a': ['1.0', 6]}}}
  assert shape_findings(recorded, {'result': {'XETHZUSD': {'a': ['2.0', 1]}, 'SOLUSD': {'a': ['3', 2]}}}, view(schema)) == []
  [finding] = shape_findings(recorded, {'result': {'XETHZUSD': {'a': ['2.0', '1']}}}, view(schema))
  assert (finding.check, finding.pointer) == ('type_changed', '/result/*/a/1')
  [finding] = shape_findings(recorded, {'result': {'XETHZUSD': {'a': ['2.0', 1, 3]}}}, view(schema))
  assert (finding.check, finding.pointer) == ('array_element', '/result/*/a')


def test_nullability_is_drift_only_where_the_schema_does_not_declare_it():
  schema = {'type': 'object', 'properties': {'a': {'type': ['string', 'null']}, 'b': {'type': 'string'}}}
  assert shape_findings({'a': 'x'}, {'a': None}, view(schema)) == []
  [finding] = shape_findings({'b': 'x'}, {'b': None}, view(schema))
  assert (finding.check, finding.pointer, finding.expected, finding.actual) == ('nullability', '/b', ['string'], ['null'])


def test_an_empty_array_says_nothing_about_its_elements():
  schema = {'type': 'object', 'properties': {'rows': {'type': 'array'}}}
  assert shape_findings({'rows': [{'a': 1}]}, {'rows': []}, view(schema)) == []
  assert shape_findings({'rows': []}, {'rows': [{'a': 1}]}, view(schema)) == []


def pin_pet_id(project: Path, pet_id: Any) -> None:
  """Point get_pet's recorded request at `pet_id`."""
  path = project / 'spec' / 'endpoints' / GET_PET / 'examples' / 'default.request.json'
  request = json.loads(path.read_text())
  key = 'request' if request.get('request') is not None else 'parameters'
  request[key]['petId'] = pet_id
  path.write_text(json.dumps(request, indent=2) + '\n')


def test_a_refused_request_that_pins_a_past_time_is_a_stale_recording_not_drift(tmp_path: Path, monkeypatch):
  """TRU-238: Binance answers 400 to a recorded window past its 30-day lookback. The API did
  not change; the recording aged. The finding an earlier night made is withdrawn, dated.
  list_pets' first page was recorded with `since=2026-01-01T00:00:00Z`."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)

  def refused(response):
    response['status'] = 400
    response['payload'] = {'code': -1130, 'msg': "parameter 'since' is invalid."}
  mutate(served, 'pets/list_pets', refused, example='first_page')
  state = tmp_path / 'state'
  only = ('--only', 'pets.list_pets')

  # While the pinned time is still ahead, a 400 is drift like any other status change.
  before = run_conform(project, served, state, '2025-12-01', *only)
  assert before.exit_code == 1, before.output
  [finding] = findings(report(state, '2025-12-01'), 'pets.list_pets')
  assert (finding['check'], finding['actual']) == ('status', 400)

  after = run_conform(project, served, state, '2026-10-01', *only)
  assert after.exit_code == 0, after.output
  data = report(state, '2026-10-01')
  pets = endpoint(data, 'pets.list_pets')
  assert (pets['status'], pets['reason']) == ('skipped', 'stale_recording')
  assert pets['detail'] == ('HTTP 400 where the recording has 200, and the recorded request pins '
                            'since (2026-01-01): re-record it')
  assert findings(data, 'pets.list_pets') == []
  [withdrawn] = data['resolved']
  assert (withdrawn['fingerprint'], withdrawn['first_seen'], withdrawn['resolved']) == (
    finding['fingerprint'], '2025-12-01', '2026-10-01')
  assert withdrawn['withdrawn'] == 'stale recording: pins since (2026-01-01)'
  assert "is invalid" not in json.dumps(data)


def test_a_refused_request_that_pins_nothing_is_still_drift(tmp_path: Path, monkeypatch):
  """`petId` 1785873900 falls in the epoch range (2026-08-04), but an id is not a time."""
  project = quickstart_project(tmp_path, monkeypatch)
  pin_pet_id(project, 1785873900)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r.update(status=400, payload={'msg': 'bad'}))
  state = tmp_path / 'state'

  result = run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  assert result.exit_code == 1, result.output
  assert endpoint(report(state, '2026-10-01'), 'pets.get_pet')['status'] == 'drift'


def test_a_finding_on_an_example_that_is_gone_or_now_private_is_withdrawn(tmp_path: Path, monkeypatch):
  """A finding conform can never judge again (the recording was removed, or the spec now
  says the endpoint is private and the run has no key) is closed, not left open forever."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))
  mutate(served, 'store/get_inventory', lambda r: r['payload'].update(sold='7'))
  state = tmp_path / 'state'
  first = run_conform(project, served, state, '2026-10-01')
  assert first.exit_code == 1, first.output

  examples = project / 'spec' / 'endpoints' / 'store' / 'get_inventory' / 'examples'
  for path in examples.glob('default.*'):
    path.rename(path.with_name(path.name.replace('default', 'renamed')))
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('[secrets]\nrequired = []\n', '[secrets]\nrequired = ["PETSTORE_API_KEY"]\n'))
  monkeypatch.delenv('PETSTORE_API_KEY', raising=False)
  patch_core(project, before=(
    '    if not public:\n'
    '      from truewire_core.exceptions import AuthError\n'
    "      raise AuthError('no key')\n"
  ))
  second = run_conform(project, served, state, '2026-10-02', '--only', 'pets.get_pet', '--only', 'store.get_inventory')

  data = report(state, '2026-10-02')
  withdrawn = {(r['function'], r['example'], r['withdrawn']) for r in data['resolved']}
  assert withdrawn == {
    ('pets.get_pet', 'default', 'not called: the spec declares it private and no credentials are set'),
    ('store.get_inventory', 'default', 'the recording is gone'),
  }, second.output
  assert '(withdrawn: the recording is gone)' in (state / 'petstore' / '2026-10-02.md').read_text()


def test_pinned_names_what_ages_in_a_recorded_request():
  from datetime import date
  from truewire.conform.run import EVERY, pinned
  today = date(2026, 9, 29)
  assert pinned({'symbol': 'BTCUSDT', 'period': '5m', 'startTime': 1785873900000, 'endTime': 1785881100000},
                today) == ['startTime (2026-08-04)', 'endTime (2026-08-04)']
  assert pinned({'symbol': 'BTC-260810-65000-C', 'limit': 10}, today) == ['symbol (2026-08-10)']
  assert pinned({'symbol': 'BTCUSDT', 'fromId': 3405122587, 'limit': 5}, today) == ['fromId (an id into history)']
  assert pinned({'since': '2026-09-01T00:00:00Z'}, today) == ['since (2026-09-01)']
  # Nothing that ages: a live symbol, a future expiry, a limit, a time still ahead, a flag.
  assert pinned({'symbol': 'BTCUSDT', 'limit': 500, 'recvWindow': 5000, 'reduceOnly': True}, today) == []
  assert pinned({'symbol': 'BTC-261225-65000-C', 'startTime': 1798761600000}, today) == []
  assert pinned({'createdAt': 1785873900, 'end_ts': '1785873900'}, today) == [
    'createdAt (2026-08-04)', 'end_ts (2026-08-04)']
  # Ids and hashes are not times, even in the epoch range (TRU-323): bitget subUid, deribit
  # announcement_id, a coinbase cursor, a tx hash, an address, a date code before 2000.
  assert pinned({'subUid': 1578009600, 'commentId': 1500000000, 'announcement_id': 1765432100000,
                 'cursor': '1262736000', 'order': 'x_991231_y'}, today) == []
  assert pinned({'txhash': '0x9d0174ca9abcc0bb08a079bcc321a90676de2d3aa47a07d0529dd2a16e921010'}, today) == []
  assert pinned({'address': '0x2578b31f550b9270fbffa790511e20c459820355'}, today) == []
  # A bare `start` is a row offset as often as a time: only a declared format makes it one (TRU-333).
  assert pinned({'start': 1500000000}, today) == []
  assert pinned({'start': 1500000000, 'end': 1759000000123456789}, today, frozenset({('start',), ('end',)})) == [
    'start (2017-07-14)', 'end (2025-09-27)']
  # A declaration holds at its own path only: `filter.start` says nothing of a top-level `start`.
  assert pinned({'start': 1500000000}, today, frozenset({('filter', 'start')})) == []
  assert pinned({'filter': {'start': [1500000000]}}, today, frozenset({('filter', 'start', EVERY)})) == ['start (2017-07-14)']
  # A tuple position's format holds at that position only: a row offset beside a time is not one.
  assert pinned({'bounds': [1900000000, 1500000000]}, today, frozenset({('bounds', 0)})) == []
  assert pinned({'bounds': [1500000000, 7]}, today, frozenset({('bounds', 0)})) == ['bounds (2017-07-14)']


def test_declared_times_reads_the_request_schema():
  from datetime import date
  from truewire.conform.run import EVERY, declared_times, pinned
  spec = {
    'request': {'type': 'object', 'properties': {
      'start': {'type': 'integer', 'format': 'epoch-millis'},
      'offset': {'type': 'integer'},
      'filter': {'type': 'object', 'properties': {'to': {'anyOf': [{'type': 'null'}, {'type': 'integer', 'format': 'epoch-seconds'}]}}},
    }},
    'response': {'type': 'object', 'properties': {'end': {'type': 'integer', 'format': 'epoch-millis'}}},
  }
  assert declared_times(spec) == {('start',), ('filter', 'to')}
  since = {'type': 'array', 'items': {'type': 'string', 'format': 'date-time'}}
  assert declared_times({'parameters': {'properties': {'since': since}}}) == {('since', EVERY)}
  bounds = {'type': 'array', 'prefixItems': [{'type': 'integer', 'format': 'epoch-seconds'}, {'type': 'integer'}]}
  assert declared_times({'request': {'properties': {'bounds': bounds}}}) == {('bounds', 0)}
  # Beside `prefixItems`, `items` covers the positions after them only.
  row = {'type': 'array', 'prefixItems': [{'type': 'integer'}], 'items': {'type': 'integer', 'format': 'epoch-seconds'}}
  times = declared_times({'request': {'properties': {'row': row}}})
  assert pinned({'row': [1500000000, 1900000000]}, date(2026, 9, 29), times) == []
  assert pinned({'row': [1500000000, 1900000000, 1500000000]}, date(2026, 9, 29), times) == ['row (2017-07-14)']


def test_declared_public_reads_every_marker():
  from truewire.conform.run import declared_public
  assert declared_public({'public': True}) and declared_public({'private': False})
  assert declared_public({'signed': False}) and declared_public({'signed': False, 'security': 'NONE'})
  assert declared_public({'signed': False, 'security': 'System'})
  # Binance's key tiers are unsigned but need an API key.
  assert not declared_public({'signed': False, 'security': 'MARKET_DATA'})
  assert not declared_public({'signed': False, 'security': 'USER_STREAM'})
  assert not declared_public({'signed': True}) and not declared_public({}) and not declared_public(None)


def test_an_empty_array_resolves_no_finding_about_its_elements(tmp_path: Path, monkeypatch):
  """Bit2me's NFT/EUR book: rows went from 3 elements to 2. A night the live book is empty
  says nothing about its rows, so the finding keeps its first-seen date."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  retyped = lambda r: [item.update(id=str(item['id'])) for item in r['payload']['items']]  # noqa: E731
  mutate(served, 'pets/list_pets', retyped, example='first_page')
  state = tmp_path / 'state'
  only = ('--only', 'pets.list_pets')
  assert run_conform(project, served, state, '2026-10-01', *only).exit_code == 1
  [finding] = findings(report(state, '2026-10-01'), 'pets.list_pets')

  mutate(served, 'pets/list_pets', lambda r: r['payload'].update(items=[]), example='first_page')
  empty = run_conform(project, served, state, '2026-10-02', *only)
  assert empty.exit_code == 0, empty.output
  assert report(state, '2026-10-02')['resolved'] == []
  ledger = json.loads((state / 'petstore' / 'ledger.json').read_text())['findings']
  assert ledger[finding['fingerprint']]['resolved'] is None

  healed = served_copy(project, tmp_path / 'healed')
  assert run_conform(project, healed, state, '2026-10-03', *only).exit_code == 0
  [resolved] = report(state, '2026-10-03')['resolved']
  assert (resolved['fingerprint'], resolved['first_seen']) == (finding['fingerprint'], '2026-10-01')


def test_reaches_needs_an_element_under_every_star():
  from truewire.conform.report import reaches
  book = {'asks': [[1, 2]], 'bids': []}
  assert reaches(book, '/asks/*') and not reaches(book, '/bids/*')
  assert reaches(book, '/gone')  # the last key need not be there: that resolves key_added
  assert not reaches(book, '/gone/deeper')
  assert reaches({'m': {'BTC': {'x': 1}}}, '/m/*/x') and not reaches({'m': {}}, '/m/*/x')
  assert reaches([[1, 2, 3]], '/*/2') and reaches(None, '')
  # A null or scalar parent says nothing about its keys (TRU-324).
  assert not reaches({'fees': None}, '/fees/maker') and not reaches({'fees': 5}, '/fees/maker')
  assert reaches({'fees': {}}, '/fees/maker') and not reaches({'rows': [1]}, '/rows/x')
  # An empty tuple says nothing about its length or its positions (TRU-335).
  assert reaches({'rows': [[1, 2]]}, '/rows/*', 'array_element')
  assert not reaches({'rows': [[]]}, '/rows/*', 'array_element') and not reaches({'rows': [[]]}, '/rows/*/0')
  assert reaches({'rows': [[], [1]]}, '/rows/*', 'array_element')


def test_a_finding_on_an_endpoint_gone_from_the_spec_is_withdrawn_by_a_full_run(tmp_path: Path, monkeypatch):
  """TRU-325: an endpoint the spec drops (or renames) is never judged again. A run over the
  whole spec withdraws its findings; a run narrowed with `--only` cannot tell, and keeps them."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))
  state = tmp_path / 'state'
  assert run_conform(project, served, state, '2026-10-01').exit_code == 1
  [finding] = findings(report(state, '2026-10-01'), 'pets.get_pet')

  shutil.rmtree(project / 'spec' / 'endpoints' / GET_PET)
  assert run_conform(project, served, state, '2026-10-02', '--only', 'pets.list_pets').exit_code == 0
  assert report(state, '2026-10-02')['resolved'] == []

  assert run_conform(project, served, state, '2026-10-03').exit_code == 0
  [withdrawn] = report(state, '2026-10-03')['resolved']
  assert (withdrawn['fingerprint'], withdrawn['first_seen'], withdrawn['withdrawn']) == (
    finding['fingerprint'], '2026-10-01', 'the endpoint is gone from the spec')


def test_a_finding_on_an_endpoint_no_longer_called_is_withdrawn(tmp_path: Path, monkeypatch):
  """A read named by `--allow` one night and not the next is `not_a_read`: never judged again."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, 'pets/create_pet', lambda r: r['payload'].update(note='x'))
  state = tmp_path / 'state'
  only = ('--only', 'pets.create_pet')
  assert run_conform(project, served, state, '2026-10-01', *only, '--allow', 'pets.create_pet').exit_code == 1

  assert run_conform(project, served, state, '2026-10-02', *only).exit_code == 0
  [withdrawn] = report(state, '2026-10-02')['resolved']
  assert withdrawn['withdrawn'].startswith('not called: not_a_read'), withdrawn


def test_a_public_endpoint_the_core_refuses_keeps_its_findings(tmp_path: Path, monkeypatch):
  """TRU-334: the spec still marks the endpoint public, so a refusal before sending is the
  core's doing. Nothing is withdrawn, and the finding keeps its first-seen date."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, 'store/get_inventory', lambda r: r['payload'].update(sold='7'))
  state = tmp_path / 'state'
  only = ('--only', 'store.get_inventory')
  assert run_conform(project, served, state, '2026-10-01', *only).exit_code == 1
  [finding] = findings(report(state, '2026-10-01'), 'store.get_inventory')
  spec = json.loads((project / 'spec' / 'endpoints' / 'store' / 'get_inventory' / 'endpoint.json').read_text())
  assert spec['meta']['public'] is True

  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('[secrets]\nrequired = []\n', '[secrets]\nrequired = ["PETSTORE_API_KEY"]\n'))
  monkeypatch.delenv('PETSTORE_API_KEY', raising=False)
  core = project / 'src' / 'petstore' / 'core' / '__init__.py'
  original = core.read_text()
  patch_core(project, before=(
    '    from truewire_core.exceptions import AuthError\n'
    "    raise AuthError('a core bug')\n"
  ))
  refused = run_conform(project, served, state, '2026-10-02', *only)
  data = report(state, '2026-10-02')
  assert endpoint(data, 'store.get_inventory')['reason'] == 'missing_credentials', refused.output
  assert data['resolved'] == []

  core.write_text(original)
  assert run_conform(project, served, state, '2026-10-03', *only).exit_code == 1
  [again] = findings(report(state, '2026-10-03'), 'store.get_inventory')
  assert (again['fingerprint'], again['first_seen']) == (finding['fingerprint'], '2026-10-01')
