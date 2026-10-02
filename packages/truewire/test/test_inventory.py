"""S1 uses the approved upstream inventory as its denominator; check enforces its shape."""
import json
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from truewire.cli import app
from truewire.project import load_project
from truewire.score import PROJECT, Scorecard, render
from truewire.score.rows import coverage
from truewire.spec.inventory import load_inventory

from test_score import fixture_project


@pytest.fixture
def project(tmp_path: Path) -> Path:
  return fixture_project(tmp_path)


def write_inventory(project: Path, **overrides) -> Path:
  data = {
    'source': 'https://example.com/docs',
    'approved': {'by': 'Test reviewer', 'date': '2026-10-01'},
    'endpoints': [{'method': 'GET', 'path': '/forecast', 'endpoint': 'weather.forecast'}],
  }
  data.update(overrides)
  path = load_project(project).spec_dir / 'inventory.json'
  path.write_text(json.dumps(data))
  return path


def cell(project: Path):
  return coverage(load_project(project)).cells[PROJECT]


def check(project: Path, *args: str):
  return CliRunner().invoke(app, ['check', '--project', str(project), *args])


def test_missing_inventory_fails_coverage_but_check_still_passes(project: Path):
  assert (cell(project).status, cell(project).detail) == ('fail', 'no spec/inventory.json')
  assert check(project).exit_code == 0


def test_approved_complete_inventory_passes_and_prints_count(project: Path):
  write_inventory(project)
  row = coverage(load_project(project))
  assert row.status == 'pass'
  assert row.cells[PROJECT].detail == '1/1 endpoints'
  assert 'pass       1/1 endpoints' in render(Scorecard('forecasts', ('python',), [row]))[1]
  assert check(project).exit_code == 0


def test_exclusions_count_as_accounted_for(project: Path):
  write_inventory(project, endpoints=[
    {'method': 'GET', 'path': '/forecast', 'endpoint': 'weather.forecast'},
    {'method': 'POST', 'path': '/admin', 'excluded': 'Operator-only endpoint.'},
  ])
  assert (cell(project).status, cell(project).detail) == ('pass', '2/2 endpoints')
  assert check(project).exit_code == 0


@pytest.mark.parametrize('method', ['GET', 'get', 'Get'])
@pytest.mark.parametrize('disposition', [{}, {'excluded': 'Private.'}, {'endpoint': 'weather.forecast'}])
def test_duplicate_operations_fail_check_and_score(project: Path, method: str, disposition):
  write_inventory(project, endpoints=[
    {'method': 'GET', 'path': '/forecast', 'endpoint': 'weather.forecast'},
    {'method': method, 'path': '/forecast', 'endpoint': 'weather.forecast'},
    {'method': method, 'path': '/forecast', **disposition},
  ])
  _, errors = load_inventory(load_project(project).spec_dir)
  expected = [
    f'spec/inventory.json.endpoints[{index}]: duplicates endpoints[0] GET /forecast'
    for index in (1, 2)
  ]
  assert errors == expected
  result = check(project)
  assert result.exit_code == 1, result.output
  assert 'duplicates endpoints[0]' in result.output
  score = cell(project)
  assert score.status == 'fail'
  for error in expected:
    assert error in score.output


def test_distinct_operations_can_share_endpoint_without_duplicates(project: Path):
  write_inventory(project, endpoints=[
    {'method': method, 'path': path, 'endpoint': 'weather.forecast'}
    for method, path in [('GET', '/forecast'), ('POST', '/forecast'), ('GET', '/Forecast')]
  ])
  assert check(project).exit_code == 0
  assert (cell(project).status, cell(project).detail) == ('pass', '3/3 endpoints')


@pytest.mark.parametrize('entry', [None, {}, {'method': [], 'path': '/forecast'}, {'method': 'GET', 'path': {}}])
def test_duplicate_check_tolerates_malformed_entries(project: Path, entry):
  write_inventory(project, endpoints=[entry, entry])
  _, errors = load_inventory(load_project(project).spec_dir)
  assert errors
  assert not any('duplicates' in error for error in errors)
  result = check(project)
  assert result.exit_code == 1, result.output
  assert 'Traceback' not in result.output


def test_unapproved_inventory_fails_coverage_but_is_valid_shape(project: Path):
  write_inventory(project, approved=None)
  assert (cell(project).status, cell(project).detail) == ('fail', '1/1 endpoints; inventory not approved')
  assert check(project).exit_code == 0


@pytest.mark.parametrize('approved', [None, {'by': 'Test reviewer', 'date': '2026-10-01'}])
def test_unspecified_entries_pass_check_but_fail_coverage_with_ratio(project: Path, monkeypatch, approved):
  write_inventory(project, approved=approved, endpoints=[
    {'method': 'GET', 'path': '/forecast', 'endpoint': 'weather.forecast'},
    {'method': 'POST', 'path': '/admin', 'excluded': 'Operator-only endpoint.'},
    {'method': 'GET', 'path': '/alerts'},
  ])
  assert check(project).exit_code == 0
  result = cell(project)
  expected = '2/3 endpoints; 1 unspecified'
  if approved is None:
    expected += '; inventory not approved'
  assert (result.status, result.detail) == ('fail', expected)
  monkeypatch.setattr('truewire.score.rows.MEASURES', (coverage,))
  output = CliRunner().invoke(app, ['score', '--project', str(project)])
  assert output.exit_code == 1
  assert f'FAIL       {expected}' in output.output
  assert '/alerts' not in output.output
  verbose = CliRunner().invoke(app, ['score', '--project', str(project), '--verbose'])
  assert verbose.exit_code == 1
  assert 'endpoints[2] GET /alerts: unspecified' in verbose.output


def test_lists_every_missing_endpoint_and_bare_entry_even_when_unapproved(project: Path, monkeypatch):
  write_inventory(project, approved=None, endpoints=[
    {'method': 'GET', 'path': '/missing', 'endpoint': 'weather.missing'},
    {'method': 'GET', 'path': '/absent', 'endpoint': 'weather.absent'},
    {'method': 'GET', 'path': '/bare'},
    {'method': 'POST', 'path': '/bare-too'},
  ])
  result = cell(project)
  assert result.status == 'fail'
  for reason in ('0/4 endpoints', '2 unspecified', 'weather.missing', 'weather.absent', 'inventory not approved'):
    assert reason in result.detail
    assert reason in result.output
  assert 'endpoints[2] GET /bare: unspecified' in result.output
  assert 'endpoints[3] POST /bare-too: unspecified' in result.output
  assert check(project).exit_code == 0
  # The normal table abbreviates long details; verbose must preserve every finding.
  monkeypatch.setattr('truewire.score.rows.MEASURES', (coverage,))
  output = CliRunner().invoke(app, ['score', '--project', str(project), '--verbose'])
  assert output.exit_code == 1
  assert result.output.strip() in output.output


@pytest.mark.parametrize('endpoints', [[], [{'method': 'GET', 'path': '/admin', 'excluded': 'Private.'}]])
def test_unlisted_spec_endpoints_only_warn_and_print_on_a_passing_row(project: Path, endpoints):
  write_inventory(project, endpoints=endpoints)
  row = coverage(load_project(project))
  assert row.status == 'pass'
  assert f'{len(endpoints)}/{len(endpoints)} endpoints' in row.cells[PROJECT].detail
  line = render(Scorecard('forecasts', ('python',), [row]))[1]
  assert 'warning: spec endpoints missing from inventory: weather.forecast' in line


@pytest.mark.parametrize('changes, reason', [
  ({'source': 'not a URL'}, 'source'),
  ({'approved': {}}, 'by'),
  ({'approved': {'by': ' ', 'date': '2026-10-01'}}, 'approved.by'),
  ({'approved': {'by': 'Reviewer', 'date': '2026-02-30'}}, 'approved.date'),
  ({'approved': False}, 'approved'),
  ({'endpoints': {}}, 'endpoints'),
  ({'endpoints': [None]}, 'endpoints[0]'),
  ({'extra': True}, 'Additional properties'),
])
def test_invalid_shape_fails_check_and_score(project: Path, changes, reason: str):
  write_inventory(project, **changes)
  result = check(project)
  assert result.exit_code == 1, result.output
  assert 'inventory.json' in result.output
  assert reason in result.output
  assert cell(project).status == 'fail'
  assert reason in cell(project).detail


@pytest.mark.parametrize('entry, reason', [
  ({'method': 'GET', 'path': '/both', 'endpoint': 'weather.forecast', 'excluded': 'Private.'}, 'must not have both'),
  ({'method': 'GET', 'path': '/blank', 'excluded': '  '}, 'excluded'),
  ({'method': 'GET', 'path': '/multiline', 'excluded': 'reason\n'}, 'excluded'),
  ({'method': 'GET', 'path': '/multiline', 'excluded': 'reason\nsecond'}, 'excluded'),
  ({'method': 'GET', 'path': '/null', 'excluded': None}, 'excluded'),
  ({'path': '/missing', 'endpoint': 'weather.forecast'}, 'method'),
  ({'method': 'GET', 'endpoint': 'weather.forecast'}, 'path'),
  ({'method': 'GET', 'path': '/bad', 'endpoint': 'weather/forecast'}, 'endpoint'),
  ({'method': 'GET', 'path': '/extra', 'endpoint': 'weather.forecast', 'notes': 'No extras.'}, 'Additional properties'),
])
def test_entry_dispositions_must_be_valid_and_mutually_exclusive(project: Path, entry, reason: str):
  write_inventory(project, endpoints=[entry])
  result = check(project)
  assert result.exit_code == 1, result.output
  assert reason in result.output
  assert cell(project).status == 'fail'


@pytest.mark.parametrize('contents', ['{', 'null', '[]', '42'])
def test_invalid_document_is_a_failure_without_a_traceback(project: Path, contents: str):
  write_inventory(project).write_text(contents)
  result = check(project)
  assert result.exit_code == 1, result.output
  assert 'inventory.json' in result.output
  assert 'Traceback' not in result.output
  assert cell(project).status == 'fail'


def test_unknown_reference_is_coverage_failure_not_shape_failure(project: Path):
  write_inventory(project, endpoints=[{'method': 'GET', 'path': '/missing', 'endpoint': 'weather.missing'}])
  assert check(project).exit_code == 0
  assert cell(project).status == 'fail'
  assert "endpoint 'weather.missing' is not in the spec" in cell(project).detail


def test_ids_are_directory_paths_not_function_overrides(project: Path):
  endpoint = project / 'spec/endpoints/weather/forecast/endpoint.json'
  data = json.loads(endpoint.read_text())
  data['function'] = 'other.name'
  endpoint.write_text(json.dumps(data))
  write_inventory(project)
  assert cell(project).status == 'pass'
  write_inventory(project, endpoints=[{'method': 'GET', 'path': '/forecast', 'endpoint': 'other.name'}])
  assert cell(project).status == 'fail'


def test_custom_spec_directory_and_subdivision_check(project: Path):
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('dir = "spec"', 'dir = "custom-spec"'))
  (project / 'spec').rename(project / 'custom-spec')
  write_inventory(project)
  assert cell(project).status == 'pass'
  assert check(project).exit_code == 0
  write_inventory(project, endpoints=[{'method': 'GET', 'path': '/bare'}])
  result = CliRunner().invoke(app, ['check', '--path', str(project / 'custom-spec/endpoints/weather')])
  assert result.exit_code == 0, result.output
  assert cell(project).status == 'fail'
  assert '0/1 endpoints; 1 unspecified' in cell(project).detail
  write_inventory(project, endpoints=[{
    'method': 'GET', 'path': '/both', 'endpoint': 'weather.forecast', 'excluded': 'Private.',
  }])
  result = CliRunner().invoke(app, ['check', '--path', str(project / 'custom-spec/endpoints/weather')])
  assert result.exit_code == 1, result.output
  assert 'inventory.json' in result.output


def test_packaged_schema_is_valid():
  schema = json.loads(files('truewire').joinpath('resources/inventory.schema.json').read_text())
  Draft202012Validator.check_schema(schema)


def test_inventory_read_error_is_reported(project: Path):
  (project / 'spec/inventory.json').mkdir()
  _, errors = load_inventory(project / 'spec')
  assert errors and 'inventory.json' in errors[0]
  assert cell(project).status == 'fail'
  assert check(project).exit_code == 1
