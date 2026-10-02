"""The published schemas are generated from the models, and the models agree with the files.

T7: every file the toolchain reads with a schema has one at `truewire.dev/schemas/`, and the
pydantic model in `truewire.schemas` is its source. These tests hold the committed JSON to
the models, hold the `truewire.toml` model to the loader's verdict on real files, and check
the JSON itself with `jsonschema`, the way an editor or CI would use it.
"""
import json
import re
import tomllib
from datetime import date
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from truewire.cli import app
from truewire.project import NotAProject, load_project, load_project_data
from truewire.schemas import PUBLISHED, SCHEMA_BASE, published_dir, render
from truewire.schemas.__main__ import main
from truewire.schemas.config import LANGUAGES, TruewireToml
from truewire.schemas.docs import DocsYml
from truewire.score import LANGUAGES as SCORE_LANGUAGES
from truewire.skeleton import DOCS_SCHEMA_URL, SCHEMA_URL

REPO = Path(__file__).resolve().parents[3]
"""The repository root, holding the example projects and the test fixtures."""

FORMATS = jsonschema.FormatChecker()
"""Asserts `format` (a `[score].stranger` date), as an editor does; `jsonschema` only annotates it otherwise."""

REPO_TOMLS = sorted(
  path for path in REPO.glob('**/truewire.toml')
  if not {'node_modules', '.venv', '.paperclip'} & set(path.relative_to(REPO).parts)
)
"""Every `truewire.toml` committed in the repository."""


@pytest.mark.parametrize('name', sorted(PUBLISHED))
def test_the_committed_schema_is_the_models(name: str):
  assert (published_dir() / name).read_text() == render(name), (
    f'{name} is stale; run `python -m truewire.schemas`'
  )


def test_the_generator_check_passes_on_the_committed_files(capsys):
  assert main(['--check']) == 0
  assert capsys.readouterr().out == ''


def test_the_generator_check_names_a_stale_file(tmp_path: Path, monkeypatch, capsys):
  monkeypatch.setattr('truewire.schemas.__main__.published_dir', lambda: tmp_path)
  assert main(['--check']) == 1
  assert sorted(tmp_path.iterdir()) == []
  assert 'stale:' in capsys.readouterr().out
  assert main([]) == 0
  assert sorted(path.name for path in tmp_path.iterdir()) == sorted(PUBLISHED)
  assert main(['--check']) == 0


def test_the_urls_init_writes_are_the_published_ones():
  assert SCHEMA_URL == SCHEMA_BASE + 'truewire.toml.json'
  assert DOCS_SCHEMA_URL == SCHEMA_BASE + 'docs.yml.json'
  for name in PUBLISHED:
    assert json.loads(render(name))['$id'] == SCHEMA_BASE + name


def test_the_languages_are_the_scorecards():
  assert LANGUAGES == SCORE_LANGUAGES


def _json(value):
  """A decoded TOML value as JSON would carry it: dates as ISO strings."""
  if isinstance(value, dict):
    return {key: _json(item) for key, item in value.items()}
  if isinstance(value, list):
    return [_json(item) for item in value]
  return value.isoformat() if isinstance(value, date) else value


def test_the_repository_has_truewire_tomls_to_check():
  assert len(REPO_TOMLS) >= 4, REPO_TOMLS


@pytest.mark.parametrize('path', REPO_TOMLS, ids=lambda path: str(path.relative_to(REPO)))
def test_every_committed_truewire_toml_passes_the_model_the_loader_and_the_schema(path: Path):
  data = tomllib.loads(path.read_text())
  load_project(path)
  TruewireToml.model_validate(data)
  jsonschema.validate(_json(data), json.loads(render('truewire.toml.json')), format_checker=FORMATS)


def test_inits_files_pass_both_published_schemas(tmp_path: Path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'petstore']).exit_code == 0
  project = tmp_path / 'petstore'
  toml = tomllib.loads((project / 'truewire.toml').read_text())
  docs = yaml.safe_load((project / 'docs' / 'docs.yml').read_text())
  jsonschema.validate(_json(toml), json.loads(render('truewire.toml.json')))
  jsonschema.validate(docs, json.loads(render('docs.yml.json')))
  TruewireToml.model_validate(toml)
  DocsYml.model_validate(docs)


INVALID_TOMLS = {
  'unknown section': '[project]\nname = "x"\n[polcy]\nrate = 1\n',
  'unknown project key': '[project]\nname = "x"\nversion = "1"\n',
  'empty name': '[project]\nname = ""\n',
  'secret that is a value': '[project]\nname = "x"\n[secrets]\nrequired = ["sk-live 123"]\n',
  'zero rate': '[project]\nname = "x"\n[policy]\nrate = 0\n',
  'rate that is a bool': '[project]\nname = "x"\n[policy]\nrate = true\n',
  'retry that is a string': '[project]\nname = "x"\n[policy]\nretry = "yes"\n',
  'unknown policy key': '[project]\nname = "x"\n[policy]\nretries = 3\n',
  'stranger not a date': '[project]\nname = "x"\n[score]\nstranger = "soon"\n',
  'python without cores': '[project]\nname = "x"\n[python]\npackage = "x"\n',
  'refuse listed twice': '[project]\nname = "x"\n[policy]\nrefuse = ["a.b", "a.b"]\n',
  'required listed twice': '[project]\nname = "x"\n[secrets]\nrequired = ["A", "A"]\n',
  'unknown spec key': '[project]\nname = "x"\n[spec]\ndirr = "api"\n',
  'spec dir not a string': '[project]\nname = "x"\n[spec]\ndir = 3\n',
  'spec dir empty': '[project]\nname = "x"\n[spec]\ndir = ""\n',
  'spec not a table': 'spec = ""\n[project]\nname = "x"\n',
  'stranger a midnight datetime': '[project]\nname = "x"\n[score]\nstranger = 2026-10-12T00:00:00\n',
}
"""Files the loader refuses; the model and the published schema must refuse each too."""

BEYOND_THE_SCHEMA = {
  'required and optional': '[project]\nname = "x"\n[secrets]\nrequired = ["A"]\noptional = ["A"]\n',
}
"""Files the loader and the model refuse that JSON Schema cannot: it has no way to say two
arrays share no item. An editor accepts these; every command refuses them."""


@pytest.mark.parametrize('text', INVALID_TOMLS.values(), ids=INVALID_TOMLS.keys())
def test_a_file_the_loader_refuses_the_model_and_the_schema_refuse(tmp_path: Path, text: str):
  data = tomllib.loads(text)
  with pytest.raises(NotAProject):
    load_project_data(data, root=tmp_path)
  with pytest.raises(ValidationError):
    TruewireToml.model_validate(data)
  with pytest.raises(jsonschema.ValidationError):
    jsonschema.validate(_json(data), json.loads(render('truewire.toml.json')), format_checker=FORMATS)


@pytest.mark.parametrize('text', BEYOND_THE_SCHEMA.values(), ids=BEYOND_THE_SCHEMA.keys())
def test_a_file_only_the_schema_accepts_is_refused_by_the_loader_and_the_model(tmp_path: Path, text: str):
  data = tomllib.loads(text)
  with pytest.raises(NotAProject):
    load_project_data(data, root=tmp_path)
  with pytest.raises(ValidationError):
    TruewireToml.model_validate(data)
  jsonschema.validate(_json(data), json.loads(render('truewire.toml.json')), format_checker=FORMATS)


def _nodes(schema: dict) -> list[tuple[str, dict]]:
  """The root and every `$defs` entry of a schema, by name."""
  return [('(root)', schema), *schema.get('$defs', {}).items()]


def test_the_toml_schema_has_no_null_branch():
  """TOML has no null. A `null` branch makes taplo report a typo inside a table as matching
  no branch of an `anyOf`, without naming the key."""
  assert 'null' not in render('truewire.toml.json')


@pytest.mark.parametrize('name', sorted(PUBLISHED))
def test_every_property_publishes_a_description(name: str):
  schema = json.loads(render(name))
  missing = [
    f'{owner}.{key}' for owner, node in _nodes(schema)
    for key, prop in node.get('properties', {}).items() if 'description' not in prop
  ]
  missing += [owner for owner, node in _nodes(schema) if 'description' not in node]
  assert missing == []


@pytest.mark.parametrize('name', sorted(PUBLISHED))
def test_no_published_description_names_a_shape_clause_or_an_adr(name: str):
  descriptions = re.findall(r'"description": "((?:[^"\\]|\\.)*)"', render(name))
  assert [text for text in descriptions if re.search(r'\b(ADR \d+|[ASTWP]\d+)\b', text)] == []


def test_the_docs_schema_refuses_an_unknown_key_and_a_language_that_is_none():
  schema = json.loads(render('docs.yml.json'))
  good = {'title': 'X', 'nav': ['index.md', {'title': 'How-to', 'pages': ['how-to/a.md']}]}
  jsonschema.validate(good, schema)
  for bad in (
    {**good, 'nva': []},
    {**good, 'quickstart': {'pyhton': {'code': 'x'}}},
    {**good, 'quickstart': {'python': {'code': 'x', 'lang': 'py'}}},
    {**good, 'nav': ['index.txt']},
    {**good, 'nav': [{'title': 'S', 'pages': []}]},
    {'nav': ['index.md']},
  ):
    with pytest.raises(jsonschema.ValidationError):
      jsonschema.validate(bad, schema)
