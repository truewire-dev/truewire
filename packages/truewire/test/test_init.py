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


def _project_files(root: Path) -> set[str]:
  return {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}


def test_init_dot_writes_into_the_current_directory_named_after_it(tmp_path: Path, monkeypatch):
  """Open-Meteo's case: the checkout already exists and is the project; `init .` fills it
  and derives the package name from the directory name."""
  cwd = tmp_path / 'open-meteo'
  cwd.mkdir()
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert f'Created {cwd}' in result.output
  assert (cwd / 'truewire.toml').is_file()
  assert not (cwd / 'open_meteo').exists()
  assert (cwd / 'src' / 'open_meteo' / 'core' / '__init__.py').is_file()
  assert 'name = "open_meteo"' in (cwd / 'truewire.toml').read_text()
  assert 'name = "OpenMeteo"' in (cwd / 'truewire.toml').read_text()
  assert (cwd / 'src' / 'open_meteo' / '__init__.py').read_text() == 'from .main import OpenMeteo\n'
  _seed_endpoint(cwd)
  assert CliRunner().invoke(app, ['check', '--project', str(cwd)]).exit_code == 0


def test_init_name_inside_an_empty_directory_of_that_name_writes_into_it(tmp_path: Path, monkeypatch):
  """`truewire init open_meteo` from an empty `open-meteo/` no longer nests a second
  directory; a `.git` and a `.venv` do not make the directory non-empty."""
  cwd = tmp_path / 'open-meteo'
  (cwd / '.git').mkdir(parents=True)
  (cwd / '.venv').mkdir()
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', 'open_meteo'])
  assert result.exit_code == 0, result.output
  assert (cwd / 'truewire.toml').is_file()
  assert not (cwd / 'open_meteo').exists()


def test_init_name_inside_a_non_empty_directory_of_that_name_creates_a_subdirectory(tmp_path: Path, monkeypatch):
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  (cwd / 'README.md').write_text('not a scaffold target\n')
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', 'demo'])
  assert result.exit_code == 0, result.output
  assert not (cwd / 'truewire.toml').exists()
  assert (cwd / 'demo' / 'truewire.toml').is_file()


def test_init_name_elsewhere_still_creates_a_subdirectory(tmp_path: Path, monkeypatch):
  """The default shape is unchanged: an empty directory of another name gets `./<name>`."""
  monkeypatch.chdir(tmp_path)
  result = CliRunner().invoke(app, ['init', 'demo'])
  assert result.exit_code == 0, result.output
  assert (tmp_path / 'demo' / 'truewire.toml').is_file()
  assert not (tmp_path / 'truewire.toml').exists()
  # The same files, whichever way the target was chosen.
  other = tmp_path / 'elsewhere' / 'demo'
  other.mkdir(parents=True)
  monkeypatch.chdir(other)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  assert _project_files(other) == _project_files(tmp_path / 'demo')


def test_init_dot_keeps_an_existing_gitignore_and_adds_what_it_needs(tmp_path: Path, monkeypatch):
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  (cwd / '.gitignore').write_text('*.log\n.venv/\n')
  monkeypatch.chdir(cwd)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  assert (cwd / '.gitignore').read_text() == '*.log\n.venv/\n.truewire/\n__pycache__/\n'


def test_init_dot_refuses_a_directory_name_that_is_no_package_name(tmp_path: Path, monkeypatch):
  cwd = tmp_path / '2024-client'
  cwd.mkdir()
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 1
  assert "'2024-client' does not give a package name" in result.output


def test_init_dot_refuses_a_directory_that_is_already_a_project(tmp_path: Path, monkeypatch):
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  monkeypatch.chdir(cwd)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 1
  assert 'already exists' in result.output
