"""`[policy].refuse` generated (docs/shape/workspace.md W15): each backend's refused method
fails with the package's `RefusedByPolicy` before any request, `generate --check` sees an
edited `refuse`, and a refusal no generator can render fails `generate`.

The runtime proof per language -- the call fails and nothing reached the server -- is in
`examples/kraken` (`test/test_policy.py`, `test/policy.test.ts`, `tests/policy.rs`), which
refuses `spot.funding.withdraw` and runs in CI."""
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.policy import python_module
from truewire.plan.build import build_plan
from truewire.project import load_project

from test_score import fixture_project

BACKENDS = '''
[typescript]
package = "forecasts"
src = "ts"
name = "Forecasts"

[rust]
package = "forecasts"
src = "rs"
name = "Forecasts"

[go]
package = "forecasts"
src = "go"
name = "Forecasts"
module = "example.com/forecasts"
root = "go"
'''
LANGUAGES = ('python', 'typescript', 'rust')


def refuse(project: Path, *functions: str):
  """Set `[policy].refuse` in the project's `truewire.toml` (the `init` template's line)."""
  toml = project / 'truewire.toml'
  text, count = re.subn(r'(?m)^refuse = \[.*?\]', f'refuse = {json.dumps(list(functions))}', toml.read_text())
  assert count == 1
  toml.write_text(text)


def add_endpoint(project: Path, name: str, **extra):
  """A second endpoint beside `weather.forecast`, so one is refused and one is not."""
  (project / 'spec' / 'endpoints' / 'weather' / name).mkdir()
  (project / 'spec' / 'endpoints' / 'weather' / name / 'endpoint.json').write_text(json.dumps({
    'docs': f'https://example.com/docs/{name}',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': f'/{name}', 'method': 'GET',
      'description': f'Get the {name}.',
      'response': {
        'title': name.title(), 'type': 'object', 'description': f'The {name}.',
        'properties': {'value': {'type': 'number', 'description': 'A value.'}},
      },
    },
    **extra,
  }))


def generate(project: Path, language: str, *flags: str):
  return CliRunner().invoke(app, ['generate', language, '--project', str(project), *flags])


@pytest.fixture
def project(tmp_path: Path) -> Path:
  """`weather.forecast` refused, `weather.alerts` not; nothing generated yet."""
  project = fixture_project(tmp_path, extra_toml=BACKENDS)
  add_endpoint(project, 'alerts')
  refuse(project, 'weather.forecast')
  return project


def test_the_plan_marks_only_the_refused_endpoint(project: Path):
  plan = build_plan(load_project(project))
  assert {endpoint.function: endpoint.refused for endpoint in plan.endpoints} == {
    'weather.alerts': False, 'weather.forecast': True,
  }
  refused = next(endpoint for endpoint in plan.to_json()['endpoints'] if endpoint['path'] == ['weather', 'forecast'])
  assert refused['refused'] is True


def test_each_backend_guards_the_refused_endpoint_only(project: Path):
  for language in LANGUAGES:
    result = generate(project, language)
    assert result.exit_code == 0, result.output
  python, typescript, rust = project / 'src' / 'forecasts', project / 'ts' / 'forecasts', project / 'rs' / 'forecasts'

  assert (python / 'policy.py').read_text().count('class RefusedByPolicy(LogicError):') == 1
  assert "@refused('weather.forecast')\nclass ForecastEndpoint(Endpoint):" in (python / 'weather' / 'forecast.py').read_text()
  assert 'from forecasts.policy import refused' in (python / 'weather' / 'forecast.py').read_text()
  assert 'refused' not in (python / 'weather' / 'alerts.py').read_text()

  assert 'export class RefusedByPolicy extends LogicError {' in (typescript / 'policy.ts').read_text()
  forecast_ts = (typescript / 'weather' / 'forecast.ts').read_text()
  assert "import { refuse } from '../policy.js'" in forecast_ts
  assert "Promise<Forecast> {\n    refuse('weather.forecast')\n    return this.core.request({" in forecast_ts
  assert 'refuse' not in (typescript / 'weather' / 'alerts.ts').read_text()
  assert "export { RefusedByPolicy } from './policy.js'" in (typescript / 'index.ts').read_text()

  assert 'impl From<RefusedByPolicy> for Error {' in (rust / 'policy.rs').read_text()
  forecast_rs = (rust / 'weather' / 'forecast.rs').read_text()
  assert 'use crate::policy::refuse;' in forecast_rs
  assert ') -> Result<serde_json::Value> {\n        refuse("weather.forecast")?;\n' in forecast_rs
  assert 'refuse' not in (rust / 'weather' / 'alerts.rs').read_text()
  assert 'pub mod policy;' in (rust / 'lib.rs').read_text()

  for language in LANGUAGES:
    assert generate(project, language, '--check').exit_code == 0


@pytest.mark.skipif(shutil.which('rustfmt') is None, reason='rustfmt is not installed')
def test_the_rust_refusal_satisfies_rustfmt(project: Path):
  """No formatter runs over the Rust output, so `policy.rs` and the guarded body must be
  laid out as `rustfmt` would."""
  assert generate(project, 'rust').exit_code == 0
  (project / 'rs' / 'forecasts' / 'core.rs').write_text('// The hand-written core, stubbed for the check.\n')
  for path in ('policy.rs', 'weather/forecast.rs', 'weather/mod.rs', 'lib.rs'):
    checked = subprocess.run(
      ['rustfmt', '--edition', '2021', '--check', str(project / 'rs' / 'forecasts' / path)], capture_output=True, text=True,
    )
    assert checked.returncode == 0, f'{path}:\n{checked.stdout}{checked.stderr}'


def test_go_says_it_renders_no_refusal(project: Path):
  result = generate(project, 'go')
  assert result.exit_code == 0, result.output
  assert 'skipped weather.forecast: [policy].refuse is not generated for Go' in result.output


@pytest.mark.parametrize('edit', ['add', 'remove'])
@pytest.mark.parametrize('language', LANGUAGES)
def test_editing_refuse_without_regenerating_is_stale(project: Path, language: str, edit: str):
  """S4: the refusal is generated code, so `generate --check` fails on an edited `refuse`
  -- one more endpoint refused, or every refusal dropped -- until the tree is regenerated."""
  assert generate(project, language).exit_code == 0
  assert generate(project, language, '--check').exit_code == 0
  refuse(project, *(['weather.forecast', 'weather.alerts'] if edit == 'add' else []))
  stale = generate(project, language, '--check')
  assert stale.exit_code == 1, stale.output
  assert 'differ from the plan' in stale.output
  assert generate(project, language).exit_code == 0
  assert generate(project, language, '--check').exit_code == 0


@pytest.mark.parametrize('language', LANGUAGES)
def test_a_project_with_nothing_refused_gets_no_policy_module(tmp_path: Path, language: str):
  project = fixture_project(tmp_path, extra_toml=BACKENDS)
  assert generate(project, language).exit_code == 0
  package = {'python': 'src', 'typescript': 'ts', 'rust': 'rs'}[language]
  assert not list((project / package / 'forecasts').glob('policy.*'))


@pytest.mark.parametrize('language', LANGUAGES)
def test_a_refusal_no_generator_can_render_fails_generate(project: Path, language: str):
  refuse(project, 'weather.nowhere')
  missing = generate(project, language)
  assert missing.exit_code == 1
  assert "[policy].refuse names 'weather.nowhere', which is no generated endpoint" in missing.output

  add_endpoint(project, 'radar', surface={'kind': 'handwritten', 'symbol': 'weather.radar:radar', 'reason': 'Served by hand.'})
  refuse(project, 'weather.radar')
  handwritten = generate(project, language)
  assert handwritten.exit_code == 1
  assert "[policy].refuse names 'weather.radar', whose method is written by hand" in handwritten.output


def test_the_python_decorator_refuses_every_public_method():
  namespace: dict = {}
  exec(python_module(), namespace)  # noqa: S102 -- the generated module, as a package imports it
  refused, RefusedByPolicy = namespace['refused'], namespace['RefusedByPolicy']
  calls: list[str] = []

  @refused('account.withdraw')
  class Withdraw:
    async def withdraw(self, amount: str) -> str:
      calls.append(amount)
      return amount

    def withdraw_paged(self):
      calls.append('paged')

    def __call__(self, amount: str) -> str:
      calls.append(amount)
      return amount

    def _helper(self) -> str:
      return 'kept'

  endpoint = Withdraw()
  with pytest.raises(RefusedByPolicy) as raised:
    asyncio.run(endpoint.withdraw('1'))
  assert raised.value.endpoint == 'account.withdraw'
  assert 'account.withdraw is refused by [policy].refuse' in str(raised.value)
  with pytest.raises(RefusedByPolicy):
    endpoint.withdraw_paged()
  with pytest.raises(RefusedByPolicy):
    endpoint('2')
  assert calls == []
  assert endpoint._helper() == 'kept'
  assert Withdraw.withdraw.__name__ == 'withdraw'


# -- hand-written paths no generated guard reaches (review of PR #28: TRU-274, TRU-276, TRU-277)

EXTRAS = {
  'python': '\n[[python.extras."weather"]]\nfile = "forecast_plus"\nclass = "ForecastPlus"\nreplaces = "forecast"\n',
  'typescript': '\n[[typescript.extras."weather"]]\nfile = "forecast_plus"\nclass = "ForecastPlus"\nreplaces = "forecast"\n',
}


@pytest.mark.parametrize('language', sorted(EXTRAS))
def test_an_extras_class_replacing_a_refused_endpoint_fails_generate(project: Path, language: str):
  """A `replaces` extras class sits behind the router in the refused leaf's place, and an
  override that reaches the core skips the generated guard: a refusal no code enforces."""
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text() + EXTRAS[language])
  result = generate(project, language)
  assert result.exit_code == 1, result.output
  assert "[policy].refuse names 'weather.forecast', whose method is written by hand" in result.output
  assert 'replaces' in result.output


BACKEND_SKIP = """
from truewire.codegen.python import Generator


class Backend(Generator):
  def skip_endpoint(self, endpoint):
    return getattr(endpoint.spec, 'path', None) == '/forecast' or super().skip_endpoint(endpoint)


generator = Backend()
"""

BACKEND_OVERRIDE = """
from truewire.codegen.python import Generator


class Backend(Generator):
  def rpc_endpoint(self, endpoint, references, *, class_name, method_name):
    return (
      'from forecasts.core import Endpoint\\n\\n\\n'
      f'class {class_name}(Endpoint):\\n'
      f'  async def {method_name}(self, **kwargs):\\n'
      '    return await self.request(kwargs, method="GET", path="/forecast")\\n'
    )


generator = Backend()
"""


def with_backend(project: Path, backend: str) -> Path:
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('[python]\n', '[python]\nbackend = "backend.py"\n', 1))
  (project / 'backend.py').write_text(backend)
  return project


def test_a_backend_skipped_refused_endpoint_fails_generate(project: Path):
  result = generate(with_backend(project, BACKEND_SKIP), 'python')
  assert result.exit_code == 1, result.output
  assert "'weather.forecast', whose method is written by hand" in result.output
  assert not (project / 'src' / 'forecasts' / 'policy.py').exists()


def test_a_backend_that_renders_a_refused_endpoint_without_the_guard_fails_generate(project: Path):
  result = generate(with_backend(project, BACKEND_OVERRIDE), 'python')
  assert result.exit_code == 1, result.output
  assert "'weather.forecast', but its module was rendered without `@refused`" in result.output


def test_go_fails_on_a_refusal_that_names_nothing(project: Path):
  refuse(project, 'weather.nowhere')
  result = generate(project, 'go')
  assert result.exit_code == 1, result.output
  assert "[policy].refuse names 'weather.nowhere', which is no generated endpoint" in result.output


def test_the_typescript_refusal_keeps_the_runtime_error_name(project: Path):
  """`isTruewireError` recognises an error from a duplicated `@truewire/core` bundle by the
  runtime's class names; a `name` of its own would hide the refusal from it (TRU-275)."""
  assert generate(project, 'typescript').exit_code == 0
  policy = (project / 'ts' / 'forecasts' / 'policy.ts').read_text()
  assert 'readonly name' not in policy
  forecast = (project / 'ts' / 'forecasts' / 'weather' / 'forecast.ts').read_text()
  assert 'Refused by `[policy].refuse`: rejects with `RefusedByPolicy`' in forecast
