"""
Run every mechanically-enforced standards check (`docs/production_standards.md`) for one
project in one pass.

Two kinds of check live here, side by side:

- **external** -- a real `truewire` subcommand (`check`, `docs check`, ...). Each already
  exists for a reason of its own beyond standards compliance -- `truewire check` is the
  primary tool for developing a spec, `docs check` type-checks documented code -- so this
  never replaces or changes what any of them mean; it only re-runs them and refuses to let
  one get forgotten.
- **native** -- a check with no `truewire` subcommand of its own, because it doesn't fit
  any existing command's domain: `truewire.standards.links`/`docstrings`/
  `duplicate_schemas`/`secrets`/`router_coverage`/`no_call` (S1/S3/S6/S16/S26/S29) each
  needed either a new target shape (project-wide instead of per-operation) or a wholly
  different kind of input (network requests, `.py` source, `truewire.toml`) that no existing
  command reads. Rather than growing a fresh top-level subcommand per narrow rule, each is
  an in-process `Path -> list[Finding]` function registered here directly.

`--only`/`--skip` (by `slug`) filter both kinds uniformly -- the point of unifying them
under one command. `links` (S1) is the one check excluded from a plain, unfiltered run: it
makes live HTTP requests to third-party API documentation hosts, and no policy for handling
a rate-limited or CAPTCHA'd host in an unattended run has been settled yet -- so it only
runs when named explicitly via `--only links`.

Does **not** cover a rule enforced by review or `manual` -- those need a human or an agent
actually reading code, which no aggregation of commands can substitute for. Green here is a
necessary precondition for calling a project production-ready, never a sufficient one.
"""
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing_extensions import Callable

import typer
from rich.console import Console
from rich.table import Table

from truewire.project import Project
from truewire.standards.docstrings import check_docstrings
from truewire.standards.duplicate_schemas import check_duplicate_schemas
from truewire.standards.finding import Finding
from truewire.standards.links import check_links
from truewire.standards.no_call import check_no_call_methods
from truewire.standards.router_coverage import check_router_coverage
from truewire.standards.secrets import check_secret_placeholders

from .common import PROJECT_OPTION, resolve_project

max_samples = 5
"""Offending locations shown per native check before the remainder collapses into a count."""


@dataclass(frozen=True)
class Check:
  """One `truewire` subcommand this aggregates."""
  label: str
  slug: str
  rules: str
  argv: list[str]
  """Argument list following `truewire` itself, e.g. `['check']` -- see `run_check`."""


@dataclass(frozen=True)
class NativeCheck:
  """One in-process check with no `truewire` subcommand of its own."""
  label: str
  slug: str
  rules: str
  fn: Callable[[Project], list[Finding]]
  default: bool = True
  """Whether this runs in a plain `truewire standards` call with no `--only`.
  `False` only for `links` (S1) -- see this module's own docstring."""


@dataclass(frozen=True)
class Result:
  """One check's outcome, independent of whether it ran as a subprocess or in-process."""
  ok: bool
  stdout: str
  stderr: str = ''
  command: str | None = None
  """Shell command actually run, shown in failed-check detail. `None` for a native check --
  there is no subprocess invocation to show."""


CHECKS: list[Check] = [
  Check('check', 'check', 'S5, S7, S8 (reserved-param), S14, S15', ['check']),
  Check('surface', 'surface', 'S8 (validate param)', ['surface']),
  Check('examples --require-verified', 'examples', 'S13', ['examples', '--require-verified']),
  Check('docs check', 'docs-check', 'S20', ['docs', 'check']),
  Check('docs lint', 'docs-lint', 'S21, S25', ['docs', 'lint']),
]
"""Per-project `truewire` subcommands, in `docs/production_standards.md` order."""

def _check_docstrings_from_project(project: Project) -> list[Finding]:
  """Adapt `check_docstrings` (which scans one package directory) to the `Project ->
  list[Finding]` shape every `NativeCheck.fn` shares, so S3 only ever sees the generated
  package -- never scripts or scratch files elsewhere under the project root."""
  return check_docstrings(project.package_dir)


def _check_no_call_methods_from_project(project: Project) -> list[Finding]:
  """Adapt `check_no_call_methods` (which scans one package directory) the same way as
  `_check_docstrings_from_project`, immediately above."""
  return check_no_call_methods(project.package_dir)


NATIVE_CHECKS: list[NativeCheck] = [
  NativeCheck('links', 'links', 'S1', check_links, default=False),
  NativeCheck('docstrings', 'docstrings', 'S3', _check_docstrings_from_project),
  NativeCheck('duplicate schemas', 'duplicate-schemas', 'S6', check_duplicate_schemas),
  NativeCheck('secret placeholders', 'secret-placeholders', 'S16', check_secret_placeholders),
  NativeCheck('router.json coverage', 'router-coverage', 'S26', check_router_coverage),
  NativeCheck('no __call__ methods', 'no-call', 'S29', _check_no_call_methods_from_project),
]
"""In-process checks with no `truewire` subcommand of their own, in
`docs/production_standards.md` order."""


def _truewire_script() -> Path:
  """
  Return the `truewire` console script installed alongside the running interpreter.

  Not `[sys.executable, '-m', 'truewire.cli']`: that package defines no `__main__.py`, so
  `-m` invocation isn't wired up. The console script `pyproject.toml` installs
  (`truewire = "truewire.cli:app"`) is what every human and every other command in this
  file's own docstring examples actually runs, so this re-invokes that, not a reimplemented
  entry point that could drift from it.
  """
  return Path(sys.executable).parent / 'truewire'


def run_check(check: Check, project: Project) -> Result:
  """
  Run one `Check` -- a `truewire` subcommand -- against a project and wrap its outcome.

  Args:
    check: Which check to run.
    project: The project to check.
  """
  argv = [str(_truewire_script()), *check.argv, '--project', str(project.root)]
  process = subprocess.run(argv, cwd=project.root, capture_output=True, text=True)
  return Result(
    ok=process.returncode == 0, stdout=process.stdout, stderr=process.stderr,
    command=' '.join(shlex.quote(part) for part in argv),
  )


def render_findings(findings: list[Finding]) -> str:
  """
  Render a native check's findings as plain text, sampled the same way
  `truewire.cli.test.report_authoring` samples spec-authoring violations.

  Args:
    findings: Every finding a native check reported, in the order it reported them.
  """
  if not findings:
    return 'OK (0 findings)\n'
  errors = sum(1 for f in findings if f['severity'] == 'error')
  warnings = sum(1 for f in findings if f['severity'] == 'warning')
  lines = [f'{errors} error(s), {warnings} warning(s)', '']
  shown = findings[:max_samples]
  for f in shown:
    lines.append(f'  [{f["severity"]}] {f["rule"]}  {f["location"]}: {f["message"]}')
  if len(findings) > len(shown):
    lines.append(f'  ... {len(findings) - len(shown)} more, rerun with --verbose')
  return '\n'.join(lines) + '\n'


def run_native_check(check: NativeCheck, root: Project) -> Result:
  """
  Run one `NativeCheck` in-process and wrap its outcome uniformly.

  Args:
    check: Which check to run.
    root: Project to check.
  """
  findings = check.fn(root)
  ok = not any(f['severity'] == 'error' for f in findings)
  return Result(ok=ok, stdout=render_findings(findings))


def _resolve_selection(
  only: str | None, skip: str | None,
) -> tuple[list[Check], list[NativeCheck]]:
  """
  Resolve `--only`/`--skip` against every registered check's `slug`.

  With neither flag, every `Check` runs plus every `NativeCheck` whose `default` is `True`
  -- `links` (S1) excluded, per this module's own docstring. `--only` selects exactly the
  named slugs, `default` included or not. `--skip` starts from the default set and removes
  the named slugs. The two are mutually exclusive; `--only` wins if both are given.

  Args:
    only: Comma-separated slugs to run, or `None`.
    skip: Comma-separated slugs to exclude from the default set, or `None`.

  Raises:
    typer.Exit: An unknown slug was named.
  """
  by_slug: dict[str, Check | NativeCheck] = {c.slug: c for c in [*CHECKS, *NATIVE_CHECKS]}

  def parse(raw: str) -> list[str]:
    slugs = [part.strip() for part in raw.split(',') if part.strip()]
    unknown = [slug for slug in slugs if slug not in by_slug]
    if unknown:
      typer.echo(
        f'Unknown check slug(s): {", ".join(unknown)}. Known slugs: '
        f'{", ".join(sorted(by_slug))}.', err=True,
      )
      raise typer.Exit(code=1)
    return slugs

  if only is not None:
    selected = set(parse(only))
  else:
    selected = {c.slug for c in CHECKS} | {c.slug for c in NATIVE_CHECKS if c.default}
    if skip is not None:
      selected -= set(parse(skip))

  return (
    [c for c in CHECKS if c.slug in selected],
    [c for c in NATIVE_CHECKS if c.slug in selected],
  )


def standards(
  project: str | None = PROJECT_OPTION,
  verbose: bool = typer.Option(False, '--verbose', '-v', help='Print full output for every check, not only failures.'),
  only: str | None = typer.Option(
    None, '--only', help='Comma-separated check slugs to run, skipping every other check. '
    'Pass --list-checks to see available slugs.',
  ),
  skip: str | None = typer.Option(
    None, '--skip', help='Comma-separated check slugs to exclude from the default run.',
  ),
  list_checks: bool = typer.Option(
    False, '--list-checks', help='Print every check slug and exit, without running anything.',
  ),
):
  """Run every mechanically-enforced `docs/production_standards.md` check for one project.

  Aggregates `check`, `surface`, `examples --require-verified`, `docs check`, `docs lint`,
  and six in-process
  checks with no subcommand of their own -- `links` (S1), `docstrings` (S3), `duplicate
  schemas` (S6), `secret placeholders` (S16), `router.json coverage` (S26), `no __call__
  methods` (S29) -- into one pass and one summary table. Every external check keeps its own
  real exit code and output; a native check's `ok` is whether it reported any
  `error`-severity finding -- `docstrings`/`duplicate schemas`/`secret placeholders` stay
  `warning`-only per each one's own docstring, the same staged rollout `ws-verb` used;
  `router.json coverage` and `no __call__ methods` are `error`-severity from the start.
  `links` is the one check excluded from a plain run -- see this module's own docstring --
  select it with `--only links`. Does **not** cover a review- or `manual`-enforced rule;
  those still need a human reading the code.

  Args:
    project: Project directory (holding `truewire.toml`); the nearest one by default.
    verbose: Print full output for every check, not only the ones that failed.
    only: Comma-separated check slugs to run, skipping every other check.
    skip: Comma-separated check slugs to exclude from the default run.
    list_checks: Print every check slug and exit, without running anything.
  """
  if list_checks:
    for check in CHECKS:
      typer.echo(f'{check.slug:<20} {check.label} ({check.rules}) [default: on]')
    for native in NATIVE_CHECKS:
      state = 'on' if native.default else 'off, pass --only to run'
      typer.echo(f'{native.slug:<20} {native.label} ({native.rules}) [default: {state}]')
    return

  loaded = resolve_project(project)
  client = loaded.name

  script = _truewire_script()
  if not script.is_file():
    typer.echo(f'No `truewire` console script found at {script}.', err=True)
    raise typer.Exit(code=1)

  checks, natives = _resolve_selection(only, skip)
  if not checks and not natives:
    typer.echo('No checks selected.', err=True)
    raise typer.Exit(code=1)

  typer.echo(f'Project: {client}\n')

  rows: list[tuple[str, str, str, Result]] = []
  for check in checks:
    rows.append((check.label, check.rules, check.slug, run_check(check, loaded)))
  for native in natives:
    rows.append((native.label, native.rules, native.slug, run_native_check(native, loaded)))

  table = Table(header_style='bold')
  table.add_column('check')
  table.add_column('rules')
  table.add_column('result')
  failed: list[tuple[str, Result]] = []
  for label, rules, _slug, result in rows:
    if not result.ok:
      failed.append((label, result))
    table.add_row(label, rules, '[green]OK[/]' if result.ok else '[red]FAILED[/]')
  Console().print(table)

  if verbose:
    for label, _rules, _slug, result in rows:
      typer.echo(f'\n--- {label} ---')
      typer.echo(result.stdout)
      if result.stderr:
        typer.echo(result.stderr, err=True)

  if failed:
    typer.echo(f'\n{len(failed)}/{len(rows)} check(s) failed:')
    for label, result in failed:
      typer.echo(f'\n=== {label} ===')
      if result.command:
        typer.echo(f'$ {result.command}')
      typer.echo(result.stdout)
      if result.stderr:
        typer.echo(result.stderr, err=True)
    typer.echo(
      f'\nSee `docs/production_standards.md` for the rules each check enforces; rerun the '
      f'failing command(s) directly for full output.'
    )
    raise typer.Exit(code=1)

  typer.echo('\nAll mechanically-enforced checks pass. Rules enforced only by '
             'review or `manual` still need that review.')
