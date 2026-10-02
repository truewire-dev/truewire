"""`truewire score` (`docs/shape/score.md`): one row per S1-S12, and no `pass` it did not measure.

The fixture is `truewire init` plus one endpoint with no recording: `check` passes on it,
`recorded` fails (no pair, no `unverified`), and `conform` has no checker, so one project shows
all three outcomes. A second, finished fixture (a recorded pair, generated code and the W11
pages) is where every row backed by a command must pass, so a wrapped call that is broken
for every project cannot hide behind a fixture that fails that row anyway. The rules that
matter most are S13 (an unmeasured row prints `unchecked`, never `pass`) and S15 (exit zero
only when every row passes and `stranger` carries a date).
"""
import inspect
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.project import NotAProject, load_project
from truewire.score import PROJECT, ROWS, Cell, Row, Scorecard, render, run_command, score, summary
from truewire.score import rows as score_rows
from truewire.skeleton import STUB_MARKER, skeleton_files

ROW_NAMES = [name for name, _ in ROWS]


def fixture_project(root: Path, *, extra_toml: str = '') -> Path:
  """`truewire init` plus one unrecorded endpoint; returns the project directory."""
  assert CliRunner().invoke(app, ['init', 'forecasts', '--dir', str(root / 'forecasts')]).exit_code == 0
  project = root / 'forecasts'
  if extra_toml:
    (project / 'truewire.toml').write_text((project / 'truewire.toml').read_text() + extra_toml)
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


def finish(project: Path) -> Path:
  """Give `project` what every row backed by a command needs to pass; returns it."""
  examples = project / 'spec' / 'endpoints' / 'weather' / 'forecast' / 'examples'
  examples.mkdir()
  (examples / 'paris.request.json').write_text(json.dumps({'parameters': {'latitude': 48.85}}))
  (examples / 'paris.response.json').write_text(json.dumps({'status': 200, 'payload': {'temperature': 21.5}}))
  assert CliRunner().invoke(app, ['generate', 'python', '--project', str(project)]).exit_code == 0
  for page in ('docs/index.md', 'docs/api-keys.md', 'docs/how-to/forecast.md', 'docs/reference/forecast.md'):
    (project / page).parent.mkdir(parents=True, exist_ok=True)
    (project / page).write_text('# Forecasts\n\nOne page of the W11 set.\n')
  (project / 'test').mkdir(exist_ok=True)
  (project / 'test' / 'test_forecasts.py').write_text(
    "import forecasts\n\n\ndef test_the_package_imports():\n  assert forecasts.__name__ == 'forecasts'\n"
  )
  return project


def table_rows(output: str) -> dict[str, str]:
  """The printed row lines of a scorecard, by row name."""
  found: dict[str, str] = {}
  for line in output.splitlines():
    words = line.split()
    if line.startswith('  ') and words and words[0] in ROW_NAMES:
      found[words[0]] = line
  return found


@pytest.fixture(scope='module')
def run(tmp_path_factory: pytest.TempPathFactory):
  project = fixture_project(tmp_path_factory.mktemp('score'))
  return CliRunner().invoke(app, ['score', '--project', str(project)]), project


class TestFixtureProject:

  def test_every_row_prints_once_in_order(self, run):
    result, _ = run
    assert list(table_rows(result.output)) == ROW_NAMES

  def test_the_header_names_the_project_and_its_one_declared_language(self, run):
    result, _ = run
    assert result.output.splitlines()[0].split() == ['forecasts', 'python']

  def test_check_passes(self, run):
    result, _ = run
    assert table_rows(result.output)['check'].split()[-1] == 'pass'

  def test_recorded_fails(self, run):
    result, _ = run
    assert 'FAIL' in table_rows(result.output)['recorded'].split()

  def test_tests_fails_on_a_package_with_no_suite(self, run):
    """S6 runs the suite; pytest collecting nothing is not a pass (S13)."""
    result, _ = run
    line = table_rows(result.output)['tests']
    assert 'FAIL' in line.split()
    assert line.endswith('collected no tests'), line

  def test_no_unchecked_row_prints_pass(self, run):
    result, _ = run
    for name, line in table_rows(result.output).items():
      if 'unchecked' in line.split():
        assert 'pass' not in line.split(), f'{name}: {line}'

  def test_rows_with_no_checker_are_unchecked(self, run):
    _, project = run
    statuses = {row.name: row.status for row in score(load_project(project)).rows}
    for name in ('conform', 'stranger'):
      assert statuses[name] == 'unchecked', name

  def test_coverage_fails_naming_the_missing_inventory(self, run):
    result, _ = run
    line = table_rows(result.output)['coverage']
    assert 'FAIL' in line.split()
    assert line.endswith('no spec/inventory.json')

  def test_docs_fails_naming_the_missing_pages(self, run):
    result, _ = run
    line = table_rows(result.output)['docs']
    assert 'FAIL' in line.split()
    assert 'docs/index.md' in line and 'docs/api-keys.md' in line

  def test_the_pages_init_wrote_are_stubs_and_count_as_missing(self, run):
    """`init` writes every W11 page as a stub; S9 must not pass on a skeleton nobody wrote."""
    _, project = run
    assert all((project / page).is_file() for page in ('docs/index.md', 'docs/api-keys.md'))
    assert score_rows.missing_pages(load_project(project)) == [
      'docs/index.md', 'docs/api-keys.md', 'docs/how-to/', 'docs/reference/',
    ]

  def test_the_last_line_is_the_summary_and_it_exits_non_zero(self, run):
    result, _ = run
    assert result.exit_code == 1
    last = result.output.splitlines()[-1]
    assert last.endswith('-> not done')
    passed, failed, unchecked = (int(part.split()[0]) for part in last.split(' -> ')[0].split(', '))
    assert passed + failed + unchecked == len(ROWS)
    assert failed >= 1 and passed >= 1 and unchecked >= 1

  def test_the_library_agrees_with_the_command(self, run):
    result, project = run
    card = score(load_project(project))
    assert render(card) == result.output.splitlines()


COMMAND_ROWS = ('recorded', 'check', 'generated', 'surface', 'lint', 'standards', 'docs')
"""The rows that run an existing `truewire` command and pass on the finished fixture."""


@pytest.fixture(scope='module')
def finished(tmp_path_factory: pytest.TempPathFactory) -> Scorecard:
  return score(load_project(finish(fixture_project(tmp_path_factory.mktemp('finished')))))


class TestFinishedProject:
  """Every row backed by a command passes on a project that has done what it asks."""

  @pytest.mark.parametrize('name', (*COMMAND_ROWS, 'tests'))
  def test_the_row_passes(self, finished: Scorecard, name: str):
    row = next(row for row in finished.rows if row.name == name)
    assert row.status == 'pass', {language: (cell.status, cell.detail) for language, cell in row.cells.items()}


@pytest.mark.parametrize('name', COMMAND_ROWS)
def test_every_wrapped_call_gives_its_command_every_parameter(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
):
  """A Typer default is a `typer.Option`, not a value, so an argument left out is a bug too."""
  calls: list[tuple[Callable[..., object], dict]] = []
  monkeypatch.setattr(score_rows, 'run_command', lambda command, **arguments: (
    calls.append((command, arguments)) or Cell('pass')
  ))
  measure = next(row for row in score_rows.MEASURES if row.__name__ == name)
  measure(load_project(fixture_project(tmp_path)))
  assert calls
  for command, arguments in calls:
    bound = inspect.signature(command).bind(**arguments)
    assert set(bound.arguments) == set(inspect.signature(command).parameters), command


ENDPOINT_ROWS = ('recorded', 'check', 'generated', 'surface', 'tests', 'standards')
"""The rows whose checker runs over the endpoint tree."""


@pytest.fixture(scope='module')
def empty(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """A fresh `truewire init demo`: no endpoint specs at all."""
  root = tmp_path_factory.mktemp('empty')
  assert CliRunner().invoke(app, ['init', 'demo', '--dir', str(root / 'demo')]).exit_code == 0
  return root / 'demo'


class TestEmptyProject:
  """A fresh `truewire init` has no endpoint: no row that needs one may pass (S13)."""

  def test_nothing_passes(self, empty: Path):
    result = CliRunner().invoke(app, ['score', '--project', str(empty)])
    assert result.exit_code == 1
    for name, line in table_rows(result.output).items():
      assert 'pass' not in line.split(), f'{name}: {line}'
    assert result.output.splitlines()[-1].startswith('0 pass, ')

  @pytest.mark.parametrize('name', ENDPOINT_ROWS)
  def test_an_endpoint_row_fails_saying_nothing_was_checked(self, empty: Path, name: str):
    row = next(row for row in score(load_project(empty)).rows if row.name == name)
    assert row.status == 'fail'
    assert {cell.detail for cell in row.cells.values()} == {score_rows.NO_ENDPOINTS}

  @pytest.mark.parametrize('name', ENDPOINT_ROWS)
  def test_an_endpoint_row_fails_even_when_its_command_would_pass(
    self, empty: Path, monkeypatch: pytest.MonkeyPatch, name: str,
  ):
    """The rule is the row's, not each command's: a command that passed over an empty tree
    (`generate --check` once did) still leaves the row failed."""
    monkeypatch.setattr(score_rows, 'run_command', lambda command, **arguments: Cell('pass'))
    monkeypatch.setattr(score_rows, 'run_language', lambda project, language: SimpleNamespace(
      passed=True, detail='', output='',
    ))
    measure = next(row for row in score_rows.MEASURES if row.__name__ == name)
    assert measure(load_project(empty)).status == 'fail'

  def test_a_per_language_row_fails_every_declared_language(self, empty: Path):
    row = score_rows.generated(load_project(empty))
    assert {language: cell.status for language, cell in row.cells.items()} == {'python': 'fail'}

  def test_with_no_language_declared_the_project_cell_fails(self, tmp_path: Path):
    root = tmp_path / 'demo'
    assert CliRunner().invoke(app, ['init', 'demo', '--dir', str(root)]).exit_code == 0
    toml = root / 'truewire.toml'
    toml.write_text(toml.read_text().split('\n[python]')[0] + '\n')
    row = score_rows.surface(load_project(root))
    assert (row.status, list(row.cells)) == ('fail', [PROJECT])


def test_every_docs_page_init_writes_carries_the_stub_marker_s9_reads():
  """The writer (`skeleton.py`) and the checker (`score/rows.py`) agree on what a stub is."""
  pages = {path: text for path, text in skeleton_files('Demo').items() if path.endswith('.md') and path.startswith('docs/')}
  assert set(pages) == {'docs/index.md', 'docs/api-keys.md', 'docs/how-to/index.md', 'docs/reference/index.md'}
  assert all(STUB_MARKER in text for text in pages.values())


def test_a_page_rewritten_without_the_marker_counts(tmp_path: Path):
  project = fixture_project(tmp_path)
  (project / 'docs' / 'index.md').write_text('# Forecasts\n\nWhat the API is.\n')
  (project / 'docs' / 'reference' / 'forecast.md').write_text('# Forecast\n')
  assert score_rows.missing_pages(load_project(project)) == ['docs/api-keys.md', 'docs/how-to/']


def test_the_cli_imports_without_click():
  """`truewire` does not depend on click (typer no longer does), so `score` must not import it."""
  code = "import sys; sys.modules['click'] = None; import truewire.cli"
  result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
  assert result.returncode == 0, result.stderr


def test_a_stranger_date_prints_instead_of_unchecked(tmp_path: Path):
  project = fixture_project(tmp_path, extra_toml='\n[score]\nstranger = 2026-10-12\n')
  result = CliRunner().invoke(app, ['score', '--project', str(project)])
  line = table_rows(result.output)['stranger']
  assert '2026-10-12' in line.split()
  assert 'unchecked' not in line.split()


def test_no_declared_language_prints_unchecked_rows(tmp_path: Path):
  project = fixture_project(tmp_path)
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().split('\n[python]')[0] + '\n')
  assert score_rows.declared_languages(load_project(project)) == ()
  rows = table_rows(CliRunner().invoke(app, ['score', '--project', str(project)]).output)
  for name in ('generated', 'surface', 'tests', 'lint', 'published'):
    assert 'unchecked' in rows[name].split(), rows[name]
    assert rows[name].endswith('no language declared in truewire.toml'), rows[name]


def test_verbose_prints_a_failing_checkers_output(tmp_path: Path):
  project = fixture_project(tmp_path)
  result = CliRunner().invoke(app, ['score', '--project', str(project), '--verbose'])
  assert '--- recorded ---' in result.output
  assert result.output.splitlines()[-1].endswith('-> not done')


def _every_row(status: str, stranger: Cell) -> list[Row]:
  return [
    Row(name, description, {PROJECT: stranger if name == 'stranger' else Cell(status)})  # type: ignore[arg-type]
    for name, description in ROWS
  ]


class TestDone:
  """S15, on hand-built cards: nothing short of every row passing and a date is `done`."""

  def test_every_row_passing_with_a_date_is_done(self):
    card = Scorecard('p', ('python',), _every_row('pass', Cell('date', shown='2026-10-12')))
    assert card.done
    assert summary(card) == '11 pass, 0 fail, 0 unchecked -> done'

  def test_no_stranger_date_is_not_done(self):
    card = Scorecard('p', ('python',), _every_row('pass', Cell('unchecked')))
    assert not card.done
    assert summary(card) == '11 pass, 0 fail, 1 unchecked -> not done'

  def test_one_unchecked_language_leaves_the_row_unchecked(self):
    row = Row('tests', 'the suite passes against the mock', {'python': Cell('pass'), 'rust': Cell('unchecked')})
    assert row.status == 'unchecked'

  def test_one_failing_language_fails_the_row(self):
    row = Row('tests', 'the suite passes against the mock', {'python': Cell('unchecked'), 'rust': Cell('fail')})
    assert row.status == 'fail'

  def test_a_missing_row_is_not_done(self):
    rows = _every_row('pass', Cell('date', shown='2026-10-12'))
    assert not Scorecard('p', ('python',), rows[1:]).done

  def test_the_command_exits_zero_only_when_done(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = fixture_project(tmp_path)
    rows = {row.name: row for row in _every_row('pass', Cell('date', shown='2026-10-12'))}
    monkeypatch.setattr(score_rows, 'MEASURES', tuple(
      (lambda _project, row=row: row) for row in rows.values()
    ))
    result = CliRunner().invoke(app, ['score', '--project', str(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == '11 pass, 0 fail, 0 unchecked -> done'


def test_standards_row_fails_when_the_standards_command_does(tmp_path: Path):
  """S8: `truewire standards` exits 1 on a broken docs link (its `docs lint` check), so the
  row must not print `pass`: S8 may skip what other rows run, but not `docs lint`."""
  project = fixture_project(tmp_path)
  for page in ('docs/index.md', 'docs/api-keys.md', 'docs/how-to/start.md', 'docs/reference/api.md'):
    (project / page).parent.mkdir(parents=True, exist_ok=True)
    (project / page).write_text('# Page\n\nSee [the guide](nowhere.md).\n')
  runner = CliRunner()
  assert runner.invoke(app, ['standards', '--project', str(project)]).exit_code == 1
  line = table_rows(runner.invoke(app, ['score', '--project', str(project)]).output)['standards']
  assert 'pass' not in line.split(), line


class TestRunCommand:
  """A row backed by a command passes exactly when that command would exit zero."""

  def test_returning_normally_passes(self):
    assert run_command(lambda: print('fine')).status == 'pass'

  def test_an_exit_with_a_code_fails_with_its_last_word(self):
    import typer

    def command():
      print('3 things checked')
      print('2 failed')
      raise typer.Exit(code=1)
    cell = run_command(command)
    assert (cell.status, cell.detail) == ('fail', '2 failed')

  def test_an_exit_with_code_zero_passes(self):
    import typer

    def command():
      raise typer.Exit()
    assert run_command(command).status == 'pass'

  def test_a_raising_command_fails_and_does_not_crash_the_card(self):
    def command():
      raise ValueError('the spec is not valid\nmore detail')
    cell = run_command(command)
    assert cell.status == 'fail'
    assert cell.detail == 'raised ValueError: the spec is not valid'

  def test_an_unbound_typer_option_fails_instead_of_running_truthy(self):
    """An omitted option keeps its `OptionInfo` default, which is truthy: without the guard
    a `--warn-only` flag would run in warn-only mode and pass where the CLI exits 1."""
    import typer

    def command(project: str | None = None, warn_only: bool = typer.Option(False, '--warn-only')):
      if not warn_only:
        raise typer.Exit(code=1)
    cell = run_command(command, project='.')
    assert (cell.status, cell.detail) == ('fail', 'score does not pass --warn-only')


class TestStrangerSetting:

  def test_a_toml_date(self, tmp_path: Path):
    project = fixture_project(tmp_path, extra_toml='\n[score]\nstranger = 2026-10-12\n')
    assert load_project(project).stranger == date(2026, 10, 12)

  def test_a_string_date(self, tmp_path: Path):
    project = fixture_project(tmp_path, extra_toml='\n[score]\nstranger = "2026-10-12"\n')
    assert load_project(project).stranger == date(2026, 10, 12)

  def test_absent_is_none(self, tmp_path: Path):
    assert load_project(fixture_project(tmp_path)).stranger is None

  @pytest.mark.parametrize('value', ['"soon"', '2026-10-12T10:00:00', '"2026-13-01"', '12'])
  def test_not_a_date_is_refused(self, tmp_path: Path, value: str):
    project = fixture_project(tmp_path, extra_toml=f'\n[score]\nstranger = {value}\n')
    with pytest.raises(NotAProject, match=r'\[score\]\.stranger'):
      load_project(project)

  def test_an_unknown_score_key_is_refused(self, tmp_path: Path):
    project = fixture_project(tmp_path, extra_toml='\n[score]\nstrangers = 2026-10-12\n')
    with pytest.raises(NotAProject, match='unknown \\[score\\] key'):
      load_project(project)
