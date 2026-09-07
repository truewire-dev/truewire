"""`truewire import registry`: a registry spec becomes a project that generates and replays."""

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app
from truewire.mock import running_mock_servers

FIXTURES = Path(__file__).parent / 'fixtures' / 'openapi'


def build_registry(tmp_path: Path, monkeypatch) -> Path:
  """A local registry holding one spec, `pets`, produced from the petstore document."""
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'source']).exit_code == 0
  source = tmp_path / 'source'
  imported = runner.invoke(app, ['import', 'openapi', str(FIXTURES / 'petstore.yaml'), '--project', str(source)])
  assert imported.exit_code == 0, imported.output
  registry = tmp_path / 'registry'
  entry = registry / 'specs' / 'pets'
  entry.mkdir(parents=True)
  shutil.copytree(source / 'spec', entry / 'spec')
  (entry / 'truewire.toml').write_text(
    '[project]\nname = "pets"\n\n[spec]\ndir = "spec"\n\n'
    '[cores.default]\nmeta = { type = "object", properties = { public = { type = "boolean" } }, additionalProperties = false }\n\n'
    '[cores.extra]\nmeta = { type = "object", properties = { tier = { type = "string" } }, additionalProperties = false }\n'
  )
  (registry / 'registry.json').write_text(json.dumps({'schema': 1, 'specs': {'pets': {'path': 'specs/pets'}}}))
  return registry


def test_registry_spec_imports_generates_and_replays(tmp_path: Path, monkeypatch):
  registry = build_registry(tmp_path, monkeypatch)
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'petstore']).exit_code == 0
  project = tmp_path / 'petstore'

  result = runner.invoke(app, ['import', 'registry', 'pets', '--registry', str(registry), '--project', str(project)])
  assert result.exit_code == 0, result.output
  assert 'Imported pets: 8 endpoints' in result.output
  assert 'added [cores.extra]' in result.output
  assert 'Result: OK' in result.output
  toml = (project / 'truewire.toml').read_text()
  assert toml.count('[cores.default]') == 1
  assert '[cores.extra]' in toml
  assert (project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'endpoint.json').is_file()

  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  with running_mock_servers(project) as servers:
    captured = runner.invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'again',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project), '--no-check',
    ])
  assert captured.exit_code == 0, captured.output


def test_registry_refuses_to_overwrite_without_force(tmp_path: Path, monkeypatch):
  registry = build_registry(tmp_path, monkeypatch)
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'busy']).exit_code == 0
  project = tmp_path / 'busy'
  (project / 'spec' / 'endpoints' / 'x').mkdir(parents=True)
  (project / 'spec' / 'endpoints' / 'x' / 'endpoint.json').write_text('{}')
  result = runner.invoke(app, ['import', 'registry', 'pets', '--registry', str(registry), '--project', str(project)])
  assert result.exit_code == 1
  assert 'already holds endpoints' in result.output
  forced = runner.invoke(app, ['import', 'registry', 'pets', '--registry', str(registry), '--project', str(project), '--force', '--no-check'])
  assert forced.exit_code == 0, forced.output


def test_registry_unknown_name_lists_known(tmp_path: Path, monkeypatch):
  registry = build_registry(tmp_path, monkeypatch)
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'p']).exit_code == 0
  result = runner.invoke(app, ['import', 'registry', 'nope', '--registry', str(registry), '--project', str(tmp_path / 'p')])
  assert result.exit_code == 1
  assert "no spec named 'nope'" in result.output and 'pets' in result.output
