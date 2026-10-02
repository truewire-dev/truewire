"""`[policy].rate` and `[policy].retry` generated (docs/shape/workspace.md W15): the root
states them for the hand-written core, which builds its `HttpClient` from them.

Python sets `RATE`/`RETRY` on the root class only when `[policy]` sets one (the core's base
declares the defaults); TypeScript and Rust always state them on the root, which the core
names. The runtime behaviour per language is pinned by each runtime's own `policy` tests;
here a client `truewire init` wrote and `generate python` finished is shown to pace and
retry through its template core, end to end.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.cli.core_templates import TEMPLATES

from test_score import fixture_project

BACKENDS = '''
[go]
package = "forecasts"
src = "go"
name = "Forecasts"
module = "example.com/forecasts"
root = "go"

[typescript]
package = "forecasts"
src = "ts"
name = "Forecasts"

[rust]
package = "forecasts"
src = "rs"
name = "Forecasts"
'''


def set_policy(project: Path, *, rate: float | None, retry: bool):
  """Set `[policy].rate`/`.retry` on the lines the `init` template writes."""
  toml = project / 'truewire.toml'
  text = toml.read_text()
  text, rates = re.subn(r'(?m)^#? ?rate = \S+', f'rate = {rate}' if rate is not None else '# rate = 10', text)
  text, retries = re.subn(r'(?m)^retry = \S+', f'retry = {json.dumps(retry)}', text)
  assert rates == retries == 1
  toml.write_text(text)


def generate(project: Path, language: str, *flags: str):
  return CliRunner().invoke(app, ['generate', language, '--project', str(project), *flags])


@pytest.fixture
def project(tmp_path: Path) -> Path:
  return fixture_project(tmp_path, extra_toml=BACKENDS)


def test_each_root_states_the_policy(project: Path):
  set_policy(project, rate=0.5, retry=True)
  for language in ('python', 'typescript', 'rust'):
    assert generate(project, language).exit_code == 0

  python = (project / 'src' / 'forecasts' / 'main.py').read_text()
  assert 'RATE: ClassVar[float | None] = 0.5\n' in python
  assert 'RETRY: ClassVar[bool] = True\n' in python
  assert 'from typing_extensions import ClassVar' in python

  typescript = (project / 'ts' / 'forecasts' / 'main.ts').read_text()
  assert 'static readonly RATE: number | undefined = 0.5\n' in typescript
  assert 'static readonly RETRY: boolean = true\n' in typescript

  rust = (project / 'rs' / 'forecasts' / 'client.rs').read_text()
  assert 'impl Forecasts {\n    /// `[policy].rate`' in rust
  assert 'pub const RATE: ::core::option::Option<f64> = Some(0.5);\n' in rust
  assert 'pub const RETRY: bool = true;\n' in rust


def test_a_whole_rate_is_a_plain_typescript_number(project: Path):
  set_policy(project, rate=5, retry=False)
  assert generate(project, 'typescript').exit_code == 0
  assert generate(project, 'rust').exit_code == 0
  assert 'static readonly RATE: number | undefined = 5\n' in (project / 'ts' / 'forecasts' / 'main.ts').read_text()
  assert 'pub const RATE: ::core::option::Option<f64> = Some(5.0);\n' in (project / 'rs' / 'forecasts' / 'client.rs').read_text()


def test_the_defaults(project: Path):
  """Unset, Python's root is unchanged (it inherits the core's `None`/`False`), and the
  TypeScript and Rust roots state them."""
  for language in ('python', 'typescript', 'rust'):
    assert generate(project, language).exit_code == 0
  assert 'RATE' not in (project / 'src' / 'forecasts' / 'main.py').read_text()
  typescript = (project / 'ts' / 'forecasts' / 'main.ts').read_text()
  assert 'static readonly RATE: number | undefined = undefined\n' in typescript
  assert 'static readonly RETRY: boolean = false\n' in typescript
  rust = (project / 'rs' / 'forecasts' / 'client.rs').read_text()
  assert 'pub const RATE: ::core::option::Option<f64> = None;\n' in rust
  assert 'pub const RETRY: bool = false;\n' in rust


@pytest.fixture
def option_project(project: Path) -> Path:
  endpoints = project / 'spec' / 'endpoints'
  (endpoints / 'weather').rename(endpoints / 'option')
  return project


@pytest.mark.skipif(shutil.which('rustc') is None, reason='rustc is not installed')
@pytest.mark.parametrize('rate', [None, 0.5, 5], ids=['absent', 'fractional', 'whole'])
def test_rust_rate_with_an_option_router(option_project: Path, rate: float | None):
  """The root's router import must not shadow the standard Option in its RATE type."""
  set_policy(option_project, rate=rate, retry=False)
  result = generate(option_project, 'rust')
  assert result.exit_code == 0, result.output
  rust = (option_project / 'rs' / 'forecasts' / 'client.rs').read_text()
  router_import = 'use crate::option::Option;'
  assert router_import in rust
  assert 'pub option: Option,' in rust
  rate_const = next(line.strip() for line in rust.splitlines() if 'pub const RATE:' in line)

  # Compile the emitted declaration and colliding import without building a client.
  source = option_project / 'rate.rs'
  source.write_text(
    'mod option { pub struct Option {} }\n'
    f'{router_import}\n'
    'pub struct Forecasts { pub option: Option }\n'
    f'impl Forecasts {{ {rate_const} }}\n'
    'const _: ::core::option::Option<f64> = Forecasts::RATE;\n'
  )
  compiled = subprocess.run(
    ['rustc', '--edition', '2021', '--crate-type', 'lib', '--emit', 'metadata',
     str(source), '-o', str(option_project / 'rate.rmeta')],
    capture_output=True, text=True, timeout=30,
  )
  assert compiled.returncode == 0, compiled.stdout + compiled.stderr


def test_go_says_it_renders_neither(project: Path):
  set_policy(project, rate=5, retry=True)
  result = generate(project, 'go')
  assert result.exit_code == 0, result.output
  assert 'skipped [policy].rate: not generated for Go' in result.output
  assert 'skipped [policy].retry: not generated for Go' in result.output
  set_policy(project, rate=None, retry=False)
  assert '[policy]' not in generate(project, 'go').output


@pytest.mark.parametrize('language', ['python', 'typescript', 'rust'])
def test_editing_the_policy_without_regenerating_is_stale(project: Path, language: str):
  assert generate(project, language).exit_code == 0
  set_policy(project, rate=5, retry=True)
  stale = generate(project, language, '--check')
  assert stale.exit_code == 1, stale.output
  assert 'differ from the plan' in stale.output
  assert generate(project, language).exit_code == 0
  assert generate(project, language, '--check').exit_code == 0


@pytest.mark.skipif(shutil.which('rustfmt') is None, reason='rustfmt is not installed')
def test_the_rust_consts_satisfy_rustfmt(project: Path):
  set_policy(project, rate=2.5, retry=True)
  assert generate(project, 'rust').exit_code == 0
  checked = subprocess.run(
    ['rustfmt', '--edition', '2021', '--check', str(project / 'rs' / 'forecasts' / 'client.rs')],
    capture_output=True, text=True,
  )
  assert checked.returncode == 0, checked.stdout + checked.stderr


def test_every_init_template_builds_its_http_client_from_the_root():
  for name, template in TEMPLATES.items():
    source = template.core
    assert 'RATE: ClassVar[float | None] = None' in source, name
    assert 'RETRY: ClassVar[bool] = False' in source, name
    assert 'rate=cls.RATE, retry=cls.RETRY' in source, name


CLIENT = '''
import asyncio, sys, time
from forecasts.main import Forecasts

script = [int(status) for status in sys.argv[1].split(',') if status]

async def handle(reader, writer):
  while True:
    try:
      await reader.readuntil(b'\\r\\n\\r\\n')
    except asyncio.IncompleteReadError:
      break
    status = script.pop(0) if script else 200
    body = b'{"temperature": 21.5}'
    writer.write(f'HTTP/1.1 {status} X\\r\\nContent-Length: {len(body)}\\r\\n\\r\\n'.encode() + body)
    await writer.drain()
  writer.close()

async def main():
  server = await asyncio.start_server(handle, '127.0.0.1', 0)
  url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
  async with Forecasts.new(base_url=url) as client:
    began = time.monotonic()
    try:
      for _ in range(int(sys.argv[2])):
        forecast = await client.weather.forecast(latitude=52.0)
      print('ok', forecast['temperature'], round(time.monotonic() - began, 2))
    except Exception as error:
      print('error', type(error).__name__)

asyncio.run(main())
'''


def run_client(project: Path, script: str, calls: int) -> list[str]:
  """Call `weather.forecast` `calls` times through the generated client and its template
  core, against a server answering `script`'s statuses first; what the client printed."""
  done = subprocess.run(
    [sys.executable, '-c', CLIENT, script, str(calls)],
    capture_output=True, text=True, cwd=project, env={'PYTHONPATH': str(project / 'src')}, timeout=60,
  )
  assert done.returncode == 0, done.stderr
  return done.stdout.split()


def test_the_generated_python_client_retries_through_its_core(project: Path):
  set_policy(project, rate=None, retry=True)
  assert generate(project, 'python').exit_code == 0
  assert run_client(project, '503', 1)[:2] == ['ok', '21.5']

  set_policy(project, rate=None, retry=False)
  assert generate(project, 'python').exit_code == 0
  assert run_client(project, '503', 1) == ['error', 'ApiError']


def test_the_generated_python_client_paces_through_its_core(project: Path):
  set_policy(project, rate=5, retry=False)
  assert generate(project, 'python').exit_code == 0
  status, _, elapsed = run_client(project, '', 10)
  assert status == 'ok'
  assert float(elapsed) >= 1.8
