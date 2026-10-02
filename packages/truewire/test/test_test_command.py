"""`truewire test [language]` (`docs/shape/toolchain.md` T4): each declared package's own suite,
reported per language, exit 1 naming the language that failed.

The end-to-end cases run a real pytest over a `truewire init` project, so the command is
measured the way a caller meets it; the other languages share every line but the command,
which `suite` pins.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.project import load_project
from truewire.test import Count, NoSuite, Outcome, Suite, package_directory, run, run_language, suite, count_tests, worst


GO = '\n[go]\npackage = "demo"\nsrc = "src"\nmodule = "example.com/demo"\n'


def init(root: Path, *, extra_toml: str = '') -> Path:
  """`truewire init` into `root / 'demo'`; returns the project directory."""
  assert CliRunner().invoke(app, ['init', 'demo', '--dir', str(root / 'demo')]).exit_code == 0
  project = root / 'demo'
  if extra_toml:
    (project / 'truewire.toml').write_text((project / 'truewire.toml').read_text() + extra_toml)
  return project


def write_test(project: Path, body: str) -> None:
  (project / 'test').mkdir(exist_ok=True)
  (project / 'test' / 'test_demo.py').write_text(f'def test_demo():\n  {body}\n')


class TestCommand:

  def test_a_passing_suite_exits_zero(self, tmp_path: Path):
    project = init(tmp_path)
    write_test(project, 'assert True')
    result = CliRunner().invoke(app, ['test', '--project', str(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith('== python: python -m pytest --verbosity=-1')
    assert result.output.splitlines()[-1].split() == ['python', 'pass']

  def test_a_failing_suite_exits_one_and_names_the_language(self, tmp_path: Path):
    project = init(tmp_path)
    write_test(project, 'assert False, "broken on purpose"')
    result = CliRunner().invoke(app, ['test', '--project', str(project)])
    assert result.exit_code == 1, result.output
    assert 'broken on purpose' in result.output
    assert 'python  FAIL  python -m pytest --verbosity=-1 exited 1' in result.output
    assert result.output.splitlines()[-1] == 'failed: python'

  def test_one_failing_language_fails_the_run_and_only_it_is_named(self, tmp_path: Path):
    """T4: the exit code is the worst of them. `[go]` has no `go.mod`, so it cannot run."""
    project = init(tmp_path, extra_toml='\n[go]\npackage = "demo"\nsrc = "src"\nmodule = "example.com/demo"\n')
    write_test(project, 'assert True')
    result = CliRunner().invoke(app, ['test', '--project', str(project)])
    assert result.exit_code == 1, result.output
    assert 'python  pass' in result.output
    assert 'go      FAIL  no go.mod between [go].src (src) and the project root' in result.output
    assert result.output.splitlines()[-1] == 'failed: go'

  def test_a_language_argument_runs_only_that_suite(self, tmp_path: Path):
    project = init(tmp_path, extra_toml='\n[go]\npackage = "demo"\nsrc = "src"\nmodule = "example.com/demo"\n')
    write_test(project, 'assert True')
    result = CliRunner().invoke(app, ['test', 'python', '--project', str(project)])
    assert result.exit_code == 0, result.output
    assert 'go' not in result.output.split()

  def test_a_package_with_no_tests_fails(self, tmp_path: Path):
    result = CliRunner().invoke(app, ['test', '--project', str(init(tmp_path))])
    assert result.exit_code == 1, result.output
    assert 'python  FAIL  python -m pytest --verbosity=-1 collected no tests' in result.output

  @pytest.mark.parametrize(('language', 'message'), [
    ('rust', 'no [rust] section'),
    ('cobol', "unknown language 'cobol'"),
  ])
  def test_a_language_that_is_not_declared_is_a_usage_error(self, tmp_path: Path, language: str, message: str):
    result = CliRunner().invoke(app, ['test', language, '--project', str(init(tmp_path))])
    assert result.exit_code == 2
    assert message in result.output


class TestSuite:

  def test_python_runs_pytest_with_src_on_the_path_and_this_toolchain_as_the_mock(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
  ):
    monkeypatch.setenv('TRUEWIRE_BIN', '/opt/truewire')
    project = load_project(init(tmp_path))
    found = suite(project, 'python')
    assert found.command == (sys.executable, '-m', 'pytest', '--verbosity=-1')
    assert found.directory == project.root.resolve()
    assert found.env['PYTHONPATH'].split(':')[0] == str(project.python_src.resolve())
    assert found.env['TRUEWIRE_BIN'] == '/opt/truewire'

  @pytest.mark.parametrize(('lockfile', 'command'), [
    ('yarn.lock', ('yarn', 'test')),
    ('pnpm-lock.yaml', ('pnpm', 'test')),
    (None, ('npm', 'test')),
  ])
  def test_typescript_runs_the_test_script_with_the_package_manager_its_lockfile_names(
    self, tmp_path: Path, lockfile: str | None, command: tuple[str, ...],
  ):
    root = init(tmp_path, extra_toml='\n[typescript]\npackage = "demo"\nsrc = "src"\n')
    (root / 'package.json').write_text('{}')
    (root / 'node_modules').mkdir()
    if lockfile:
      (root / lockfile).write_text('')
    assert suite(load_project(root), 'typescript').command == command

  def test_typescript_without_node_modules_says_to_install(self, tmp_path: Path):
    root = init(tmp_path, extra_toml='\n[typescript]\npackage = "demo"\nsrc = "src"\n')
    (root / 'package.json').write_text('{}')
    (root / 'yarn.lock').write_text('')
    with pytest.raises(NoSuite, match='run `yarn install` first'):
      suite(load_project(root), 'typescript')

  @pytest.mark.parametrize(('language', 'section', 'command'), [
    ('rust', '[rust]\npackage = "demo"\nsrc = "src"\n', ('cargo', 'test')),
    ('go', '[go]\npackage = "demo"\nsrc = "src"\nmodule = "example.com/demo"\n', ('go', 'test', '-v', './...')),
  ])
  def test_rust_and_go_run_their_own_test_command(self, tmp_path: Path, language: str, section: str, command: tuple[str, ...]):
    root = init(tmp_path, extra_toml='\n' + section)
    (root / {'rust': 'Cargo.toml', 'go': 'go.mod'}[language]).write_text('')
    assert suite(load_project(root), language).command == command


class TestPackageDirectory:

  def test_the_manifest_beside_src_at_the_root(self, tmp_path: Path):
    project = load_project(init(tmp_path))
    assert package_directory(project, 'python') == project.root.resolve()

  def test_the_manifest_under_packages_language(self, tmp_path: Path):
    """The W-layout: `packages/python/pyproject.toml` owns `packages/python/src`."""
    root = init(tmp_path)
    toml = root / 'truewire.toml'
    toml.write_text(toml.read_text().replace('src = "src"', 'src = "packages/python/src"'))
    (root / 'packages' / 'python' / 'src').mkdir(parents=True)
    (root / 'packages' / 'python' / 'pyproject.toml').write_text('')
    assert package_directory(load_project(root), 'python') == (root / 'packages' / 'python').resolve()

  def test_no_manifest_up_to_the_root_is_no_suite(self, tmp_path: Path):
    root = init(tmp_path, extra_toml=GO)
    with pytest.raises(NoSuite, match='no go.mod between'):
      package_directory(load_project(root), 'go')

  def test_python_without_a_pyproject_runs_from_the_root(self, tmp_path: Path):
    """pytest needs no manifest; `examples/kraken` has none and its CI runs pytest at the root."""
    root = init(tmp_path)
    (root / 'pyproject.toml').unlink()
    assert package_directory(load_project(root), 'python') == root.resolve()

  def test_kraken_python_suite_is_found_where_ci_runs_it(self):
    kraken = Path(__file__).resolve().parents[3] / 'examples' / 'kraken'
    assert suite(load_project(kraken), 'python').directory == kraken.resolve()


class TestRun:

  def test_the_exit_code_and_output_are_the_suites(self, tmp_path: Path):
    found = Suite('python', tmp_path, (sys.executable, '-c', 'print("ran"); raise SystemExit(3)'))
    lines: list[str] = []
    outcome = run(found, echo=lines.append)
    assert (outcome.code, outcome.output, ''.join(lines)) == (3, 'ran\n', 'ran\n')
    assert outcome.detail.endswith('exited 3')

  def test_a_tool_that_is_not_installed_fails_saying_so(self, tmp_path: Path):
    outcome = run(Suite('rust', tmp_path, ('no-such-cargo-here', 'test')))
    assert not outcome.passed
    assert outcome.detail == 'no-such-cargo-here not found on PATH'

  def test_a_suite_that_cannot_be_found_is_a_failed_outcome(self, tmp_path: Path):
    outcome = run_language(load_project(init(tmp_path, extra_toml=GO)), 'go')
    assert not outcome.passed and 'no go.mod' in outcome.detail

  def test_output_is_echoed_as_it_arrives_not_per_line(self, tmp_path: Path):
    """pytest -q prints its dots on one line; a per-line echo would show nothing until it ends."""
    script = 'import sys, time\nfor _ in range(3):\n  sys.stdout.write("."); sys.stdout.flush(); time.sleep(0.5)\nprint()\n'
    arrivals: list[float] = []
    start = time.monotonic()
    run(Suite('python', tmp_path, (sys.executable, '-c', script)), echo=lambda _: arrivals.append(time.monotonic() - start))
    assert arrivals and arrivals[0] < 1.0, arrivals

  def test_pytest_not_installed_says_so(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`pip install truewire` has no pytest (it is in `[dev]`)."""
    monkeypatch.setattr(sys, 'executable', '/usr/bin/python3')
    if subprocess.run(['/usr/bin/python3', '-c', 'import pytest'], capture_output=True).returncode == 0:
      pytest.skip('/usr/bin/python3 has pytest')
    outcome = run_language(load_project(init(tmp_path)), 'python')
    assert outcome.detail == 'pytest is not installed in /usr/bin/python3; install it there', outcome.detail

  def test_colour_forced_by_the_callers_env_or_the_package_still_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`PY_COLORS=1` beats `NO_COLOR` in pytest, and `addopts = --color=yes` beats both."""
    monkeypatch.setenv('PY_COLORS', '1')
    project = init(tmp_path)
    write_test(project, 'assert True')
    assert run_language(load_project(project), 'python').code == 0
    (package_directory(load_project(project), 'python') / 'pytest.ini').write_text('[pytest]\naddopts = --color=yes\n')
    outcome = run_language(load_project(project), 'python')
    assert outcome.code == 0, (outcome.detail, outcome.output)
    assert '\x1b[' in outcome.output


class TestNoTestsIsNotAPass:
  """S13: a suite that exits 0 having passed nothing (none found, or every one skipped) is not measured."""

  @pytest.mark.parametrize(('language', 'output', 'count'), [
    ('python', '..s\n2 passed, 1 skipped in 0.12s\n', Count(2, 1)),
    ('python', 'ss\n2 skipped in 0.01s\n', Count(0, 2)),
    ('python', '==== 1 failed, 13 passed, 2 warnings in 7.46s ====\n', Count(13, 0)),
    ('python', 'no summary here\n', None),
    ('python', '\x1b[32m\x1b[32m\x1b[1m1 passed\x1b[0m\x1b[32m in 0.00s\x1b[0m\x1b[0m\n', Count(1, 0)),
    ('rust', 'test result: \x1b[32mok\x1b[0m. 2 passed; 0 failed; 0 ignored\n', Count(2, 0)),
    ('typescript', ' Test Files  3 passed (3)\n      Tests  18 passed (18)\n', Count(18, 0)),
    ('typescript', ' Test Files  1 skipped (1)\n      Tests  3 skipped (3)\n', Count(0, 3)),
    ('typescript', 'Done in 1.2s.\n', None),
    ('rust', 'test result: ok. 0 passed; 0 failed; 0 ignored\ntest result: ok. 3 passed; 0 failed; 1 ignored\n', Count(3, 1)),
    ('rust', 'test result: ok. 0 passed; 0 failed; 2 ignored; 0 measured\n', Count(0, 2)),
    ('go', '=== RUN   TestA\n--- PASS: TestA (0.00s)\n    --- PASS: TestA/sub (0.00s)\nok  \tm/tests\n', Count(1, 0)),
    # `twtest.ReplayHTTP` skips each hand-written surface; the parent still says PASS.
    ('go', '--- PASS: TestReplay (0.00s)\n    --- SKIP: TestReplay/repos.get/ok (0.00s)\nPASS\n', Count(0, 1)),
    ('go', '--- PASS: A (0s)\n    --- PASS: A/b/c (0s)\n    --- PASS: A/b (0s)\n--- PASS: AB (0s)\n', Count(2, 0)),
    # A test that prints without a newline puts its `--- PASS:` mid-line.
    ('go', '=== RUN   TestA\nprogress...--- PASS: TestA (0.00s)\nPASS\n', Count(1, 0)),
    ('go', '--- SKIP: TestA (0.00s)\n?   \tm/src/demo\t[no test files]\n', Count(0, 1)),
  ])
  def test_the_count_comes_from_the_suites_own_summary(self, language: str, output: str, count: Count | None):
    assert count_tests(language, output) == count

  def test_a_python_suite_that_skipped_everything_fails_saying_so(self, tmp_path: Path):
    """pytest exits 0 when every test skipped, as `truewire.testing` does for an ungenerated method."""
    project = init(tmp_path)
    write_test(project, 'pytest.skip("method not generated yet")')
    (project / 'test' / 'test_demo.py').write_text('import pytest\n\n' + (project / 'test' / 'test_demo.py').read_text())
    outcome = run_language(load_project(project), 'python')
    assert outcome.detail == 'python -m pytest --verbosity=-1 ran no tests (1 skipped)', outcome.output

  @pytest.mark.parametrize('addopts', ['-q', '-qq', '-v'])
  def test_the_packages_own_verbosity_does_not_hide_the_summary(self, tmp_path: Path, addopts: str):
    """`-q` on top of `addopts = -q` is `-qq`, which prints no `N passed` line: the run sets it absolutely."""
    project = init(tmp_path)
    write_test(project, 'assert True')
    (project / 'pytest.ini').write_text(f'[pytest]\naddopts = {addopts}\n')
    outcome = run_language(load_project(project), 'python')
    assert outcome.code == 0, (outcome.detail, outcome.output)

  @pytest.mark.skipif(shutil.which('go') is None, reason='needs go')
  def test_a_go_package_with_no_tests_fails(self, tmp_path: Path):
    root = init(tmp_path, extra_toml=GO)
    (root / 'go.mod').write_text('module example.com/demo\n\ngo 1.21\n')
    (root / 'src' / 'demo' / 'demo.go').write_text('package demo\n\nfunc Unused() int { return 1 }\n')
    outcome = run_language(load_project(root), 'go')
    assert outcome.detail == 'go test -v ./... ran no tests', outcome.output

  @pytest.mark.skipif(shutil.which('cargo') is None, reason='needs cargo')
  def test_a_rust_crate_with_no_tests_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('CARGO_TARGET_DIR', str(tmp_path / 'target'))
    root = init(tmp_path, extra_toml='\n[rust]\npackage = "demo"\nsrc = "src"\n')
    (root / 'Cargo.toml').write_text('[package]\nname = "demo"\nversion = "0.0.0"\nedition = "2021"\n\n[lib]\npath = "src/lib.rs"\n')
    (root / 'src' / 'lib.rs').write_text('pub fn unused() {}\n')
    outcome = run_language(load_project(root), 'rust')
    assert outcome.detail == 'cargo test ran no tests', outcome.output

  def test_the_worst_outcome_decides_the_exit_code(self):
    assert worst([Outcome('python', 0), Outcome('go', 0)]) == 0
    assert worst([Outcome('python', 0), Outcome('rust', 101)]) == 1
