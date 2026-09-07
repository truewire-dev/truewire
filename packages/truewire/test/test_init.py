"""`truewire init` writes a project whose first `generate` needs nothing seeded by hand.

Before ADR 0011 the generator imported the target package to introspect `.new()`, so `init`
wrote a placeholder `main.py` for the package's `__init__.py` to import on that first run.
Now the plan is read from `truewire.toml` alone: `init` writes no placeholder, the first
`generate` runs with the package refused from `sys.path`, and the generated package then
imports with its `Meta` coming from the module `init` and `generate` both write.
"""
import importlib
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app

from conftest import forbid_import


def _seed_endpoint(project: Path):
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
      'response': {
        'title': 'Pet', 'type': 'object', 'required': ['id'],
        'properties': {
          'id': {'type': 'integer', 'description': 'Pet id.'},
          'born': {'type': 'string', 'format': 'date', 'description': 'Birth date.'},
        },
        'description': 'The pet.',
      },
    },
  }))


def test_init_then_first_generate_without_the_package_importable(tmp_path: Path, monkeypatch):
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  assert not (project / 'src' / 'demo' / 'main.py').exists()  # no placeholder
  meta_before = (project / 'src' / 'demo' / 'meta.py').read_text()
  assert 'class DefaultMeta(TypedDict):' in meta_before
  assert 'from truewire_core.types import' in (project / 'src' / 'demo' / 'core' / 'types.py').read_text()
  _seed_endpoint(project)

  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
  with forbid_import('demo'):
    result = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert result.exit_code == 0, result.output

  # `generate` rewrites the same `meta.py` `init` wrote, byte for byte.
  assert (project / 'src' / 'demo' / 'meta.py').read_text() == meta_before
  get_module = (project / 'src' / 'demo' / 'pets' / 'get.py').read_text()
  assert 'from truewire_core.types import DateIso' in get_module
  assert "meta={'public': True}" in get_module

  sys.path.insert(0, str(project / 'src'))
  try:
    demo = importlib.import_module('demo')
    assert demo.Demo.new(api_key='k').client.api_key == 'k'
    meta = importlib.import_module('demo.meta')
    assert meta.DefaultMeta.__annotations__.keys() == {'public'}
  finally:
    sys.path.remove(str(project / 'src'))
    for name in [n for n in list(sys.modules) if n == 'demo' or n.startswith('demo.')]:
      del sys.modules[name]
