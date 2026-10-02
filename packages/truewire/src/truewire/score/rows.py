"""Measure each of S1-S12 for one project.

A row backed by an existing command runs that command's own function in-process, with its
output captured, and passes exactly when the command would exit zero. Nothing is
reimplemented here, so a row cannot come to disagree with the command a person would run
to see why it failed. A command that raises instead of exiting is a `fail` carrying the
exception, not a crash of the whole card.

A row whose checker does not exist yet is `unchecked` and says which one is missing (S13).
Nothing here can produce `pass` without a checker having run.

A row whose checker runs over the endpoint tree fails, without running it, on a project with
no endpoint specs: it would have examined nothing, and `check` refuses that as a success. The
rule lives here, once, as a backstop: every command now refuses an empty tree itself, but a
command that ever passed over one (`generate --check` once did) cannot turn its row green.
"""
import functools
import inspect
import io
from collections.abc import Callable, Iterator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing_extensions import Any

import httpx
import typer
from typer.models import ParameterInfo

from truewire.project import PROJECT_FILE, Project
from truewire.skeleton import STUB_MARKER
from truewire.spec import endpoint_specs
from truewire.spec.inventory import load_inventory
from truewire.test import run_language

from .card import PROJECT, ROWS, Cell, Row, Scorecard
from .published import check_package

LANGUAGES = ('python', 'typescript', 'rust', 'go')
"""Every language a project can declare, in the order the scorecard prints its columns."""

REQUIRED_PAGES = ('docs/index.md', 'docs/api-keys.md')
"""The W11 pages S9 requires by name; `docs/how-to/` and `docs/reference/` each need one page."""

REQUIRED_SECTIONS = ('docs/how-to', 'docs/reference')

NO_ENDPOINTS = 'no endpoint specs, so nothing was checked'
"""Why a row over the endpoint tree fails on a project that has none."""


def declared_languages(project: Project) -> tuple[str, ...]:
  """The languages `truewire.toml` declares a section for, in `LANGUAGES` order."""
  return tuple(language for language in LANGUAGES if getattr(project.config, language) is not None)


def _last_word(stdout: str, stderr: str) -> str:
  """A failed command's reason: the first line it wrote to stderr, else its last to stdout."""
  for text, pick in ((stderr, 0), (stdout, -1)):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
      return lines[pick]
  return 'failed with no output'


def run_command(command: Callable[..., Any], **arguments: Any) -> Cell:
  """Run one `truewire` command function in-process; `pass` exactly when it exits zero.

  Every argument must be given: a Typer command called directly has `typer.Option`
  objects, not values, as its defaults, and those are truthy. A parameter left at one is
  a `fail`, so a new flag on the command cannot silently change what the row measures.
  """
  unbound = [
    name for name, parameter in inspect.signature(command).parameters.items()
    if name not in arguments and isinstance(parameter.default, ParameterInfo)
  ]
  if unbound:
    return Cell('fail', 'score does not pass ' + ', '.join(f'--{name.replace("_", "-")}' for name in unbound))
  stdout, stderr = io.StringIO(), io.StringIO()
  try:
    with redirect_stdout(stdout), redirect_stderr(stderr):
      command(**arguments)
    code: object = 0
  except typer.Exit as exc:
    code = exc.exit_code
  except SystemExit as exc:
    code = exc.code
  except Exception as exc:  # noqa: BLE001 - a checker that raises has failed, not passed
    lines = str(exc).strip().splitlines()
    reason = f'{type(exc).__name__}: {lines[0]}' if lines else type(exc).__name__
    output = stdout.getvalue() + stderr.getvalue() + f'{type(exc).__name__}: {exc}\n'
    return Cell('fail', f'raised {reason}', output=output)
  output = stdout.getvalue() + stderr.getvalue()
  if code in (0, None):
    return Cell('pass', output=output)
  return Cell('fail', _last_word(stdout.getvalue(), stderr.getvalue()), output=output)


def _unchecked(reason: str) -> Cell:
  return Cell('unchecked', reason)


def _row(name: str, cells: dict[str, Cell]) -> Row:
  return Row(name=name, description=dict(ROWS)[name], cells=cells)


def _per_language(project: Project, cell: Callable[[str], Cell]) -> dict[str, Cell]:
  """A cell per declared language; one unchecked project cell when none is declared."""
  languages = declared_languages(project)
  if not languages:
    return {PROJECT: _unchecked(f'no language declared in {PROJECT_FILE}')}
  return {language: cell(language) for language in languages}


def over_endpoints(*, per_language: bool) -> Callable[[Callable[[Project], Row]], Callable[[Project], Row]]:
  """Fail the decorated row on a project with no endpoint specs, instead of measuring it.

  Every cell the row would have fails with `NO_ENDPOINTS`: one per declared language when
  `per_language`, else the one project cell. A per-language row with no language declared
  fails its project cell too; there is nothing to generate or reach either way.
  """
  def decorate(measure: Callable[[Project], Row]) -> Callable[[Project], Row]:
    @functools.wraps(measure)
    def guarded(project: Project) -> Row:
      if endpoint_specs(project):
        return measure(project)
      keys = (declared_languages(project) or (PROJECT,)) if per_language else (PROJECT,)
      return _row(measure.__name__, {key: Cell('fail', NO_ENDPOINTS) for key in keys})
    return guarded
  return decorate


def coverage(project: Project) -> Row:
  """S1: account for every entry in the human-approved upstream inventory."""
  inventory, errors = load_inventory(project.spec_dir)
  if inventory is None and not errors:
    return _row('coverage', {PROJECT: Cell('fail', 'no spec/inventory.json')})
  specified = {
    '.'.join(path.parent.relative_to(project.spec_dir / 'endpoints').parts)
    for path in endpoint_specs(project)
  }
  listed: set[str] = set()
  entries = []
  resolved = 0
  unspecified = []
  if isinstance(inventory, dict):
    if inventory.get('approved') is None:
      errors.append('inventory not approved')
    if isinstance(inventory.get('endpoints'), list):
      entries = inventory['endpoints']
  for index, entry in enumerate(entries):
    if not isinstance(entry, dict):
      continue  # The shape validator already names this entry.
    endpoint = entry.get('endpoint')
    if isinstance(endpoint, str):
      listed.add(endpoint)
      if endpoint not in specified:
        errors.append(f'endpoints[{index}] {entry.get("method", "")} {entry.get("path", "")}: '
                      f'endpoint {endpoint!r} is not in the spec')
      elif 'excluded' not in entry:
        resolved += 1
    elif 'endpoint' not in entry:
      if 'excluded' not in entry:
        unspecified.append(f'endpoints[{index}] {entry.get("method", "")} {entry.get("path", "")}: unspecified')
      elif isinstance(entry['excluded'], str) and entry['excluded'].strip():
        resolved += 1
  details = [f'{resolved}/{len(entries)} endpoints']
  if unspecified:
    details.append(f'{len(unspecified)} unspecified')
  details.extend(errors)
  unlisted = sorted(specified - listed)
  if unlisted:
    details = [*details, f'warning: spec endpoints missing from inventory: {", ".join(unlisted)}']
  return _row('coverage', {PROJECT: Cell(
    'fail' if errors or unspecified else 'pass', '; '.join(details),
    output='\n'.join([*details, *unspecified]) + '\n',
  )})


@over_endpoints(per_language=False)
def recorded(project: Project) -> Row:
  """S2: `truewire examples --require-verified`."""
  from truewire.cli.examples import examples
  return _row('recorded', {PROJECT: run_command(
    examples, project=str(project.root), path=None, verbose=False, require_verified=True,
  )})


@over_endpoints(per_language=False)
def check(project: Project) -> Row:
  """S3: `truewire check`."""
  from truewire.cli.check import check as check_command
  return _row('check', {PROJECT: run_command(
    check_command, project=str(project.root), path=None, verbose=False,
  )})


@over_endpoints(per_language=True)
def generated(project: Project) -> Row:
  """S4: `truewire generate <language> --check`, per declared language."""
  from truewire.cli.generate import generate
  return _row('generated', _per_language(project, lambda language: run_command(
    generate, language=language, project=str(project.root), verbose=0, delete=False, check=True,
  )))


@over_endpoints(per_language=True)
def surface(project: Project) -> Row:
  """S5: `truewire surface --language <language>`, per declared language."""
  from truewire.cli.surface import surface as surface_command
  return _row('surface', _per_language(project, lambda language: run_command(
    surface_command, project=str(project.root), path=None, verbose=False, language=language,
  )))


@over_endpoints(per_language=True)
def tests(project: Project) -> Row:
  """S6: `truewire test <language>`, per declared language."""
  def cell(language: str) -> Cell:
    outcome = run_language(project, language)
    if outcome.passed:
      return Cell('pass', output=outcome.output)
    return Cell('fail', outcome.detail, output=outcome.output)

  return _row('tests', _per_language(project, cell))


def lint(project: Project) -> Row:
  """S7: `truewire lint <language>`, per declared language."""
  from truewire.cli.lint import lint as lint_command
  return _row('lint', _per_language(project, lambda language: run_command(
    lint_command, language=language, project=str(project.root), verbose=False,
  )))


@over_endpoints(per_language=False)
def standards(project: Project) -> Row:
  """S8: `truewire standards`, minus the checks other rows already run.

  `check` (S3), `surface` (S5), `examples` (S2) and `docs check` (S9) are skipped: they
  are rows of their own here, and running them twice would only make the card slower.
  Every other default check runs, `docs lint` and any check added later included, so
  this row fails whenever `truewire standards` does.
  """
  from truewire.cli.standards import standards as standards_command
  return _row('standards', {PROJECT: run_command(
    standards_command, project=str(project.root), verbose=False, only=None,
    skip='check,surface,examples,docs-check', list_checks=False,
  )})


def is_written(page: Path) -> bool:
  """Whether `page` is a page someone wrote: it exists and no longer carries the stub marker
  `truewire init` puts in the pages it lays down."""
  return page.is_file() and STUB_MARKER not in page.read_text(errors='replace')


def missing_pages(project: Project) -> list[str]:
  """The W11 pages S9 requires that `project` lacks, as paths relative to its root. A page
  that is still `truewire init`'s stub counts as missing."""
  missing = [page for page in REQUIRED_PAGES if not is_written(project.root / page)]
  for section in REQUIRED_SECTIONS:
    directory = project.root / section
    if not directory.is_dir() or not any(is_written(page) for page in directory.rglob('*.md')):
      missing.append(f'{section}/')
  return missing


def docs(project: Project) -> Row:
  """S9: `truewire docs check`, and the W11 pages present."""
  from truewire.cli.docs import check as docs_check
  checked = run_command(docs_check, project=str(project.root), path=None)
  missing = missing_pages(project)
  if not missing:
    return _row('docs', {PROJECT: checked})
  reason = f'missing or still a stub: {", ".join(missing)}'
  if checked.status != 'pass':
    reason += f'; docs check: {checked.detail}'
  return _row('docs', {PROJECT: Cell('fail', reason, output=checked.output)})


def published(project: Project, *, transport: httpx.BaseTransport | None = None) -> Row:
  """S10: the exact version in each package manifest exists on its registry."""
  return _row('published', _per_language(project, lambda language: check_package(project, language, transport=transport)))


def conform(project: Project) -> Row:
  """S11: needs the ADR 0012 conformance report, which no run writes yet."""
  return _row('conform', {PROJECT: _unchecked('no conformance report yet (ADR 0012)')})


def stranger(project: Project) -> Row:
  """S12: the date in `[score].stranger`, or `unchecked` without one."""
  if project.stranger is None:
    cell = _unchecked(f'no [score].stranger date in {PROJECT_FILE}')
  else:
    cell = Cell('date', shown=project.stranger.isoformat())
  return _row('stranger', {PROJECT: cell})


MEASURES: tuple[Callable[[Project], Row], ...] = (
  coverage, recorded, check, generated, surface, tests, lint, standards, docs, published,
  conform, stranger,
)
"""One function per row, in `ROWS` order."""


def measure(project: Project) -> Iterator[Row]:
  """Measure every row in order, yielding each as it is done, so a caller can print as it goes."""
  for row in MEASURES:
    yield row(project)


def score(project: Project) -> Scorecard:
  """Measure every row of `project`'s scorecard."""
  return Scorecard(project.name, declared_languages(project), list(measure(project)))
