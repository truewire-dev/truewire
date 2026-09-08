"""
Exercise `truewire.spec.authoring.check_router_names` (`docs/spec/authoring.md` rule 18)
against synthetic fixtures, never `examples/` -- mirrors
`test_spec_authoring_mixed_leaf_router.py`'s own isolation style for its neighbouring
project-root-level checks.

Only the function tree and `truewire.toml`'s declared client names matter here, so
fixtures write the smallest `endpoint.json` that makes a directory resolve as a leaf.
"""
import json
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app
from truewire.project import load_project
from truewire.spec.authoring import check_router_names, client_class_names

ENDPOINT = json.dumps({
  'meta': {},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
    'description': 'Get a thing.',
    'request': {
      'title': 'GetThingRequest', 'type': 'object', 'description': 'Which thing.',
      'required': ['id'],
      'properties': {'id': {'type': 'string', 'description': 'Thing id.'}},
    },
    'response': {
      'title': 'Thing', 'type': 'object', 'description': 'A thing.', 'required': ['id'],
      'properties': {'id': {'type': 'string', 'description': 'Thing id.'}},
    },
  },
})

ROUTER = json.dumps({
  'description': 'A group.', 'upstream': 'https://example.com/docs', 'core': 'default',
})


def write_leaf(root: Path, *segments: str) -> None:
  """Write a minimal `endpoint.json` at `spec/endpoints/<segments>`, with a `router.json`
  in every grouping directory above it."""
  directory = root / 'spec' / 'endpoints'
  for segment in segments[:-1]:
    directory = directory / segment
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'router.json').write_text(ROUTER)
  leaf = directory / segments[-1]
  leaf.mkdir(parents=True, exist_ok=True)
  (leaf / 'endpoint.json').write_text(ENDPOINT)


def write_toml(root: Path, **names: str) -> None:
  """Write a `truewire.toml` declaring one backend section per keyword argument, each
  naming its own root class (`python='Weather'`)."""
  sections = ['[project]', 'name = "demo"', '']
  for language, name in names.items():
    sections += [f'[{language}]', 'package = "demo"', 'src = "src"', f'name = "{name}"', '']
  if 'python' in names:
    sections += ['[python.cores.default]', 'base = "demo.core:Endpoint"', '']
  (root / 'truewire.toml').write_text('\n'.join(sections))


def test_no_endpoints_root_produces_no_findings(tmp_path: Path):
  """A project with no `endpoints` at all has no group to collide with anything."""
  write_toml(tmp_path, python='Demo')
  assert check_router_names(tmp_path) == []


def test_distinct_names_are_clean(tmp_path: Path):
  """The ordinary shape -- a client named after the API, groups named after its
  domains -- reports nothing."""
  write_toml(tmp_path, python='GitHub', typescript='GitHub', rust='GitHub')
  write_leaf(tmp_path, 'repos', 'get')
  write_leaf(tmp_path, 'issues', 'list')
  assert check_router_names(tmp_path) == []


def test_client_name_equal_to_a_group_is_flagged(tmp_path: Path):
  """The reported bug: a client named `Weather` over a spec with a `weather` group. The
  finding names the client, the group, and the `router.json` the group comes from."""
  write_toml(tmp_path, python='Weather')
  write_leaf(tmp_path, 'weather', 'forecast')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'router-name-collision'
  assert violations[0]['location'] == str(Path('spec', 'endpoints', 'weather', 'router.json'))
  message = violations[0]['message']
  assert "'weather'" in message
  assert "'Weather'" in message
  assert '[python]' in message


def test_one_finding_names_every_backend_that_declares_the_colliding_name(tmp_path: Path):
  """Three backends sharing one colliding `name` are one thing to fix, so they report as
  one finding naming all three -- not as three copies of the same line."""
  write_toml(tmp_path, python='Weather', typescript='Weather', rust='Weather')
  write_leaf(tmp_path, 'weather', 'forecast')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  message = violations[0]['message']
  assert '[python].name' in message
  assert '[typescript].name' in message
  assert '[rust].name' in message


def test_only_the_backend_whose_name_collides_is_flagged(tmp_path: Path):
  """A collision belongs to the section that declares the colliding name; the sections
  that name the client something else are clean."""
  write_toml(tmp_path, python='OpenMeteo', typescript='Weather')
  write_leaf(tmp_path, 'weather', 'forecast')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  assert '[typescript].name' in violations[0]['message']
  assert '[python]' not in violations[0]['message']


def test_language_narrows_the_root_to_one_backend(tmp_path: Path):
  """`truewire generate <language>` judges only the backend it is generating: a project
  whose `[rust].name` collides can still generate Python."""
  write_toml(tmp_path, python='OpenMeteo', rust='Weather')
  write_leaf(tmp_path, 'weather', 'forecast')

  assert check_router_names(tmp_path, language='python') == []
  assert len(check_router_names(tmp_path, language='rust')) == 1


def test_a_backend_the_project_does_not_declare_is_not_judged(tmp_path: Path):
  """An undeclared section generates no package, so its defaulted name has no module to
  collide inside of -- `[typescript]` absent is not `[typescript].name` = `Weather`."""
  write_toml(tmp_path, python='OpenMeteo')
  write_leaf(tmp_path, 'weather', 'forecast')

  assert check_router_names(tmp_path) == []
  assert check_router_names(tmp_path, language='typescript') == []


def test_client_name_defaulted_from_the_project_name_still_collides(tmp_path: Path):
  """`[python].name` is optional: a project called `weather` renders `Weather` anyway,
  and collides with its own `weather` group exactly the same way."""
  (tmp_path / 'truewire.toml').write_text(
    '[project]\nname = "weather"\n\n[python]\npackage = "weather"\nsrc = "src"\n\n'
    '[python.cores.default]\nbase = "weather.core:Endpoint"\n'
  )
  write_leaf(tmp_path, 'weather', 'forecast')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  assert "'Weather'" in violations[0]['message']


def test_a_group_named_like_its_parent_group_is_flagged(tmp_path: Path):
  """The same shape one level down: `alpha/beta/beta` renders `Beta` into the module that
  already declares `Beta`."""
  write_toml(tmp_path, python='Demo')
  write_leaf(tmp_path, 'alpha', 'beta', 'beta', 'get')
  write_leaf(tmp_path, 'alpha', 'beta', 'other')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  assert violations[0]['location'] == str(
    Path('spec', 'endpoints', 'alpha', 'beta', 'beta', 'router.json')
  )
  assert "'alpha.beta.beta'" in violations[0]['message']
  assert "'alpha.beta'" in violations[0]['message']


def test_a_group_repeating_a_grandparents_name_is_clean(tmp_path: Path):
  """Only the module that actually composes the group can collide with it: `alpha` and
  `alpha.beta.alpha` are declared in different modules and never meet."""
  write_toml(tmp_path, python='Demo')
  write_leaf(tmp_path, 'alpha', 'beta', 'alpha', 'get')

  assert check_router_names(tmp_path) == []


def test_two_siblings_rendering_one_class_name_are_flagged(tmp_path: Path):
  """`list-orders` and `list_orders` both render `ListOrders`, and one module composes
  both under that one name."""
  write_toml(tmp_path, python='Demo')
  write_leaf(tmp_path, 'list-orders', 'get')
  write_leaf(tmp_path, 'list_orders', 'get')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'router-name-collision'
  assert "'list-orders'" in violations[0]['message']
  assert "'list_orders'" in violations[0]['message']
  assert "'ListOrders'" in violations[0]['message']


def test_sibling_leaf_endpoints_rendering_one_class_name_are_flagged(tmp_path: Path):
  """Sibling leaves claim a name in their parent's module the same way sibling groups do
  -- `generate python` already refuses this shape, and the check now names it first."""
  write_toml(tmp_path, python='Demo')
  write_leaf(tmp_path, 'orders', 'list-open')
  write_leaf(tmp_path, 'orders', 'list_open')

  violations = check_router_names(tmp_path)

  assert len(violations) == 1
  assert "'ListOpen'" in violations[0]['message']


def test_siblings_in_unrelated_parents_are_clean(tmp_path: Path):
  """Two groups rendering the same class name under different parents are two classes in
  two modules, which is the ordinary shape -- `spot/orders` and `futures/orders`."""
  write_toml(tmp_path, python='Demo')
  write_leaf(tmp_path, 'spot', 'orders', 'get')
  write_leaf(tmp_path, 'futures', 'orders', 'get')

  assert check_router_names(tmp_path) == []


def test_client_class_names_resolve_the_way_each_backend_does(tmp_path: Path):
  """`[typescript]`/`[rust]` fall back to `[python].name`, which falls back to PascalCase
  of `[project].name` -- the resolution `codegen.typescript.root_class_name`,
  `codegen.rust.root_struct_name` and `plan.build`'s `root_class` each perform."""
  (tmp_path / 'truewire.toml').write_text(
    '[project]\nname = "open-meteo"\n\n[python]\npackage = "open_meteo"\nsrc = "src"\n\n'
    '[python.cores.default]\nbase = "open_meteo.core:Endpoint"\n\n'
    '[typescript]\npackage = "open_meteo"\nsrc = "src"\n\n'
    '[rust]\npackage = "open_meteo"\nsrc = "src"\nname = "OpenMeteoRs"\n'
  )
  assert client_class_names(load_project(tmp_path)) == {
    'python': 'OpenMeteo', 'typescript': 'OpenMeteo', 'rust': 'OpenMeteoRs',
  }


def test_truewire_check_fails_on_the_collision(tmp_path: Path, monkeypatch):
  """The gate itself: `truewire check` reports rule 18 as an `error` and exits non-zero,
  naming the group's `router.json` rather than crashing somewhere in generation."""
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'weather']).exit_code == 0
  project = tmp_path / 'weather'
  group = project / 'spec' / 'endpoints' / 'weather'
  (group / 'forecast').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Weather: forecasts.', 'upstream': 'https://example.com/docs',
    'core': 'default',
  }))
  (group / 'forecast' / 'endpoint.json').write_text(json.dumps({
    'docs': 'https://example.com/docs/forecast',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/forecast', 'method': 'GET',
      'description': 'Get a forecast.',
      'request': {
        'title': 'ForecastRequest', 'type': 'object', 'description': 'Where.',
        'required': ['latitude'],
        'properties': {'latitude': {'type': 'number', 'description': 'Degrees north.'}},
      },
      'response': {
        'title': 'Forecast', 'type': 'object', 'description': 'A forecast.',
        'required': ['temperature'],
        'properties': {'temperature': {'type': 'number', 'description': 'Celsius.'}},
      },
    },
  }))

  result = runner.invoke(app, ['check', '--project', str(project)])

  assert result.exit_code != 0
  assert "A router group's class name" in result.output, result.output
  assert 'weather' in result.output
  assert 'Traceback' not in result.output
