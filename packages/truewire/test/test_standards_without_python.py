"""`truewire standards`, and so the score's standards row, on a project with no [python].

The checks that scan the generated Python package (docstrings, paged shape, no `__call__`)
read `Project.package_dir`, which raises `NotAProject` without a `[python]` section. They
skip instead, and the other checks still decide the exit code.
"""
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app
from test_score import fixture_project, table_rows


def typescript_only(tmp_path: Path) -> Path:
  project = fixture_project(tmp_path)
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().split('\n[python]')[0] + '\n\n[typescript]\npackage = "forecasts"\n')
  return project


def test_standards_does_not_crash_on_a_project_without_python(tmp_path: Path):
  result = CliRunner().invoke(app, ['standards', '--project', str(typescript_only(tmp_path))])
  assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
  assert result.output.count('SKIPPED') == 3, result.output


def test_the_standards_row_does_not_report_a_crash_without_python(tmp_path: Path):
  project = typescript_only(tmp_path)
  line = table_rows(CliRunner().invoke(app, ['score', '--project', str(project)]).output)['standards']
  assert 'raised' not in line.split(), line
