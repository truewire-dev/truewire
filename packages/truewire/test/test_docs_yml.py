"""`truewire docs check` validates `docs/docs.yml` (W10, S9).

The file must match its model, name only pages that exist under `docs/`, and carry one
quickstart block per language `truewire.toml` declares. Each test starts from what `init`
writes, so the fresh project passing is the baseline every failure departs from.
"""
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from truewire.cli import app
from truewire.docs import check_docs_yml

LANGUAGE_TOML = {
  'typescript': '\n[typescript]\npackage = "petstore"\n',
  'rust': '\n[rust]\npackage = "petstore"\n',
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
  """A fresh `truewire init petstore`."""
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'petstore']).exit_code == 0
  return tmp_path / 'petstore'


def docs_check(project: Path, *args: str):
  return CliRunner().invoke(app, ['docs', 'check', '--project', str(project), *args])


def edit(project: Path, change) -> None:
  """Load `docs/docs.yml`, apply `change` to the mapping, and write it back."""
  path = project / 'docs' / 'docs.yml'
  data = yaml.safe_load(path.read_text())
  change(data)
  path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_a_fresh_init_passes(project: Path):
  result = docs_check(project)
  assert result.exit_code == 0, result.output
  assert check_docs_yml(project, ('python',)) == []


def test_init_writes_a_quickstart_block_for_python(project: Path):
  data = yaml.safe_load((project / 'docs' / 'docs.yml').read_text())
  assert list(data['quickstart']) == ['python']
  assert data['quickstart']['python']['code'].startswith('# truewire init: a stub')


def test_an_unknown_key_fails_naming_it(project: Path):
  path = project / 'docs' / 'docs.yml'
  path.write_text(path.read_text() + 'nva:\n  - index.md\n')
  result = docs_check(project)
  assert result.exit_code == 1
  assert 'docs/docs.yml: nva: unknown key' in result.output


def test_a_nav_page_that_is_missing_fails_naming_it(project: Path):
  edit(project, lambda data: data['nav'].append({'title': 'Guides', 'pages': ['guides/orders.md']}))
  result = docs_check(project)
  assert result.exit_code == 1
  assert 'docs/docs.yml: nav: guides/orders.md: no such page under docs/' in result.output


def test_a_nav_section_of_pages_that_exist_passes(project: Path):
  edit(project, lambda data: data['nav'].append({'title': 'Guides', 'pages': ['how-to/index.md']}))
  assert docs_check(project).exit_code == 0


def test_a_nav_page_outside_docs_fails(project: Path):
  (project / 'README.md').write_text('# Petstore\n')
  edit(project, lambda data: data['nav'].append('../README.md'))
  assert check_docs_yml(project, ('python',)) == ['docs/docs.yml: nav: ../README.md: no such page under docs/']


def test_a_nav_entry_that_is_no_page_fails(project: Path):
  edit(project, lambda data: data['nav'].append('index.txt'))
  (problem,) = check_docs_yml(project, ('python',))
  assert problem.startswith("docs/docs.yml: nav[4]: 'index.txt' is not a page")


def test_a_quickstart_missing_a_declared_language_fails(project: Path):
  with (project / 'truewire.toml').open('a') as file:
    file.write(LANGUAGE_TOML['typescript'])
  result = docs_check(project)
  assert result.exit_code == 1
  assert 'docs/docs.yml: quickstart: no block for typescript, which truewire.toml declares' in result.output


def test_a_quickstart_block_for_an_undeclared_language_fails(project: Path):
  edit(project, lambda data: data['quickstart'].update(rust={'code': 'let client = Petstore::new();'}))
  assert check_docs_yml(project, ('python',)) == [
    'docs/docs.yml: quickstart.rust: truewire.toml declares no [rust]',
  ]


def test_a_quickstart_key_that_is_no_language_fails(project: Path):
  edit(project, lambda data: data['quickstart'].update(pyhton={'code': 'x'}))
  assert check_docs_yml(project, ('python',)) == [
    'docs/docs.yml: quickstart.pyhton: not a language; one of python, typescript, rust, go',
  ]


def test_a_missing_title_fails(project: Path):
  edit(project, lambda data: data.pop('title'))
  assert check_docs_yml(project, ('python',)) == ['docs/docs.yml: title: required']


def test_a_file_that_is_not_a_mapping_fails(project: Path):
  (project / 'docs' / 'docs.yml').write_text('- index.md\n')
  assert check_docs_yml(project, ('python',)) == ['docs/docs.yml: must be a mapping of keys, not a list']


def test_an_empty_file_says_what_it_needs(project: Path):
  (project / 'docs' / 'docs.yml').write_text('')
  assert check_docs_yml(project, ('python',)) == ['docs/docs.yml: empty; it needs at least `title` and `nav`']


def test_a_file_that_is_not_yaml_names_the_line(project: Path):
  (project / 'docs' / 'docs.yml').write_text('title: X\nnav: [\n')
  (problem,) = check_docs_yml(project, ('python',))
  assert problem.startswith('docs/docs.yml:3: not YAML: ')
  assert '\n' not in problem


@pytest.mark.parametrize('entry', [3, None, ['index.md']])
def test_a_nav_entry_of_the_wrong_kind_says_what_an_entry_is(project: Path, entry):
  edit(project, lambda data: data['nav'].append(entry))
  assert check_docs_yml(project, ('python',)) == [
    f'docs/docs.yml: nav[4]: a nav entry is a page path under docs/ or a section {{title, pages}}; got {entry!r}',
  ]


def test_an_absolute_page_is_told_to_be_relative(project: Path):
  edit(project, lambda data: data['nav'].append('/etc/passwd.md'))
  (problem,) = check_docs_yml(project, ('python',))
  assert 'relative to docs/' in problem


def test_a_quickstart_block_that_is_a_string_says_what_a_block_is(project: Path):
  edit(project, lambda data: data['quickstart'].update(python='print(1)'))
  assert check_docs_yml(project, ('python',)) == [
    "docs/docs.yml: quickstart.python: must be a quickstart block {code, install}; got 'print(1)'",
  ]


def test_an_empty_nav_section_says_so(project: Path):
  edit(project, lambda data: data['nav'].append({'title': 'Guides', 'pages': []}))
  assert check_docs_yml(project, ('python',)) == ['docs/docs.yml: nav[4].pages: must not be empty']


def test_no_docs_yml_is_the_layout_checks_to_report(project: Path):
  (project / 'docs' / 'docs.yml').unlink()
  assert check_docs_yml(project, ('python',)) == []
  assert docs_check(project).exit_code == 0


def test_a_path_without_truewire_toml_skips_the_language_check(tmp_path: Path):
  (tmp_path / 'docs').mkdir()
  (tmp_path / 'docs' / 'index.md').write_text('# X\n')
  (tmp_path / 'docs' / 'docs.yml').write_text('title: X\nnav: [index.md]\n')
  result = CliRunner().invoke(app, ['docs', 'check', '--path', str(tmp_path)])
  assert result.exit_code == 0, result.output


def test_init_over_a_project_writes_a_block_per_declared_language(tmp_path: Path, monkeypatch):
  root = tmp_path / 'petstore'
  root.mkdir()
  (root / 'truewire.toml').write_text(
    '[project]\nname = "petstore"\n' + LANGUAGE_TOML['typescript'] + LANGUAGE_TOML['rust']
  )
  monkeypatch.chdir(root)
  assert CliRunner().invoke(app, ['init', '.']).exit_code == 0
  data = yaml.safe_load((root / 'docs' / 'docs.yml').read_text())
  assert list(data['quickstart']) == ['typescript', 'rust']
  assert data['quickstart']['rust']['code'].startswith('// truewire init: a stub')
  assert docs_check(root).exit_code == 0
