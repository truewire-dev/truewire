"""`truewire score`: print a project's scorecard (`docs/shape/score.md`)."""
import typer

from truewire.score import Scorecard, declared_languages, header, measure, render_row, summary

from .common import PROJECT_OPTION, resolve_project


def score(
  project: str | None = PROJECT_OPTION,
  verbose: bool = typer.Option(
    False, '--verbose', '-v', help='After the table, print everything each failing checker printed.',
  ),
):
  """Print one row per clause of `docs/shape/score.md`, and exit zero only when finished.

  A row whose checker does not exist yet prints `unchecked`, never `pass`. The exit code is
  zero only when every row passes in every declared language and `stranger` carries a date.

  Args:
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    verbose: After the table, print the full output of every failing checker.
  """
  loaded = resolve_project(project)
  card = Scorecard(loaded.name, declared_languages(loaded))
  typer.echo(header(card))
  for row in measure(loaded):
    card.rows.append(row)
    typer.echo(render_row(row, card.languages))

  if verbose:
    for row in card.rows:
      for key, cell in row.cells.items():
        if cell.status == 'fail' and cell.output:
          typer.echo(f'\n--- {row.name}{f" ({key})" if key else ""} ---')
          typer.echo(cell.output.rstrip())
    typer.echo('')

  typer.echo(summary(card))
  if not card.done:
    raise typer.Exit(code=1)
