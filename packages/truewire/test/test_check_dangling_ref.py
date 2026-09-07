"""`truewire check` reports a `$ref` that resolves to nothing as a violation, not a crash."""

import json
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app


def test_dangling_ref_is_reported_not_raised(tmp_path: Path, monkeypatch):
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  group = project / 'spec' / 'endpoints' / 'pets'
  (group / 'get' / 'examples').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Pets.', 'upstream': 'https://example.com/docs', 'core': 'default',
  }))
  (group / 'get' / 'endpoint.json').write_text(json.dumps({
    'docs': 'https://example.com/docs/pets/get',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/pets/{id}', 'method': 'GET',
      'description': 'Get a pet.',
      'request': {
        'title': 'GetPetRequest', 'type': 'object', 'required': ['id'],
        'properties': {'id': {'type': 'integer', 'description': 'Pet id.'}},
      },
      'response': {'$ref': 'Pet', 'description': 'The pet.'},
    },
  }))
  (group / 'get' / 'examples' / 'default.request.json').write_text(json.dumps({'request': {'id': 1}}))
  (group / 'get' / 'examples' / 'default.response.json').write_text(json.dumps({'status': 200, 'payload': {'id': 1}}))

  result = runner.invoke(app, ['check', '--project', str(project)])

  assert result.exit_code != 0
  assert 'unresolvable $ref' in result.output, result.output
  assert 'Traceback' not in result.output
