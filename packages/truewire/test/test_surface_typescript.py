"""`truewire surface --language typescript`: every in-scope spec reconciled against what a
TypeScript caller can call -- the generated module's camelCase method, a `[typescript.extras]`
class serving a `surface: handwritten` spec, or a declared `absent`."""
import shutil
from pathlib import Path

import pytest
from truewire.cli import app
from truewire.project import load_project
from truewire.surface import reconcile
from truewire.surface.check import typescript_callables, typescript_parameters
from typer.testing import CliRunner

EXAMPLES = Path(__file__).parents[3] / 'examples'
PETS = Path(__file__).parents[2] / 'testing-ts' / 'test' / 'fixture'


def _example(name: str) -> Path:
  root = EXAMPLES / name
  if not (root / 'truewire.toml').is_file():
    pytest.skip(f'examples/{name} is not checked out beside the package')
  return root


def test_typescript_member_parsing(tmp_path: Path):
  module = tmp_path / 'm.ts'
  module.write_text(
    'export class A {\n'
    '  readonly core: X\n'
    '  getPet(request: Request, options: CallOptions & { validate: false }): Promise<unknown>\n'
    '  async getPet(request: Request, options?: CallOptions): Promise<Pet> {\n'
    '  }\n'
    '  *walk(): Generator<number> {}\n'
    '  listPaged<T>(request = {}, options?: CallOptions): X {}\n'
    '}\n'
    'export function helper(a: number) {}\n'
  )
  assert {'getPet', 'walk', 'listPaged', 'helper', 'core'} <= typescript_callables(module)
  assert typescript_parameters(module, 'getPet') == {'request', 'options'}
  assert typescript_parameters(module, 'missing') is None


def test_kraken_is_fully_reachable_from_typescript():
  """74 generated methods and `retrieve_export`, hand-written and folded in by an extra."""
  project = load_project(_example('kraken'))
  result = reconcile(project, language='typescript')
  assert result.gaps == [] and result.missing_validate == []
  assert result.handwritten == ['spot.account.retrieve_export']
  assert len(result.generated) == 74 and result.total == 75


def test_a_handwritten_spec_with_no_extra_is_a_gap(tmp_path: Path):
  root = tmp_path / 'kraken'
  shutil.copytree(_example('kraken'), root, ignore=shutil.ignore_patterns('node_modules', '.truewire', '*.py', 'test'))
  toml = root / 'truewire.toml'
  toml.write_text(toml.read_text().split('[[typescript.extras."spot.account"]]')[0])
  result = reconcile(load_project(root), language='typescript')
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('spot.account.retrieve_export', 'no_symbol')]
  assert 'no [typescript.extras' in result.gaps[0].detail


def test_a_missing_module_is_a_gap(tmp_path: Path):
  if not (PETS / 'truewire.toml').is_file():
    pytest.skip('packages/testing-ts is not checked out beside the package')
  root = tmp_path / 'pets'
  shutil.copytree(PETS, root, ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  (root / 'src' / 'pets' / 'pets' / 'get_pet.ts').unlink()
  result = reconcile(load_project(root), language='typescript')
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('pets.get_pet', 'no_module')]
  assert sorted(result.generated) == ['pets.adoptions', 'pets.list_pets']


def test_the_cli_reports_typescript_callables():
  result = CliRunner().invoke(app, ['surface', '--project', str(_example('kraken')), '--language', 'typescript'])
  assert result.exit_code == 0, result.output
  assert 'Callables: 74 generated, 1 hand-written, 0 declared absent, over 75 spec(s)' in result.output
