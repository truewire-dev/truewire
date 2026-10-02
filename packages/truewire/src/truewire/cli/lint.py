"""`truewire lint [language]`: each declared package's formatter, linter and type checker."""
import typer

from truewire.lint import LANGUAGES, Report, Step, UnknownLanguage, declared_languages, steps, worst
from truewire.project import PROJECT_FILE

from .common import PROJECT_OPTION, resolve_project

_SHOWN = {'pass': 'pass', 'fail': 'FAIL', 'missing': 'MISSING', 'skipped': 'skipped'}


def _echo_step(step: Step, *, verbose: bool) -> None:
  line = f'{step.language:<11} {step.tool:<12} {_SHOWN[step.status]}'
  if step.status == 'fail' and step.command:
    line += f'  exit {step.code}'
  if step.detail:
    line += f'  {step.detail}'
  typer.echo(line)
  if step.output.strip() and (verbose or step.status == 'fail'):
    for text in step.output.rstrip().splitlines():
      typer.echo(f'  | {text}')


def _summary(reports: list[Report]) -> str:
  failed = [report for report in reports if report.code]
  if not failed:
    return 'lint passed: ' + ', '.join(report.language for report in reports)
  return 'lint failed: ' + '; '.join(
    f'{report.language} ({", ".join(f"{step.tool} exit {step.code}" for step in report.failed)})'
    for report in failed
  )


def lint(
  language: str | None = typer.Argument(
    None, help=f'One of {", ".join(LANGUAGES)}; every language truewire.toml declares when omitted.',
  ),
  project: str | None = PROJECT_OPTION,
  verbose: bool = typer.Option(False, '--verbose', '-v', help='Print every tool\'s output, not only a failing one\'s.'),
):
  """Run each declared package's own formatter, linter and type checker.

  Python: `ruff format --check`, `ruff check`, `pyright`. TypeScript: `tsc --noEmit`, and
  `eslint` when the package configures it. Rust: `cargo fmt --check`, `cargo clippy
  --all-targets -- -D warnings`. Go: `gofmt -l`, `go vet`, `staticcheck`. Each runs in the
  package (the directory holding its manifest) and reads that package's config.

  A tool that cannot be found fails its language. The exit code is the worst of any tool's,
  and the last line names each language that failed and the tools that failed it.

  Args:
    language: The one language to lint; every declared one when omitted.
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    verbose: Print every tool's output, not only a failing one's.
  """
  loaded = resolve_project(project)
  languages = (language,) if language is not None else declared_languages(loaded)
  if not languages:
    typer.echo(f'{loaded.root / PROJECT_FILE} declares no language, so nothing was linted', err=True)
    raise typer.Exit(code=1)

  reports: list[Report] = []
  try:
    for name in languages:
      report = Report(name)
      for step in steps(loaded, name):
        report.steps.append(step)
        _echo_step(step, verbose=verbose)
      reports.append(report)
  except UnknownLanguage as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=2)

  typer.echo(_summary(reports))
  code = worst(reports)
  if code:
    raise typer.Exit(code=code)
