"""`truewire capture`: one live call, recorded as an example pair through the real client.

The "live API" here is the project's own mock server serving the examples it already
has, so the capture is checked against a known answer: the recorded pair must equal what
the mock served, and `truewire check` must accept it.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app
from truewire.mock import running_mock_servers

FIXTURES = Path(__file__).parent / 'fixtures' / 'openapi'


def quickstart_project(tmp_path: Path, monkeypatch) -> Path:
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'petstore', '--base-url', 'https://petstore.example/v1']).exit_code == 0
  project = tmp_path / 'petstore'
  imported = runner.invoke(app, ['import', 'openapi', str(FIXTURES / 'petstore.yaml'), '--project', str(project)])
  assert imported.exit_code == 0, imported.output
  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  return project


def test_capture_records_the_wire_pair_and_checks_it(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  served = json.loads((examples / 'default.response.json').read_text())

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--description', 'Recorded through the mock', '--new', f'base_url={servers.http_base_url}',
      '--project', str(project),
    ])
  assert result.exit_code == 0, result.output
  assert 'pets.get_pet[captured]: HTTP 200' in result.output

  recorded_request = json.loads((examples / 'captured.request.json').read_text())
  recorded_response = json.loads((examples / 'captured.response.json').read_text())
  assert recorded_request == {'description': 'Recorded through the mock', 'request': {'petId': 42}}
  assert recorded_response == {'status': served['status'], 'payload': served['payload']}
  assert 'Result: OK' in result.output


def test_capture_scrubs_named_keys(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'scrubbed',
      '--scrub', 'name', '--new', f'base_url={servers.http_base_url}', '--project', str(project),
      '--no-check',
    ])
  assert result.exit_code == 0, result.output
  recorded = json.loads((examples / 'scrubbed.response.json').read_text())
  assert recorded['payload']['name'] == 'REDACTED_NAME'


def test_capture_refuses_an_error_response(tmp_path: Path, monkeypatch):
  """A request the mock has no example for answers 422; nothing is written."""
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 999}', '--id', 'missing',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])
  assert result.exit_code == 1
  assert 'HTTP 422' in result.output
  assert not (examples / 'missing.response.json').exists()


def test_capture_rejects_an_unknown_function(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  result = CliRunner().invoke(app, ['capture', 'pets.no_such', '--project', str(project)])
  assert result.exit_code == 1
  assert 'no endpoint with function' in result.output


def test_capture_drops_the_stale_unverified_declaration(tmp_path: Path, monkeypatch):
  """The pair `capture` writes is the evidence `unverified` said was missing, so the block
  goes with it; left behind it failed `truewire examples` unconditionally. Every other
  key keeps its place."""
  project = quickstart_project(tmp_path, monkeypatch)
  endpoint_file = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'endpoint.json'
  items = list(json.loads(endpoint_file.read_text()).items())
  keys = [key for key, _ in items]
  items.insert(1, ('unverified', {'reason': 'not_captured', 'detail': 'imported'}))
  endpoint_file.write_text(json.dumps(dict(items), indent=2) + '\n')

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])
  assert result.exit_code == 0, result.output
  assert 'removed the stale `unverified` declaration from spec/endpoints/pets/get_pet/endpoint.json' in result.output
  assert 'Result: OK' in result.output
  rewritten = json.loads(endpoint_file.read_text())
  assert 'unverified' not in rewritten
  assert list(rewritten) == keys
  assert endpoint_file.read_text().startswith('{\n  "')
  assert endpoint_file.read_text().endswith('}\n')

  coverage = CliRunner().invoke(app, ['examples', '--project', str(project)])
  assert coverage.exit_code == 0, coverage.output


def test_capture_leaves_an_endpoint_without_unverified_alone(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  endpoint_file = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'endpoint.json'
  before = endpoint_file.read_bytes()

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project), '--no-check',
    ])
  assert result.exit_code == 0, result.output
  assert 'unverified' not in result.output
  assert endpoint_file.read_bytes() == before
