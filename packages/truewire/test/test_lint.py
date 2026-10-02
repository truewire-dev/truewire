"""`truewire lint [language]` (`docs/shape/toolchain.md` T3, T4; `docs/shape/score.md` S7).

The Python cases run the real ruff and pyright over a one-file package. Everything else is
about what the command does *around* the tools: where it finds the package, how a missing
tool is reported (never as a pass), and how exit codes combine. None of that needs a
toolchain installed, so those tests fake the tools away rather than depend on one.
"""
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire import lint as lint_module
from truewire.cli import app
from truewire.lint import MISSING, Report, Step, lint_language, package_root, worst
from truewire.lint import run as lint_run
from truewire.project import load_project
from truewire.score import rows as score_rows

CLEAN_MODULE = '"""Demo."""\n\nVALUE = 1\n'


def python_project(root: Path, *, src: str = 'src', manifest_dir: str = '.') -> Path:
  """A project declaring only `[python]`, whose package is one clean module."""
  root.mkdir(parents=True, exist_ok=True)
  (root / 'truewire.toml').write_text(
    '[project]\nname = "demo"\n\n[cores.default]\nmeta = { type = "object" }\n\n'
    f'[python]\npackage = "demo"\nsrc = "{src}"\n\n[python.cores.default]\nbase = "demo.core:Endpoint"\n'
  )
  (root / manifest_dir).mkdir(parents=True, exist_ok=True)
  (root / manifest_dir / 'pyproject.toml').write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
  package = root / src / 'demo'
  package.mkdir(parents=True)
  (package / '__init__.py').write_text(CLEAN_MODULE)
  return root


def typescript_project(root: Path) -> Path:
  """A project declaring only `[typescript]`, with a manifest and a tsconfig but no install."""
  root.mkdir(parents=True)
  (root / 'truewire.toml').write_text(
    '[project]\nname = "demo"\n\n[cores.default]\nmeta = { type = "object" }\n\n'
    '[typescript]\npackage = "demo"\nsrc = "src"\n'
  )
  (root / 'package.json').write_text('{"name": "demo", "version": "0.1.0"}\n')
  (root / 'tsconfig.json').write_text('{"include": ["src"]}\n')
  (root / 'src' / 'demo').mkdir(parents=True)
  (root / 'src' / 'demo' / 'index.ts').write_text('export const value = 1;\n')
  return root


def invoke(*args: str):
  return CliRunner().invoke(app, ['lint', *args])


requires_python_tools = pytest.mark.skipif(
  lint_run._python_tool('pyright') is None, reason='pyright is a dev extra; not installed here',
)


@requires_python_tools
class TestPython:

  def test_a_clean_package_passes_every_tool(self, tmp_path: Path):
    result = invoke('python', '--project', str(python_project(tmp_path / 'demo')))
    assert result.exit_code == 0, result.output
    tools = [line.split()[:3] for line in result.output.splitlines()[:-1] if not line.startswith('  |')]
    assert tools == [['python', 'ruff', 'format'], ['python', 'ruff', 'pass'], ['python', 'pyright', 'pass']]
    assert result.output.splitlines()[-1] == 'lint passed: python'

  def test_an_unused_import_fails_and_names_ruff(self, tmp_path: Path):
    project = python_project(tmp_path / 'demo')
    (project / 'src' / 'demo' / '__init__.py').write_text('"""Demo."""\n\nimport os\n\nVALUE = 1\n')
    result = invoke('python', '--project', str(project))
    assert result.exit_code == 1, result.output
    assert 'F401' in result.output
    assert result.output.splitlines()[-1] == 'lint failed: python (ruff exit 1)'

  def test_without_a_language_every_declared_one_is_linted(self, tmp_path: Path):
    result = invoke('--project', str(python_project(tmp_path / 'demo')))
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == 'lint passed: python'

  def test_the_packages_own_ruff_config_is_the_one_read(self, tmp_path: Path):
    """A package with `[tool.ruff]` is linted by it, not by the config truewire ships."""
    project = python_project(tmp_path / 'demo')
    (project / 'src' / 'demo' / '__init__.py').write_text('"""Demo."""\n\nimport os\n\nVALUE = 1\n')
    with (project / 'pyproject.toml').open('a') as pyproject:
      pyproject.write('\n[tool.ruff]\nindent-width = 2\n\n[tool.ruff.lint]\nselect = ["E9"]\n\n[tool.ruff.format]\nquote-style = "double"\n')
    result = invoke('python', '--project', str(project))
    assert result.exit_code == 0, result.output

  def test_a_package_in_its_own_directory_is_found_above_src(self, tmp_path: Path):
    project = python_project(tmp_path / 'demo', src='packages/python/src', manifest_dir='packages/python')
    report = lint_language(load_project(project), 'python')
    assert report.package == (project / 'packages' / 'python').resolve()
    assert report.code == 0, [(step.tool, step.output) for step in report.steps]

  def test_the_score_row_passes_and_fails_with_the_command(self, tmp_path: Path):
    project = python_project(tmp_path / 'demo')
    assert score_rows.lint(load_project(project)).cells['python'].status == 'pass'
    (project / 'src' / 'demo' / '__init__.py').write_text('"""Demo."""\n\nimport os\n\nVALUE = 1\n')
    cell = score_rows.lint(load_project(project)).cells['python']
    assert (cell.status, cell.detail) == ('fail', 'lint failed: python (ruff exit 1)')


class TestMissingTools:

  def test_a_missing_tool_fails_the_language_and_is_named(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lint_run, '_python_tool', lambda module: None)
    monkeypatch.setattr(lint_run, '_node_bin', lambda package, name: None)
    result = invoke('python', '--project', str(python_project(tmp_path / 'demo')))
    assert result.exit_code == MISSING
    assert 'pyright      MISSING' in result.output
    assert result.output.splitlines()[-1] == (
      f'lint failed: python (ruff format exit {MISSING}, ruff exit {MISSING}, pyright exit {MISSING})'
    )

  def test_typescript_without_node_modules_fails_on_tsc_and_says_eslint_was_skipped(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
  ):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    result = invoke('typescript', '--project', str(typescript_project(tmp_path / 'demo')))
    assert result.exit_code == MISSING, result.output
    lines = result.output.splitlines()
    assert lines[0].split()[:3] == ['typescript', 'tsc', 'MISSING']
    assert lines[1] == 'typescript  eslint       skipped  the package has no eslint config'
    assert lines[-1] == f'lint failed: typescript (tsc exit {MISSING})'

  def test_a_configured_eslint_that_is_not_installed_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    project = typescript_project(tmp_path / 'demo')
    (project / 'eslint.config.js').write_text('export default [];\n')
    report = lint_language(load_project(project), 'typescript')
    assert [(step.tool, step.status) for step in report.steps] == [('tsc', 'missing'), ('eslint', 'missing')]


class TestPackage:

  def test_no_manifest_fails_naming_the_file(self, tmp_path: Path):
    project = typescript_project(tmp_path / 'demo')
    (project / 'package.json').unlink()
    report = lint_language(load_project(project), 'typescript')
    assert report.package is None
    [step] = report.steps
    assert (step.tool, step.status, step.code) == ('package', 'fail', 1)
    assert step.detail == 'no package.json between [typescript].src (src) and the project root'

  def test_the_search_stops_at_the_project_root(self, tmp_path: Path):
    (tmp_path / 'package.json').write_text('{"name": "outer"}\n')
    project = typescript_project(tmp_path / 'demo')
    (project / 'package.json').unlink()
    assert package_root(load_project(project), 'typescript') is None

  def test_a_python_package_without_a_manifest_is_the_project_root(self, tmp_path: Path):
    """As for `truewire test`: `examples/kraken` has no `pyproject.toml`, and both commands
    run from its root."""
    project = python_project(tmp_path / 'demo')
    (project / 'pyproject.toml').unlink()
    assert package_root(load_project(project), 'python') == project.resolve()


class TestCommand:

  def test_an_undeclared_language_is_refused(self, tmp_path: Path):
    result = invoke('rust', '--project', str(python_project(tmp_path / 'demo')))
    assert result.exit_code == 2
    assert 'declares no [rust] section' in result.output

  def test_an_unknown_language_is_refused(self, tmp_path: Path):
    result = invoke('cobol', '--project', str(python_project(tmp_path / 'demo')))
    assert result.exit_code == 2
    assert "no such language: 'cobol'" in result.output

  def test_a_project_declaring_no_language_lints_nothing_and_fails(self, tmp_path: Path):
    (tmp_path / 'truewire.toml').write_text('[project]\nname = "demo"\n\n[cores.default]\nmeta = { type = "object" }\n')
    result = invoke('--project', str(tmp_path))
    assert result.exit_code == 1
    assert 'declares no language' in result.output


class TestExitCodes:

  def test_the_worst_code_wins(self):
    reports = [
      Report('python', steps=[Step('python', 'ruff', 'fail', 1)]),
      Report('rust', steps=[Step('rust', 'clippy', 'fail', 101), Step('rust', 'rustfmt', 'pass')]),
      Report('go', steps=[Step('go', 'gofmt', 'pass')]),
    ]
    assert [report.code for report in reports] == [1, 101, 0]
    assert worst(reports) == 101

  def test_a_skipped_step_passes(self):
    assert Report('typescript', steps=[Step('typescript', 'eslint', 'skipped')]).code == 0

  def test_output_where_none_is_wanted_fails(self, tmp_path: Path):
    """`gofmt -l` exits zero and lists the files it would reformat."""
    step = lint_run._run('go', 'gofmt', (sys.executable, '-c', 'print("client.go")'), tmp_path, fail_on_output=True)
    assert (step.status, step.code) == ('fail', 1)

  def test_a_command_that_cannot_start_is_missing(self, tmp_path: Path):
    step = lint_run._run('go', 'go vet', (str(tmp_path / 'no-such-go'), 'vet'), tmp_path)
    assert (step.status, step.code) == ('missing', MISSING)


def test_the_library_is_importable_without_the_cli():
  """T5: the behaviour lives in `truewire.lint`; `truewire.cli.lint` only prints it."""
  assert lint_module.lint is lint_run.lint
  assert 'typer' not in Path(lint_run.__file__).read_text()
