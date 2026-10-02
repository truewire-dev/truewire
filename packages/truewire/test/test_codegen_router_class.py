"""
A router group's `router.json` `class` names its class in every backend, in place of the
PascalCase of its directory (`docs/spec/authoring.md` rules 14 and 18).

The case that asked for it: a `chain/rpc` group derives `Rpc`, which in Python is also a
core transport primitive. The override lives in the spec, not in one backend's config, so
all four backends name the class the same -- and only the type name moves: the parent
still reaches the group as `rpc`.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.plan.build import build_plan
from truewire.spec.authoring import check_router_docs, check_router_names
from truewire.spec.router import RouterDoc

SECTIONS = (
  '\n[typescript]\npackage = "aster"\nsrc = "ts"\nname = "Aster"\n'
  '\n[rust]\npackage = "aster"\nsrc = "rs"\nname = "Aster"\n'
  '\n[go]\npackage = "aster"\nsrc = "go"\nname = "Aster"\nmodule = "example.com/aster"\nroot = "go"\n'
)


def endpoint(name: str) -> str:
  """A minimal public GET endpoint whose types are named after `name`."""
  return json.dumps({
    'docs': f'https://example.com/docs/{name}',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': f'/{name}', 'method': 'GET',
      'description': f'Get the {name}.',
      'request': {
        'title': f'{name.title()}Request', 'type': 'object', 'description': 'Which one.',
        'required': ['id'],
        'properties': {'id': {'type': 'string', 'description': 'Its id.'}},
      },
      'response': {
        'title': f'{name.title()}Response', 'type': 'object', 'description': 'The answer.',
        'required': ['id'],
        'properties': {'id': {'type': 'string', 'description': 'Its id.'}},
      },
    },
  })


def router(description: str, **extra: str) -> str:
  return json.dumps({
    'description': description, 'upstream': 'https://example.com/docs', 'core': 'default',
    **extra,
  })


def aster(root: Path, *, rpc_class: str | None = 'ChainRpc') -> Path:
  """A client with `chain/rpc/status` and `chain/blocks/latest`; `chain/rpc` declares
  `class` unless `rpc_class` is `None`. Returns the project directory."""
  assert CliRunner().invoke(app, ['init', 'aster', '--dir', str(root / 'aster')]).exit_code == 0
  project = root / 'aster'
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text() + SECTIONS)
  endpoints = project / 'spec' / 'endpoints'
  (endpoints / 'chain').mkdir()
  (endpoints / 'chain' / 'router.json').write_text(router('The chain.'))
  for group, leaf in (('rpc', 'status'), ('blocks', 'latest')):
    (endpoints / 'chain' / group / leaf).mkdir(parents=True)
    extra = {'class': rpc_class} if group == 'rpc' and rpc_class is not None else {}
    (endpoints / 'chain' / group / 'router.json').write_text(router(f'The {group}.', **extra))
    (endpoints / 'chain' / group / leaf / 'endpoint.json').write_text(endpoint(leaf))
  return project


def generate(project: Path, language: str) -> None:
  result = CliRunner().invoke(app, ['generate', language, '--project', str(project)])
  assert result.exit_code == 0, result.output


def check(project: Path) -> str:
  return CliRunner().invoke(app, ['check', '--project', str(project)]).output


# -- the field -------------------------------------------------------------------------

@pytest.mark.parametrize('value', ['ChainRpc', 'Rpc2', 'X'])
def test_a_pascal_case_class_loads(value: str):
  doc = RouterDoc.model_validate({'description': 'd', 'upstream': 'https://x.test', 'class': value})
  assert doc.class_ == value


@pytest.mark.parametrize('value', ['chainRpc', 'chain_rpc', 'Chain-Rpc', 'Chain Rpc', '', '2Rpc', 'Ärger', 'Self', 'None'])
def test_anything_else_is_refused(value: str):
  with pytest.raises(ValueError):
    RouterDoc.model_validate({'description': 'd', 'upstream': 'https://x.test', 'class': value})


def test_no_class_keeps_the_derived_name():
  assert RouterDoc.model_validate({'description': 'd', 'upstream': 'https://x.test'}).class_ is None


# -- the plan --------------------------------------------------------------------------

def test_the_plan_names_the_group_after_its_class(tmp_path: Path):
  """The accessor keeps the directory's name; only the class moves."""
  plan = build_plan(aster(tmp_path))
  chain = next(r for r in plan.routers if r.path == ['chain'])
  children = {child.name: child.class_ for child in chain.children}
  assert children == {'blocks': 'Blocks', 'rpc': 'ChainRpc'}
  dumped = plan.model_dump(by_alias=True, mode='json')
  chain_json = next(r for r in dumped['routers'] if r['path'] == ['chain'])
  assert {'name': 'rpc', 'kind': 'router', 'class': 'ChainRpc'} in chain_json['children']


# -- every backend ---------------------------------------------------------------------

def test_python_declares_and_composes_the_class(tmp_path: Path):
  project = aster(tmp_path)
  generate(project, 'python')
  package = project / 'src' / 'aster'
  group = (package / 'chain' / 'rpc' / '__init__.py').read_text()
  parent = (package / 'chain' / '__init__.py').read_text()
  assert 'class ChainRpc(' in group
  assert 'class Rpc(' not in group
  assert 'from .rpc import ChainRpc' in parent
  assert 'def rpc(self) -> ChainRpc:' in parent


def test_typescript_declares_and_composes_the_class(tmp_path: Path):
  project = aster(tmp_path)
  generate(project, 'typescript')
  package = project / 'ts' / 'aster'
  group = (package / 'chain' / 'rpc' / 'index.ts').read_text()
  parent = (package / 'chain' / 'index.ts').read_text()
  assert 'export class ChainRpc ' in group
  assert 'class Rpc ' not in group
  assert "import { ChainRpc } from './rpc/index.js'" in parent
  assert 'readonly rpc: ChainRpc' in parent


def test_rust_declares_and_composes_the_struct(tmp_path: Path):
  project = aster(tmp_path)
  generate(project, 'rust')
  package = project / 'rs' / 'aster'
  group = (package / 'chain' / 'rpc' / 'mod.rs').read_text()
  parent = (package / 'chain' / 'mod.rs').read_text()
  assert 'pub struct ChainRpc ' in group
  assert 'struct Rpc ' not in group
  assert 'pub rpc: rpc::ChainRpc,' in parent


def test_go_declares_and_composes_the_type(tmp_path: Path):
  project = aster(tmp_path)
  generate(project, 'go')
  package = project / 'go' / 'aster'
  group = (package / 'chain' / 'rpc' / 'rpc.go').read_text()
  parent = (package / 'chain' / 'chain.go').read_text()
  assert 'type ChainRPC struct' in group
  assert 'type ChainRpc struct' not in group
  assert 'func New(core truewire.HttpEndpoint) *ChainRPC {' in group
  assert 'RPC *rpc.ChainRPC' in parent


@pytest.mark.parametrize('rpc_class', [None, 'Rpc'])
def test_go_recases_a_declared_class_like_a_derived_one(tmp_path: Path, rpc_class: str | None):
  """Go upper-cases initialisms in every name (`Rpc` renders `RPC`), a declared `class`
  included: declaring the name a directory derives renders the same type as leaving it out."""
  project = aster(tmp_path, rpc_class=rpc_class)
  generate(project, 'go')
  group = (project / 'go' / 'aster' / 'chain' / 'rpc' / 'rpc.go').read_text()
  assert 'type RPC struct' in group
  assert 'type Rpc struct' not in group


# -- rule 18 still judges the name the group renders ------------------------------------

def test_the_override_is_clean(tmp_path: Path):
  project = aster(tmp_path)
  assert check_router_names(project) == []


@pytest.mark.parametrize('name,claimed', [
  ('Blocks', "'blocks' and 'rpc' under chain both render the class 'Blocks'"),
  ('Chain', "the router group 'chain' already renders it"),
])
def test_a_class_colliding_with_a_sibling_or_its_parent_is_refused(
  tmp_path: Path, name: str, claimed: str,
):
  project = aster(tmp_path, rpc_class=name)
  messages = [v['message'] for v in check_router_names(project)]
  assert any(claimed in message for message in messages), messages
  assert any('`class`' in message for message in messages), messages


def test_a_class_colliding_with_a_shared_schema_is_refused(tmp_path: Path):
  project = aster(tmp_path)
  (project / 'spec' / 'schemas.json').write_text(json.dumps({
    'ChainRpc': {
      'title': 'ChainRpc', 'type': 'object', 'description': 'A node.',
      'properties': {'id': {'type': 'string', 'description': 'Its id.'}},
    },
  }))
  output = check(project)
  assert "renders the class 'ChainRpc', and a shared schema" in output, output


def test_a_class_on_the_root_router_is_refused(tmp_path: Path):
  """The root is named by each backend's `name`; a `class` there would do nothing."""
  project = aster(tmp_path)
  root = project / 'spec' / 'endpoints' / 'router.json'
  root.write_text(json.dumps({**json.loads(root.read_text()), 'class': 'Other'}))
  violations = check_router_docs(project)
  assert [(v['rule'], v['location']) for v in violations] == [
    ('router-doc', 'spec/endpoints/router.json'),
  ]
  assert '[python].name' in violations[0]['message']
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code != 0, result.output
  assert '14. A router grouping' in result.output, result.output


def test_check_refuses_a_class_that_is_not_pascal_case(tmp_path: Path):
  """Reported as a finding against the file, not raised from the first check to load it."""
  project = aster(tmp_path, rpc_class='chain_rpc')
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code != 0, result.output
  assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
  assert (
    "spec/endpoints/chain/rpc/router.json: class: class 'chain_rpc' must be a PascalCase"
    in result.output
  ), result.output
  assert 'Value error' not in result.output
  assert 'not run    meta and rule 18' in result.output


def test_check_names_a_router_json_that_is_not_json(tmp_path: Path):
  project = aster(tmp_path)
  (project / 'spec' / 'endpoints' / 'chain' / 'rpc' / 'router.json').write_text('{description: 1}')
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code != 0, result.output
  assert 'spec/endpoints/chain/rpc/router.json: not valid JSON: Expecting property name' in result.output, result.output
  assert 'not run    meta and rule 18' in result.output


def test_a_class_on_the_root_router_does_not_hide_rule_18(tmp_path: Path):
  """The root `class` is a file that loads: rule 18 still runs, and still reports a group
  declaring its parent's class."""
  project = aster(tmp_path, rpc_class='Chain')
  root = project / 'spec' / 'endpoints' / 'router.json'
  root.write_text(json.dumps({**json.loads(root.read_text()), 'class': 'Other'}))
  output = check(project)
  assert "18. A router group's class name" in output, output
  assert "the router group 'chain' already renders it" in output, output
  assert '14. A router grouping' in output, output
  assert 'not run' not in output, output


@pytest.mark.parametrize('language', ['python', 'typescript', 'rust', 'go'])
def test_generate_refuses_a_class_that_is_not_pascal_case(tmp_path: Path, language: str):
  """Every backend names the file and writes nothing, instead of a traceback."""
  project = aster(tmp_path, rpc_class='chain_rpc')
  result = CliRunner().invoke(app, ['generate', language, '--project', str(project)])
  assert result.exit_code != 0, result.output
  assert 'rule 14); nothing generated' in result.output, result.output
  assert 'spec/endpoints/chain/rpc/router.json: class:' in result.output
  assert not (project / '.truewire' / 'codegen' / f'{language}.json').exists()
  assert not (project / '.truewire' / f'{language}-files.json').exists()
