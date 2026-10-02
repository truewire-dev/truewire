"""`truewire test [language]`: run each declared package's own suite against `truewire mock`."""
import typer

from truewire.project import PROJECT_FILE
from truewire.score import LANGUAGES, declared_languages
from truewire.test import Outcome, run_language, worst

from .common import PROJECT_OPTION, resolve_project


def run_tests(
  language: str | None = typer.Argument(
    None, help=f'One of {", ".join(LANGUAGES)}; every declared language when omitted.', show_default=False,
  ),
  project: str | None = PROJECT_OPTION,
):
  """Run each declared package's own test suite, the way its CI job does, and report per language.

  Each suite starts `truewire mock` over the project's recordings from its own harness;
  `TRUEWIRE_BIN` points every harness at this toolchain. The exit code is 1 when any
  language failed, and the last line names which (`docs/shape/toolchain.md` T4).

  Args:
    language: The one language to test; every declared language when omitted.
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
  """
  loaded = resolve_project(project)
  declared = declared_languages(loaded)
  if language is not None:
    if language not in LANGUAGES:
      typer.echo(f'unknown language {language!r}; expected one of {", ".join(LANGUAGES)}', err=True)
      raise typer.Exit(code=2)
    if language not in declared:
      typer.echo(f'{loaded.root / PROJECT_FILE}: no [{language}] section', err=True)
      raise typer.Exit(code=2)
  languages = (language,) if language is not None else declared
  if not languages:
    typer.echo(f'no language declared in {PROJECT_FILE}, so no suite to run', err=True)
    raise typer.Exit(code=1)

  outcomes: list[Outcome] = []
  for name in languages:
    outcomes.append(run_language(loaded, name, echo=lambda text: typer.echo(text, nl=False)))
    typer.echo('')

  width = max(len(outcome.language) for outcome in outcomes)
  for outcome in outcomes:
    status = 'pass' if outcome.passed else f'FAIL  {outcome.detail}'
    typer.echo(f'{outcome.language:<{width}}  {status}')
  failed = [outcome.language for outcome in outcomes if not outcome.passed]
  if failed:
    typer.echo(f'failed: {", ".join(failed)}')
  raise typer.Exit(code=worst(outcomes))
