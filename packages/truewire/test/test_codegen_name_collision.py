"""
Regression: `truewire generate` refuses a client whose declared `name` is also one of its
router groups, in every backend, and writes nothing (`docs/spec/authoring.md` rule 18).

Each backend rendered the collision differently before this refusal existed, and each
assertion below is the exact output that failed:

- **Rust**: `client.rs` carried `use crate::weather::Weather;` above `pub struct Weather`,
  so the root struct held itself -- `error[E0255]: the name 'Weather' is defined multiple
  times`, `error[E0072]: recursive type 'client::Weather' has infinite size`, and
  `error[E0391]: cycle detected when computing drop-check constraints`.
- **TypeScript**: `main.ts` carried `import { Weather } from './weather/index.js'` above
  `export class Weather` -- `TS2440: Import declaration conflicts with local declaration`
  and `TS2395: Individual declarations in merged declaration must be all exported or all
  local`.
- **Python**: nothing raised at all. `main.py`'s `class Weather(ClientBase)` shadowed
  `from .weather import Weather`, so the `weather` property returned another root client
  and `forecast` was unreachable from the package's public surface.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app

TYPESCRIPT_SECTION = '\n[typescript]\npackage = "weather"\nsrc = "src"\nname = "Weather"\n'

RUST_SECTION = '\n[rust]\npackage = "weather"\nsrc = "src"\nname = "Weather"\n'


def collide(root: Path) -> Path:
  """Create the reported project: a client named `Weather` over a spec whose one router
  group is `weather`. Returns the project directory."""
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'weather', '--dir', str(root / 'weather')]).exit_code == 0
  project = root / 'weather'
  (project / 'truewire.toml').write_text(
    (project / 'truewire.toml').read_text() + TYPESCRIPT_SECTION + RUST_SECTION
  )
  group = project / 'spec' / 'endpoints' / 'weather'
  (group / 'forecast').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Weather: forecasts.',
    'upstream': 'https://example.com/docs/weather',
    'core': 'default',
  }))
  (group / 'forecast' / 'endpoint.json').write_text(json.dumps({
    'docs': 'https://example.com/docs/forecast',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/forecast', 'method': 'GET',
      'description': 'Get a forecast for one place.',
      'request': {
        'title': 'ForecastRequest', 'type': 'object', 'description': 'Where to forecast.',
        'required': ['latitude'],
        'properties': {'latitude': {'type': 'number', 'description': 'Degrees north.'}},
      },
      'response': {
        'title': 'Forecast', 'type': 'object', 'description': 'A forecast.',
        'required': ['temperature'],
        'properties': {'temperature': {'type': 'number', 'description': 'Degrees celsius.'}},
      },
    },
  }))
  return project


@pytest.mark.parametrize('language', ['python', 'typescript', 'rust'])
def test_generate_refuses_the_collision_and_writes_nothing(
  tmp_path: Path, language: str,
):
  """Every backend refuses before it renders, names the client, the group and the
  `router.json` it comes from, and leaves no manifest behind -- a half-written package
  would be exactly the broken tree the refusal exists to prevent."""
  project = collide(tmp_path)

  result = CliRunner().invoke(app, ['generate', language, '--project', str(project)])

  assert result.exit_code != 0, result.output
  assert 'rule 18' in result.output, result.output
  assert 'nothing generated' in result.output
  assert "'weather'" in result.output
  assert "'Weather'" in result.output
  assert f'[{language}]' in result.output
  assert not (project / '.truewire' / f'{language}-files.json').exists()
  assert 'Traceback' not in result.output


@pytest.mark.parametrize(
  'language,evidence',
  [
    ('python', 'from .weather import Weather'),
    ('typescript', "import { Weather } from './weather/index.js'"),
    ('rust', 'use crate::weather::Weather;'),
  ],
)
def test_the_broken_root_module_is_never_written(tmp_path: Path, language: str, evidence: str):
  """The line that made each backend's root module import the class it then declares
  itself is nowhere under the package after a refused run."""
  project = collide(tmp_path)

  CliRunner().invoke(app, ['generate', language, '--project', str(project)])

  written = [
    path for path in (project / 'src').rglob('*')
    if path.is_file() and evidence in path.read_text()
  ]
  assert written == []


def test_renaming_the_client_makes_every_backend_generate(tmp_path: Path):
  """The fix the message names actually works, and nothing else about the spec changes:
  a client called `OpenMeteo` over the same `weather` group generates in all three."""
  project = collide(tmp_path)
  (project / 'truewire.toml').write_text(
    (project / 'truewire.toml').read_text().replace('name = "Weather"', 'name = "OpenMeteo"')
  )

  runner = CliRunner()
  for language in ('python', 'typescript', 'rust'):
    result = runner.invoke(app, ['generate', language, '--project', str(project)])
    assert result.exit_code == 0, result.output
  assert 'class OpenMeteo' in (project / 'src' / 'weather' / 'main.py').read_text()
  assert 'export class OpenMeteo' in (project / 'src' / 'weather' / 'main.ts').read_text()
  assert 'pub struct OpenMeteo' in (project / 'src' / 'weather' / 'client.rs').read_text()


def shared_schema_collision(root: Path) -> Path:
  """Create the second reported shape: a router group `forecast` beside a shared schema
  titled `Forecast`, which both render as the class/type `Forecast`.

  `api.weather.gov` really is shaped that way -- `/gridpoints/{office}/{x},{y}/forecast`
  wants a `forecast` group, and the thing it returns wants to be called a forecast. The
  spec passed `truewire check`, generated valid Python, and then failed `tsc`.
  """
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'nws', '--dir', str(root / 'nws')]).exit_code == 0
  project = root / 'nws'
  (project / 'truewire.toml').write_text(
    (project / 'truewire.toml').read_text()
    + '\n[typescript]\npackage = "nws"\nsrc = "src"\nname = "Nws"\n'
  )
  (project / 'spec' / 'schemas.json').write_text(json.dumps({
    'Forecast': {
      'title': 'Forecast', 'type': 'object', 'description': 'A forecast for one place.',
      'required': ['temperature'],
      'properties': {'temperature': {'type': 'number', 'description': 'Degrees celsius.'}},
    },
  }))
  group = project / 'spec' / 'endpoints' / 'forecast'
  (group / 'get_forecast').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Forecasts for one place.',
    'upstream': 'https://example.com/docs/forecast',
    'core': 'default',
  }))
  (group / 'get_forecast' / 'endpoint.json').write_text(json.dumps({
    'docs': 'https://example.com/docs/forecast',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/forecast', 'method': 'GET',
      'description': 'Get a forecast for one place.',
      'request': {
        'title': 'GetForecastRequest', 'type': 'object', 'description': 'Where to forecast.',
        'required': ['latitude'],
        'properties': {'latitude': {'type': 'number', 'description': 'Degrees north.'}},
      },
      'response': {'$ref': 'Forecast', 'description': 'The forecast.'},
    },
  }))
  return project


class TestAGroupThatCollidesWithASharedSchema:
  """Rule 18's third shape: the module composing a group also imports the shared types."""

  def test_check_refuses_it(self, tmp_path: Path):
    """The gate that let this through is the one that has to catch it -- the whole point
    is that a spec is refused before a backend discovers it."""
    project = shared_schema_collision(tmp_path)

    result = CliRunner().invoke(app, ['check', '--project', str(project)])

    assert result.exit_code != 0, result.output
    assert "18. A router group" in result.output, result.output
    assert "'forecast'" in result.output
    assert "'Forecast'" in result.output

  def test_the_message_names_both_ways_out(self, tmp_path: Path):
    """Either name can move, and the spec's author is the one who knows which should."""
    project = shared_schema_collision(tmp_path)

    result = CliRunner().invoke(app, ['check', '--project', str(project)])

    assert 'rename the' in result.output
    assert 'schema' in result.output
    assert 'group directory' in result.output

  def test_renaming_the_schema_clears_it(self, tmp_path: Path):
    """The fix taken by the showcase that found this: the schema became
    `GridpointForecast`, which is the service's own term for it."""
    project = shared_schema_collision(tmp_path)
    schemas = project / 'spec' / 'schemas.json'
    schemas.write_text(schemas.read_text().replace('Forecast', 'GridpointForecast'))
    endpoint = project / 'spec' / 'endpoints' / 'forecast' / 'get_forecast' / 'endpoint.json'
    endpoint.write_text(
      endpoint.read_text().replace('"$ref": "Forecast"', '"$ref": "GridpointForecast"')
    )

    result = CliRunner().invoke(app, ['check', '--project', str(project)])

    assert result.exit_code == 0, result.output

  def test_a_schema_no_group_is_named_after_is_fine(self, tmp_path: Path):
    """A guard on the guard: the check must not refuse every shared schema in sight."""
    project = shared_schema_collision(tmp_path)
    schemas = project / 'spec' / 'schemas.json'
    schemas.write_text(schemas.read_text().replace('Forecast', 'Reading'))
    endpoint = project / 'spec' / 'endpoints' / 'forecast' / 'get_forecast' / 'endpoint.json'
    endpoint.write_text(endpoint.read_text().replace('"$ref": "Forecast"', '"$ref": "Reading"'))

    result = CliRunner().invoke(app, ['check', '--project', str(project)])

    assert result.exit_code == 0, result.output
