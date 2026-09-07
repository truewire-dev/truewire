"""`truewire.openapi`: an OpenAPI document becomes a spec tree the rest of the toolchain
accepts -- loaded by the real loaders, clean under `truewire check`, and generating a
client that answers through the mock server."""
import asyncio
import json
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.openapi import import_openapi, load_document, validate_tree
from truewire.project import resolve
from truewire.spec import endpoint_records

FIXTURES = Path(__file__).parent / 'fixtures' / 'openapi'


def import_petstore(out: Path):
  """Import the petstore fixture into `out` and return the report."""
  return import_openapi(
    load_document(FIXTURES / 'petstore.yaml'), out=out, core='root', group_core='default',
  )


def test_petstore_imports_every_operation_into_the_tag_layout(tmp_path: Path):
  report = import_petstore(tmp_path)
  assert report.operations == 8
  assert report.written == 8
  assert report.skipped == []
  endpoints = sorted(str(p) for p in report.endpoints)
  assert 'spec/endpoints/pets/get_pet/endpoint.json' in endpoints
  assert 'spec/endpoints/store/place_order/endpoint.json' in endpoints
  assert (tmp_path / 'spec' / 'endpoints' / 'router.json').is_file()
  root = json.loads((tmp_path / 'spec' / 'endpoints' / 'router.json').read_text())
  assert root['core'] == 'root'
  pets = json.loads((tmp_path / 'spec' / 'endpoints' / 'pets' / 'router.json').read_text())
  assert pets['core'] == 'default'
  assert pets['upstream'].startswith('https://')


def test_component_schemas_used_more_than_once_are_shared(tmp_path: Path):
  report = import_petstore(tmp_path)
  shared = json.loads((tmp_path / 'spec' / 'schemas.json').read_text())
  assert 'Pet' in shared and 'Category' in shared
  assert set(report.schemas_shared) == set(shared)


def test_unions_are_anyof_and_error_responses_are_dropped(tmp_path: Path):
  import_petstore(tmp_path)
  text = ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
  assert 'oneOf' not in text
  assert 'allOf' not in text
  place_order = json.loads(
    (tmp_path / 'spec' / 'endpoints' / 'store' / 'place_order' / 'endpoint.json').read_text()
  )
  assert 'anyOf' in json.dumps(place_order['spec']['request'])
  assert '4' not in {k[:1] for k in place_order['spec'].get('responses', {})}


def test_header_parameters_are_dropped_with_a_warning(tmp_path: Path):
  report = import_petstore(tmp_path)
  assert any('X-Request-Id' in w for w in report.warnings)
  list_pets = json.loads(
    (tmp_path / 'spec' / 'endpoints' / 'pets' / 'list_pets' / 'endpoint.json').read_text()
  )
  assert 'X-Request-Id' not in list_pets['spec']['request']['properties']


def test_operations_without_security_are_marked_public(tmp_path: Path):
  import_petstore(tmp_path)
  public = json.loads(
    (tmp_path / 'spec' / 'endpoints' / 'pets' / 'list_pets' / 'endpoint.json').read_text()
  )
  private = json.loads(
    (tmp_path / 'spec' / 'endpoints' / 'pets' / 'create_pet' / 'endpoint.json').read_text()
  )
  assert public['meta'] == {'public': True}
  assert private['meta'] == {}


def test_document_examples_become_recorded_pairs_flagged_as_document_sourced(tmp_path: Path):
  report = import_petstore(tmp_path)
  assert report.examples == 5
  examples = tmp_path / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  request = json.loads((examples / 'default.request.json').read_text())
  response = json.loads((examples / 'default.response.json').read_text())
  assert request['request'] == {'petId': 42}
  assert response['status'] == 200 and response['payload']['id'] == 42
  endpoint = json.loads((examples.parent / 'endpoint.json').read_text())
  assert any('OpenAPI' in note for note in endpoint['notes'])


def test_the_written_tree_loads_and_passes_the_authoring_checks(tmp_path: Path):
  import_petstore(tmp_path)
  assert len(endpoint_records(tmp_path)) == 8
  result = validate_tree(tmp_path)
  assert result.errors == []
  assert result.ok


def test_minimal_document_without_components(tmp_path: Path):
  report = import_openapi(load_document(FIXTURES / 'minimal.json'), out=tmp_path)
  assert report.written == 2
  assert not (tmp_path / 'spec' / 'schemas.json').exists()
  assert validate_tree(tmp_path).errors == []


def test_init_import_check_generate_and_call_through_the_mock(tmp_path: Path, monkeypatch):
  """The README's quickstart, end to end: a fresh project seeded from OpenAPI generates a
  client whose typed calls are answered by the recorded (document) examples."""
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  init = runner.invoke(app, ['init', 'petstore', '--base-url', 'https://petstore.example/v1'])
  assert init.exit_code == 0, init.output
  project = tmp_path / 'petstore'
  imported = runner.invoke(app, ['import', 'openapi', str(FIXTURES / 'petstore.yaml'), '--project', str(project)])
  assert imported.exit_code == 0, imported.output
  checked = runner.invoke(app, ['check', '--project', str(project)])
  assert checked.exit_code == 0, checked.output
  pyproject = tomllib.loads((project / 'pyproject.toml').read_text())
  assert pyproject['project']['name'] == 'petstore'
  assert pyproject['tool']['setuptools']['packages']['find']['where'] == ['src']
  assert any(dep.startswith('truewire-core') for dep in pyproject['project']['dependencies'])
  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  assert (project / 'src' / 'petstore' / 'pets' / 'get_pet.py').is_file()

  from truewire.mock import running_mock_servers

  sys.path.insert(0, str(project / 'src'))
  try:
    for name in [m for m in sys.modules if m == 'petstore' or m.startswith('petstore.')]:
      del sys.modules[name]
    from petstore import Petstore  # type: ignore[import-not-found]

    async def call(base_url: str):
      async with Petstore.new(base_url=base_url) as client:
        pet = await client.pets.get_pet(pet_id=42)
        inventory = await client.store.get_inventory()
        return pet, inventory

    with running_mock_servers(resolve(project)) as servers:
      pet, inventory = asyncio.run(call(servers.http_base_url))
    assert pet['name'] == 'Fido'
    assert pet['created_at'].year == 2025
    assert inventory['available'] == 3
  finally:
    sys.path.remove(str(project / 'src'))
    for name in [m for m in sys.modules if m == 'petstore' or m.startswith('petstore.')]:
      del sys.modules[name]


def test_endpoints_without_a_document_example_are_declared_not_captured(tmp_path: Path):
  """An imported endpoint the document gave no example for says so in `unverified`, so
  `truewire examples --require-verified` passes on an honest import instead of failing on
  a gap nobody has had the chance to close."""
  import_petstore(tmp_path)
  delete_pet = json.loads(
    (tmp_path / 'spec' / 'endpoints' / 'pets' / 'delete_pet' / 'endpoint.json').read_text()
  )
  assert delete_pet['unverified']['reason'] == 'not_captured'
  get_pet = json.loads(
    (tmp_path / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'endpoint.json').read_text()
  )
  assert 'unverified' not in get_pet
  runner = CliRunner()
  result = runner.invoke(app, ['examples', '--require-verified', '--path', str(tmp_path)])
  assert result.exit_code == 0, result.output
  assert '3 endpoint(s) declared `unverified`' in result.output
