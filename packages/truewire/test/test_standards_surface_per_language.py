"""`truewire standards` runs `surface` for the languages the project declares.

`surface` takes `--language` and checks S8 for every backend. `standards` runs it once per
declared language, as `surface (<language>)`, so a project with no `[python]` is still
checked, and a Python-and-TypeScript project has its TypeScript surface checked too. A
project that declares no language shows the row as skipped.
"""
from pathlib import Path

from typer.testing import CliRunner, Result

from truewire.cli import app
from test_score import fixture_project
from test_standards_without_python import typescript_only


def run_surface(project: Path) -> Result:
  return CliRunner().invoke(app, ['standards', '--only', 'surface', '--project', str(project)])


def test_standards_checks_the_typescript_surface_of_a_typescript_project(tmp_path: Path):
  output = run_surface(typescript_only(tmp_path)).output
  assert 'nothing was checked' not in output, output
  assert 'surface (typescript)' in output, output
  assert 'surface (python)' not in output, output


def test_standards_checks_the_surface_of_every_declared_language(tmp_path: Path):
  project = fixture_project(tmp_path)
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text() + '\n[typescript]\npackage = "forecasts"\n')
  output = run_surface(project).output
  assert 'surface (python)' in output, output
  assert 'surface (typescript)' in output, output


def test_standards_skips_surface_on_a_project_that_declares_no_language(tmp_path: Path):
  project = fixture_project(tmp_path)
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().split('\n[python]')[0] + '\n')
  result = run_surface(project)
  assert 'SKIPPED' in result.output, result.output
  assert result.exit_code == 0, result.output
