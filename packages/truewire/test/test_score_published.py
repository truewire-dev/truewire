"""Registry checks run against fake transports, including failures and exact request paths."""
import json
from pathlib import Path

import httpx
import pytest

from truewire.project import load_project
from truewire.score.rows import published
from truewire.score.published import TIMEOUT, USER_AGENT, lookup


PACKAGES = (
  ('python', 'truewire-weather-gov', '0.1.0', 'PyPI', '/pypi/truewire-weather-gov/0.1.0/json'),
  ('typescript', '@truewire/weather-gov', '0.1.0', 'npm', '/%40truewire%2Fweather-gov/0.1.0'),
  ('rust', 'truewire-weather-gov', '0.2.0', 'crates.io', '/api/v1/crates/truewire-weather-gov/0.2.0'),
  ('go', 'example.com/Weather/API/v2', '2.1.0', 'Go proxy', '/example.com/!weather/!a!p!i/v2/@v/v2.1.0.info'),
)


@pytest.mark.parametrize('language,name,version,registry,path', PACKAGES)
@pytest.mark.parametrize('outcome', [200, 404, 500, 503, 'network', 'timeout', 301, 401, 403, 429])
def test_registry_outcomes(language, name, version, registry, path, outcome):
  requests = []

  def handle(request):
    requests.append(request)
    assert request.method == 'GET'
    assert request.url.scheme == 'https'
    assert request.url.host == {
      'python': 'pypi.org', 'typescript': 'registry.npmjs.org', 'rust': 'crates.io', 'go': 'proxy.golang.org',
    }[language]
    assert request.url.raw_path.decode() == path
    assert request.headers['User-Agent'] == USER_AGENT
    assert set(request.extensions['timeout'].values()) == {TIMEOUT}
    if outcome == 'network':
      raise httpx.ConnectError('offline', request=request)
    if outcome == 'timeout':
      raise httpx.ReadTimeout('timed out', request=request)
    return httpx.Response(outcome, headers={'Location': 'https://example.com/redirect'})

  cell = lookup(name, version, language, transport=httpx.MockTransport(handle))
  assert len(requests) == 1
  if outcome == 200:
    assert (cell.status, cell.detail) == ('pass', f'{name} {version}')
  elif outcome == 404:
    assert (cell.status, cell.detail) == ('fail', f'{name} {version} is not on {registry}')
  elif outcome in (500, 503, 'network', 'timeout'):
    assert (cell.status, cell.detail) == ('unchecked', f'{registry} unreachable')
  else:
    assert (cell.status, cell.detail) == ('unchecked', f'{registry} returned HTTP {outcome}')


def fixture_project(root: Path):
  (root / 'truewire.toml').write_text('''[project]
name = "different_import_name"
[python]
src = "packages/python/src"
cores = { default = { base = "example.core:Endpoint" } }
[typescript]
src = "packages/typescript/src"
[rust]
src = "packages/rust/src"
''')
  for language, name, version, _, _ in PACKAGES[:3]:
    package = root / 'packages' / language
    package.mkdir(parents=True)
    if language == 'typescript':
      (package / 'package.json').write_text(json.dumps({'name': name, 'version': version}))
    else:
      filename, section = ('pyproject.toml', 'project') if language == 'python' else ('Cargo.toml', 'package')
      (package / filename).write_text(f'[{section}]\nname = "{name}"\nversion = "{version}"\n')
  return load_project(root)


def test_each_declared_language_reads_its_own_manifest(tmp_path):
  project = fixture_project(tmp_path)
  requests = []

  def handle(request):
    requests.append(request.url.raw_path.decode())
    return httpx.Response(200)

  row = published(project, transport=httpx.MockTransport(handle))
  assert row.status == 'pass'
  assert list(row.cells) == ['python', 'typescript', 'rust']
  assert requests == [path for *_, path in PACKAGES[:3]]
  assert [cell.detail for cell in row.cells.values()] == [f'{name} {version}' for _, name, version, _, _ in PACKAGES[:3]]


def test_absent_python_version_does_not_hide_other_languages(tmp_path):
  project = fixture_project(tmp_path)
  manifest = tmp_path / 'packages/python/pyproject.toml'
  manifest.write_text(manifest.read_text().replace('0.1.0', '9.9.9'))
  transport = httpx.MockTransport(lambda request: httpx.Response(404 if '/9.9.9/' in request.url.path else 200))
  row = published(project, transport=transport)
  assert row.status == 'fail'
  assert row.cells['python'].detail == 'truewire-weather-gov 9.9.9 is not on PyPI'
  assert row.cells['typescript'].status == row.cells['rust'].status == 'pass'


@pytest.mark.parametrize('language,filename', [('python', 'pyproject.toml'), ('typescript', 'package.json'), ('rust', 'Cargo.toml')])
@pytest.mark.parametrize('content', [None, 'invalid [', '', '[]'])
def test_missing_or_invalid_manifest_is_unchecked(tmp_path, language, filename, content):
  project = fixture_project(tmp_path)
  manifest = tmp_path / 'packages' / language / filename
  if content is None:
    manifest.unlink()
  else:
    manifest.write_text(content)

  def handle(request):
    assert request.url.host != {'python': 'pypi.org', 'typescript': 'registry.npmjs.org', 'rust': 'crates.io'}[language]
    return httpx.Response(200)

  row = published(project, transport=httpx.MockTransport(handle))
  assert row.cells[language].status == 'unchecked'
  assert filename in row.cells[language].detail


@pytest.mark.parametrize('field', ['name', 'version'])
@pytest.mark.parametrize('value', [None, '', ' ', 12, {'workspace': True}])
def test_missing_or_indirect_metadata_cannot_pass(tmp_path, field, value):
  project = fixture_project(tmp_path)
  package = {'name': '@truewire/weather-gov', 'version': '0.1.0', field: value}
  (tmp_path / 'packages/typescript/package.json').write_text(json.dumps(package))
  row = published(project, transport=httpx.MockTransport(lambda _: httpx.Response(200)))
  assert row.cells['typescript'].status == 'unchecked'
  assert 'no declared' in row.cells['typescript'].detail


@pytest.mark.parametrize('language,filename,contents', [
  ('python', 'pyproject.toml', '[project]\nname = "weather"\ndynamic = ["version"]\n'),
  ('rust', 'Cargo.toml', '[package]\nname = "weather"\nversion.workspace = true\n'),
])
def test_a_version_needing_build_tool_resolution_is_unchecked(tmp_path, language, filename, contents):
  project = fixture_project(tmp_path)
  (tmp_path / 'packages' / language / filename).write_text(contents)
  row = published(project, transport=httpx.MockTransport(lambda _: httpx.Response(200)))
  assert row.cells[language].status == 'unchecked'
  assert row.cells[language].detail == f'{filename}: no declared version for weather'


def test_flat_layout_reads_manifest_at_project_root(tmp_path):
  project = fixture_project(tmp_path)
  manifest = tmp_path / 'packages/python/pyproject.toml'
  manifest.rename(tmp_path / 'pyproject.toml')
  row = published(project, transport=httpx.MockTransport(lambda _: httpx.Response(200)))
  assert row.cells['python'].status == 'pass'
  assert row.cells['python'].detail == 'truewire-weather-gov 0.1.0'


def test_go_proxy_preserves_v_prefix_and_escapes_version_case():
  def handle(request):
    assert request.url.raw_path == b'/example.com/mod/@v/v1.0.0-!r!c1.info'
    return httpx.Response(200)

  cell = lookup('example.com/mod', 'v1.0.0-RC1', 'go', transport=httpx.MockTransport(handle))
  assert (cell.status, cell.detail) == ('pass', 'example.com/mod v1.0.0-RC1')


def test_go_without_declared_version_is_unchecked_without_a_request(tmp_path):
  (tmp_path / 'truewire.toml').write_text('[project]\nname = "weather"\n[go]\nmodule = "example.com/weather"\n')
  (tmp_path / 'go.mod').write_text('module example.com/weather\n\ngo 1.23\n')

  def handle(request):
    pytest.fail(f'a package without a declared version must not query {request.url}')

  row = published(load_project(tmp_path), transport=httpx.MockTransport(handle))
  assert row.status == 'unchecked'
  assert row.cells['go'].detail == 'no declared version for Go package'
