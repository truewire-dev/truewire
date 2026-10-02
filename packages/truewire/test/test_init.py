"""`truewire init` writes a project whose first `generate` needs nothing seeded by hand.

Before ADR 0011 the generator imported the target package to introspect `.new()`, so `init`
wrote a placeholder `main.py` for the package's `__init__.py` to import on that first run.
Now the plan is read from `truewire.toml` alone: `init` writes no placeholder, the first
`generate` runs with the package refused from `sys.path`, and the generated package then
imports with its `Meta` coming from the module `init` and `generate` both write.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner
from truewire_core import types as runtime_types

from truewire.cli import app
from truewire.project import Policy, Secrets, load_project
from truewire.skeleton import GITIGNORE_LINES, SCHEMA_URL, ensure_gitignore, narrow_state_lines

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
    types = importlib.import_module('demo.core.types')
    for unit in ('Seconds', 'Millis', 'Micros', 'Nanos'):
      for name in (f'Timestamp{unit}Float', f'timestamp_{unit.lower()}_float'):
        assert getattr(types, name) is getattr(runtime_types, name)
  finally:
    sys.path.remove(str(project / 'src'))
    for name in [n for n in list(sys.modules) if n == 'demo' or n.startswith('demo.')]:
      del sys.modules[name]


@pytest.mark.parametrize('language', ['python', 'typescript', 'rust', 'go'])
def test_generate_on_a_fresh_init_refuses_the_empty_endpoint_tree(tmp_path: Path, language: str):
  # Same rule as `check`: a command must not report success having examined nothing. Python
  # used to exit 0 without writing `main.py`, so the package `init` wrote did not import.
  runner = CliRunner()
  project = tmp_path / 'demo'
  assert runner.invoke(app, ['init', 'demo', '--dir', str(project)]).exit_code == 0
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text() + '\n[typescript]\nsrc = "ts"\n[rust]\nsrc = "rs"\n[go]\nsrc = "go"\nmodule = "example.com/demo"\n')
  before = _project_files(project)

  result = runner.invoke(app, ['generate', language, '--project', str(project)])

  assert result.exit_code == 1, result.output
  assert 'demo: no endpoint specs found under spec/endpoints, so nothing was generated.' in result.output
  assert f'`truewire generate {language} --delete`' in result.output
  assert 'Generated' not in result.output
  assert _project_files(project) == before

  checked = runner.invoke(app, ['generate', language, '--check', '--project', str(project)])

  assert checked.exit_code == 1, checked.output
  assert 'demo: no endpoint specs found under spec/endpoints, so nothing was checked.' in checked.output
  assert _project_files(project) == before


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
  assert (cwd / 'src' / 'open_meteo' / '__init__.py').read_text() == (
    "from .main import OpenMeteo\n\n__all__ = ['OpenMeteo']\n"
  )
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
  assert (cwd / '.gitignore').read_text() == '*.log\n.venv/\n.env\n.truewire/cache/\n__pycache__/\n'


@pytest.mark.parametrize('broad', ['.truewire/', '.truewire', '/.truewire/', '  .truewire/  '])
def test_init_narrows_a_broad_truewire_ignore_and_says_so(tmp_path: Path, monkeypatch, broad: str):
  """W16: a `.truewire/` line an older `init` wrote would keep the committed codegen manifest
  out of git; `init` rewrites it to `.truewire/cache/` in place and leaves the rest alone."""
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  (cwd / '.gitignore').write_text(f'# local\n*.log\n{broad}\n.truewire-notes\n.env\n')
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert '.gitignore (`.truewire/` narrowed to `.truewire/cache/`)' in result.output
  assert (cwd / '.gitignore').read_text() == (
    '# local\n*.log\n.truewire/cache/\n.truewire-notes\n.env\n.venv/\n__pycache__/\n'
  )
  assert 'Nothing changed' in CliRunner().invoke(app, ['init', '.']).output


def test_init_drops_a_broad_truewire_ignore_beside_the_cache_line(tmp_path: Path, monkeypatch):
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  (cwd / '.gitignore').write_text('.truewire/cache/\n.truewire/\n*.log')
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', '.'])
  assert '.gitignore (`.truewire/` narrowed to `.truewire/cache/`)' in result.output
  assert (cwd / '.gitignore').read_text() == '.truewire/cache/\n*.log\n.env\n.venv/\n__pycache__/\n'


@pytest.mark.parametrize('cache', ['.truewire/cache', '/.truewire/cache/', '/.truewire/cache'])
def test_init_drops_a_broad_truewire_ignore_beside_any_cache_line(tmp_path: Path, monkeypatch, cache: str):
  """Any spelling of the cache line counts: `.truewire/` beside it is dropped, not rewritten
  into a second cache line, and `init` adds no `.truewire/cache/` of its own."""
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  (cwd / '.gitignore').write_text(f'.truewire/\n{cache}\n*.log\n')
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', '.'])
  assert '.gitignore (`.truewire/` narrowed to `.truewire/cache/`)' in result.output
  assert (cwd / '.gitignore').read_text() == f'{cache}\n*.log\n.env\n.venv/\n__pycache__/\n'


@pytest.mark.parametrize(('before', 'after'), [
  ('*.log\r\n.truewire/\r\n', '*.log\r\n.truewire/cache/\r\n.env\r\n.venv/\r\n__pycache__/\r\n'),
  ('*.log\r\n.venv/', '*.log\r\n.venv/\r\n.env\r\n.truewire/cache/\r\n__pycache__/\r\n'),
])
def test_init_keeps_a_crlf_gitignore_crlf(tmp_path: Path, monkeypatch, before: str, after: str):
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  (cwd / '.gitignore').write_bytes(before.encode())
  monkeypatch.chdir(cwd)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  assert (cwd / '.gitignore').read_bytes() == after.encode()
  assert 'Nothing changed' in CliRunner().invoke(app, ['init', '.']).output


@pytest.mark.parametrize('line', [
  '!.truewire/', '.truewire/*', '.truewire/cache/', '# .truewire/', '.truewire/\\ ',
  '**/.truewire/', '.truewire-notes',
])
def test_narrowing_leaves_every_other_form_alone(line: str):
  text = f'*.log\n{line}\n.env'
  assert narrow_state_lines(text) == text


def test_init_leaves_the_codegen_manifest_committable(tmp_path: Path, monkeypatch):
  """What git itself makes of the narrowed `.gitignore`: the manifest is not ignored, the
  cache is."""
  git = shutil.which('git')
  if git is None:
    pytest.skip('git is not installed')
  cwd = tmp_path / 'demo'
  cwd.mkdir()
  subprocess.run([git, 'init', '-q'], cwd=cwd, check=True)
  (cwd / '.gitignore').write_text('.truewire/\n')
  monkeypatch.chdir(cwd)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  ignored = lambda path: subprocess.run([git, 'check-ignore', '-q', path], cwd=cwd).returncode
  assert ignored('.truewire/codegen/python.json') == 1
  assert ignored('.truewire/cache/x') == 0


def test_init_dot_refuses_a_directory_name_that_is_no_package_name(tmp_path: Path, monkeypatch):
  cwd = tmp_path / '2024-client'
  cwd.mkdir()
  monkeypatch.chdir(cwd)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 1
  assert "'2024-client' does not give a package name" in result.output


SKELETON = [
  '.agents/rules/.gitkeep',
  '.agents/skills/README.md',
  '.agents/skills/core/SKILL.md',
  '.agents/skills/discover/SKILL.md',
  '.agents/skills/docs/SKILL.md',
  '.agents/skills/implement/SKILL.md',
  '.agents/skills/review/SKILL.md',
  '.agents/skills/spec/SKILL.md',
  '.gitignore',
  '.truewire/codegen/.gitkeep',
  'AGENTS.md',
  'CLAUDE.md',
  'dev/capture/.gitkeep',
  'docs/api-keys.md',
  'docs/docs.yml',
  'docs/how-to/index.md',
  'docs/index.md',
  'docs/reference/index.md',
  'packages/.gitkeep',
  'spec/endpoints/router.json',
  'truewire.toml',
]
"""Every file of the workspace skeleton `init` writes beside the Python package scaffold."""


def test_init_writes_the_workspace_skeleton(tmp_path: Path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  result = CliRunner().invoke(app, ['init', 'demo'])
  assert result.exit_code == 0, result.output
  project = tmp_path / 'demo'
  assert set(SKELETON) <= _project_files(project)
  assert (project / 'CLAUDE.md').read_text() == '@AGENTS.md\n'
  assert (project / '.gitignore').read_text().splitlines() == ['.env', '.truewire/cache/', '.venv/', '__pycache__/']
  assert (project / 'docs' / 'docs.yml').read_text().startswith('$schema: https://truewire.dev/schemas/docs.yml.json\n')
  toml = (project / 'truewire.toml').read_text()
  assert toml.splitlines()[0] == '#:schema https://truewire.dev/schemas/truewire.toml.json'
  data = tomllib.loads(toml)
  assert list(data)[:4] == ['project', 'spec', 'secrets', 'policy']
  assert data['secrets'] == {'required': []}
  loaded = load_project(project)
  assert loaded.secrets == Secrets()
  assert loaded.policy == Policy()


def test_init_links_claude_skills_and_rules_into_agents(tmp_path: Path, monkeypatch):
  """A9: the links are relative, so they survive a clone to another path."""
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  for name in ('skills', 'rules'):
    link = project / '.claude' / name
    assert link.is_symlink()
    assert os.readlink(link) == f'../.agents/{name}'
    assert link.resolve() == (project / '.agents' / name).resolve()


def test_init_leaves_a_claude_directory_that_is_already_there(tmp_path: Path, monkeypatch):
  root = tmp_path / 'demo'
  (root / '.claude' / 'skills').mkdir(parents=True)
  (root / '.claude' / 'skills' / 'mine.md').write_text('mine\n')
  (root / 'truewire.toml').write_text('[project]\nname = "demo"\n')
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert not (root / '.claude' / 'skills').is_symlink()
  assert (root / '.claude' / 'skills' / 'mine.md').read_text() == 'mine\n'
  assert (root / '.claude' / 'rules').is_symlink()


def test_init_again_changes_nothing_and_says_so(tmp_path: Path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  before = _tree(project)
  result = CliRunner().invoke(app, ['init', 'demo'])
  assert result.exit_code == 0, result.output
  assert 'Nothing changed' in result.output
  assert _tree(project) == before
  assert os.readlink(project / '.claude' / 'skills') == '../.agents/skills'


def test_init_again_from_inside_the_project_changes_nothing(tmp_path: Path, monkeypatch):
  """Inside `demo/`, `truewire init demo` is the re-run over that project, not a second
  project nested in `demo/demo/`."""
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  before = _tree(project)
  monkeypatch.chdir(project)
  result = CliRunner().invoke(app, ['init', 'demo'])
  assert result.exit_code == 0, result.output
  assert 'Nothing changed' in result.output
  assert not (project / 'demo').exists()
  assert _tree(project) == before


def test_init_inside_a_project_named_in_its_toml_fills_it_in(tmp_path: Path, monkeypatch):
  """A checkout whose directory is not the package name still counts when its
  `truewire.toml` names the project."""
  project = tmp_path / 'petstore-client'
  project.mkdir()
  (project / 'truewire.toml').write_text('[project]\nname = "petstore"\n')
  monkeypatch.chdir(project)
  result = CliRunner().invoke(app, ['init', 'petstore'])
  assert result.exit_code == 0, result.output
  assert (project / 'AGENTS.md').is_file()
  assert not (project / 'petstore').exists()


def test_init_inside_another_project_still_creates_a_subdirectory(tmp_path: Path, monkeypatch):
  project = tmp_path / 'demo'
  project.mkdir()
  (project / 'truewire.toml').write_text('[project]\nname = "demo"\n')
  monkeypatch.chdir(project)
  result = CliRunner().invoke(app, ['init', 'other'])
  assert result.exit_code == 0, result.output
  assert (project / 'other' / 'truewire.toml').is_file()


def test_init_over_a_project_fills_in_the_skeleton_and_overwrites_nothing(tmp_path: Path, monkeypatch):
  """A project from an older `init`: its own files stay byte for byte, the missing
  skeleton is written, and the package scaffold is not touched even where it is absent."""
  project = tmp_path / 'demo'
  project.mkdir()
  (project / 'truewire.toml').write_text('[project]\nname = "demo"\n')
  (project / 'AGENTS.md').write_text('# Ours\n')
  (project / '.gitignore').write_text('.truewire/\n')
  monkeypatch.chdir(tmp_path)
  result = CliRunner().invoke(app, ['init', 'demo'])
  assert result.exit_code == 0, result.output
  assert 'Filled in' in result.output and 'left as they are' in result.output
  assert (project / 'truewire.toml').read_text() == f'#:schema {SCHEMA_URL}\n[project]\nname = "demo"\n'
  assert (project / 'AGENTS.md').read_text() == '# Ours\n'
  assert (project / '.gitignore').read_text() == '.truewire/cache/\n.env\n.venv/\n__pycache__/\n'
  assert (project / 'CLAUDE.md').read_text() == '@AGENTS.md\n'
  assert (project / 'docs' / 'api-keys.md').is_file()
  assert not (project / 'src').exists()
  assert not (project / 'pyproject.toml').exists()
  assert 'Nothing changed' in CliRunner().invoke(app, ['init', 'demo']).output


def test_init_over_a_toml_without_a_schema_line_prepends_it_and_nothing_else(tmp_path: Path, monkeypatch):
  """W13 for a project from before it: line 1 becomes the schema line, the rest of the
  file is the old one byte for byte, and a second run changes nothing."""
  root = tmp_path / 'demo'
  root.mkdir()
  old = b'[project]\nname = "demo"  # ours\n\n[python]\npackage = "demo"\nname = "Demo"\n'
  (root / 'truewire.toml').write_bytes(old)
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  first, rest = (root / 'truewire.toml').read_bytes().split(b'\n', 1)
  assert first.decode() == f'#:schema {SCHEMA_URL}'
  assert rest == old
  filled = next(line for line in result.output.splitlines() if line.startswith('Filled in'))
  assert f'truewire.toml (line 1 only: `#:schema {SCHEMA_URL}`; every other byte as it was)' in filled
  assert 'but for that first line' in result.output
  again = CliRunner().invoke(app, ['init', '.'])
  assert again.exit_code == 0, again.output
  assert 'Nothing changed' in again.output
  assert (root / 'truewire.toml').read_bytes() == f'#:schema {SCHEMA_URL}\n'.encode() + old


def test_init_adds_the_schema_line_in_the_files_own_line_ending(tmp_path: Path, monkeypatch):
  root = tmp_path / 'demo'
  root.mkdir()
  (root / 'truewire.toml').write_bytes(b'[project]\r\nname = "demo"\r\n')
  monkeypatch.chdir(root)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  assert (root / 'truewire.toml').read_bytes() == f'#:schema {SCHEMA_URL}\r\n[project]\r\nname = "demo"\r\n'.encode()


@pytest.mark.parametrize('text', [
  '#:schema https://example.com/other.json\n[project]\nname = "demo"\n',
  '# ours\n#:schema https://truewire.dev/schemas/truewire.toml.json\n[project]\nname = "demo"\n',
], ids=['another-schema-on-line-1', 'schema-on-a-later-line'])
def test_init_leaves_a_toml_that_names_a_schema_alone(tmp_path: Path, monkeypatch, text: str):
  """`init` does not rewrite a schema line that is there; `truewire standards` reports a
  wrong one (W13)."""
  root = tmp_path / 'demo'
  root.mkdir()
  (root / 'truewire.toml').write_text(text)
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert (root / 'truewire.toml').read_text() == text
  assert 'truewire.toml (' not in result.output
  assert 'Nothing changed' in CliRunner().invoke(app, ['init', '.']).output


def test_init_does_not_add_the_schema_line_through_a_symlinked_toml(tmp_path: Path, monkeypatch):
  outside = tmp_path / 'outside.toml'
  outside.write_text('[project]\nname = "demo"\n')
  root = tmp_path / 'demo'
  root.mkdir()
  (root / 'truewire.toml').symlink_to(outside)
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert outside.read_text() == '[project]\nname = "demo"\n'
  assert (root / 'truewire.toml').is_symlink()


PETSTORE_TOML = '[project]\nname = "petstore"\n\n[python]\npackage = "petstore"\nname = "Petstore"\n'


def test_init_dot_over_a_project_names_the_skeleton_after_truewire_toml(tmp_path: Path, monkeypatch):
  root = tmp_path / 'my-client'
  root.mkdir()
  (root / 'truewire.toml').write_text(PETSTORE_TOML)
  monkeypatch.chdir(root)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  assert (root / 'AGENTS.md').read_text().startswith('# Petstore\n')
  assert 'title: Petstore\n' in (root / 'docs' / 'docs.yml').read_text()


def test_init_dot_over_a_project_titles_it_by_project_name_without_a_python_block(tmp_path: Path, monkeypatch):
  root = tmp_path / 'client'
  root.mkdir()
  (root / 'truewire.toml').write_text('[project]\nname = "open_meteo"\n')
  monkeypatch.chdir(root)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  assert (root / 'AGENTS.md').read_text().startswith('# OpenMeteo\n')


def test_init_dot_over_a_project_needs_no_package_name_from_the_directory(tmp_path: Path, monkeypatch):
  root = tmp_path / '2024-client'
  root.mkdir()
  (root / 'truewire.toml').write_text(PETSTORE_TOML)
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert (root / 'AGENTS.md').is_file()


def test_init_over_a_toml_that_is_not_toml_says_so(tmp_path: Path, monkeypatch):
  root = tmp_path / 'demo'
  root.mkdir()
  (root / 'truewire.toml').write_text('[project\n')
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 1
  assert 'not TOML' in result.output
  assert not (root / 'AGENTS.md').exists()


def test_init_keeps_hand_written_package_files_without_a_toml(tmp_path: Path, monkeypatch):
  """No `truewire.toml`, so the package scaffold is written, around what is already there."""
  root = tmp_path / 'demo'
  (root / 'src' / 'demo' / 'core').mkdir(parents=True)
  (root / 'src' / 'demo' / 'core' / '__init__.py').write_text('# ours\n')
  (root / 'pyproject.toml').write_text('# ours\n')
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'demo']).exit_code == 0
  assert (root / 'src' / 'demo' / 'core' / '__init__.py').read_text() == '# ours\n'
  assert (root / 'pyproject.toml').read_text() == '# ours\n'
  assert (root / 'truewire.toml').is_file()
  assert (root / 'src' / 'demo' / 'core' / 'types.py').is_file()


def test_init_does_not_write_through_a_dangling_symlink(tmp_path: Path, monkeypatch):
  root = tmp_path / 'demo'
  root.mkdir()
  (root / 'truewire.toml').write_text('[project]\nname = "demo"\n')
  (root / 'CLAUDE.md').symlink_to(tmp_path / 'outside.md')
  (root / '.gitignore').symlink_to(tmp_path / 'outside.gitignore')
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert not (tmp_path / 'outside.md').exists()
  assert not (tmp_path / 'outside.gitignore').exists()
  assert (root / 'AGENTS.md').is_file()


def test_init_does_not_write_through_a_live_gitignore_symlink(tmp_path: Path, monkeypatch):
  """A `.gitignore` linked to a file outside the workspace is not `init`'s to change, not
  even to narrow a broad `.truewire/` line."""
  root = tmp_path / 'demo'
  root.mkdir()
  outside = tmp_path / 'outside.gitignore'
  outside.write_text('.truewire/\n*.log\n')
  (root / '.gitignore').symlink_to(outside)
  monkeypatch.chdir(root)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert outside.read_text() == '.truewire/\n*.log\n'
  assert result.output.count('Left symlinked .gitignore alone;') == 1
  assert 'by hand' in result.output
  assert (root / '.gitignore').is_symlink()


def test_init_then_check_refuses_an_unknown_policy_refusal(tmp_path: Path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  _seed_endpoint(project)
  assert CliRunner().invoke(app, ['check', '--project', str(project)]).exit_code == 0

  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('refuse = []', 'refuse = ["pets.get", "nope.nope"]'))
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code == 1, result.output
  assert "[policy].refuse names 'nope.nope', which is no endpoint in the spec" in result.output
  assert "'pets.get'" not in result.output
  assert 'Result: FAILED' in result.output


def _tree(root: Path) -> dict[str, bytes]:
  return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


@pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed')
def test_init_narrows_a_broad_line_behind_a_utf8_bom(tmp_path: Path, monkeypatch):
  """Finding 2: git skips a UTF-8 BOM and still reads `.truewire/` on the first line; `init`
  does not, so it appends a cache line and the manifest stays ignored."""
  subprocess.run([shutil.which('git'), 'init', '-q'], cwd=tmp_path, check=True)
  (tmp_path / '.gitignore').write_bytes(b'\xef\xbb\xbf.truewire/\r\n*.log\r\n')
  manifest = tmp_path / '.truewire' / 'codegen' / 'python.json'
  manifest.parent.mkdir(parents=True)
  manifest.write_text('{}')
  assert subprocess.run([shutil.which('git'), 'check-ignore', '-q', str(manifest)], cwd=tmp_path).returncode == 0
  monkeypatch.chdir(tmp_path)
  result = CliRunner().invoke(app, ['init', '.'])
  assert result.exit_code == 0, result.output
  assert subprocess.run([shutil.which('git'), 'check-ignore', '-q', str(manifest)], cwd=tmp_path).returncode == 1
  assert (tmp_path / '.gitignore').read_bytes().startswith(b'\xef\xbb\xbf.truewire/cache/\r\n')


def test_appended_lines_follow_the_last_line_ending(tmp_path: Path):
  """Finding 3: one stray CRLF line in an LF file turns every appended line CRLF."""
  (tmp_path / '.gitignore').write_bytes(b'a\nb\r\nc\nd\n')
  ensure_gitignore(tmp_path, ['.env'])
  assert (tmp_path / '.gitignore').read_bytes() == b'a\nb\r\nc\nd\n.env\n'


@pytest.mark.parametrize(('before', 'after'), [
  ('\ufeff.truewire/\r\n/.truewire/cache/\r\n', '\ufeff/.truewire/cache/\r\n'),
  ('\ufeff.truewire/cache/\n.truewire/\n', '\ufeff.truewire/cache/\n'),
  ('\ufeff# comment\n.truewire/\n', '\ufeff# comment\n.truewire/cache/\n'),
  ('foo\u2028.truewire/\n', 'foo\u2028.truewire/\n'),
  ('foo\r.truewire/\n', 'foo\r.truewire/\n'),
])
def test_narrowing_preserves_bom_and_recognizes_existing_cache(before: str, after: str):
  assert narrow_state_lines(before) == after
  assert narrow_state_lines(after) == after


@pytest.mark.parametrize(('before', 'after'), [
  (b'a\r\nb\nc', b'a\r\nb\nc\n.env\n'),
  (b'a\nb\r\nc', b'a\nb\r\nc\r\n.env\r\n'),
  (b'a\rb', b'a\rb\n.env\n'),
  (b'a\nb\r', b'a\nb\r\n.env\n'),
  (b'a\r\nb\r', b'a\r\nb\r\n.env\r\n'),
  (b'a', b'a\n.env\n'),
  (b'\xef\xbb\xbf', b'\xef\xbb\xbf.env\n'),
])
def test_appended_lines_use_last_ending_or_lf(tmp_path: Path, before: bytes, after: bytes):
  target = tmp_path / '.gitignore'
  target.write_bytes(before)
  ensure_gitignore(tmp_path, ['.env'])
  assert target.read_bytes() == after
  assert ensure_gitignore(tmp_path, ['.env']) is None


@pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed')
@pytest.mark.parametrize('existing', [b'a\r\nsecrets.json\r', b'build/\r\nnode_modules\r'])
def test_appended_lines_keep_an_unterminated_last_rule(tmp_path: Path, existing: bytes):
  """Git drops one CR before an LF, so an unterminated last line `x\\r` reads as `x`; the
  lines `init` appends must not turn it into `x\\r\\r\\n`, which git reads as `x\\r`."""
  git = shutil.which('git')
  subprocess.run([git, 'init', '-q'], cwd=tmp_path, check=True)
  (tmp_path / '.gitignore').write_bytes(existing)
  rule = existing.rsplit(b'\n', 1)[1].rstrip(b'\r').decode()
  (tmp_path / rule).write_text('x\n')

  def ignored() -> bool:
    return subprocess.run([git, 'check-ignore', '-q', rule], cwd=tmp_path).returncode == 0

  assert ignored()
  ensure_gitignore(tmp_path, GITIGNORE_LINES)
  assert ignored(), (tmp_path / '.gitignore').read_bytes()


def test_gitignore_preserves_bytes_with_a_non_utf8_default(tmp_path: Path, monkeypatch):
  target = tmp_path / '.gitignore'
  target.write_bytes(b'\xef\xbb\xbf.truewire/\r\n# caf\xc3\xa9 \x81\r\n')
  original_open = Path.open

  def cp1252_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
    if 'b' not in mode and encoding is None:
      encoding = 'cp1252'
    return original_open(path, mode, buffering, encoding, errors, newline)

  monkeypatch.setattr(Path, 'open', cp1252_open)
  ensure_gitignore(tmp_path, ['.truewire/cache/', '.env'])
  expected = b'\xef\xbb\xbf.truewire/cache/\r\n# caf\xc3\xa9 \x81\r\n.env\r\n'
  assert target.read_bytes() == expected
  assert ensure_gitignore(tmp_path, ['.truewire/cache/', '.env']) is None


@pytest.mark.parametrize('existing_project', [False, True])
@pytest.mark.parametrize('broad', [False, True])
def test_init_warns_only_for_a_broad_symlinked_gitignore(tmp_path: Path, monkeypatch, existing_project: bool, broad: bool):
  root = tmp_path / 'demo'
  root.mkdir()
  if existing_project:
    (root / 'truewire.toml').write_text('[project]\nname = "demo"\n')
  outside = tmp_path / 'outside.gitignore'
  content = b'\xef\xbb\xbf.truewire/\r\n' if broad else b'*.log\n.truewire/cache/\n'
  outside.write_bytes(content)
  (root / '.gitignore').symlink_to(outside)
  monkeypatch.chdir(root)
  for _ in range(2):
    result = CliRunner().invoke(app, ['init', '.'])
    assert result.exit_code == 0, result.output
    assert result.output.count('Left symlinked .gitignore alone;') == int(broad)
    assert outside.read_bytes() == content
    assert (root / '.gitignore').is_symlink()
