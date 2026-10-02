"""`truewire agents update` and `truewire agents check`: the skills and rules a project
vendors from the installed toolchain (`docs/shape/agents.md`, A10, A11)."""
import typer

from truewire import agents
from truewire.score.rows import declared_languages

from .common import PROJECT_OPTION, resolve_project

app = typer.Typer(help='The skills and rules vendored under .agents/: refresh them, or check them for drift.', no_args_is_help=True)


@app.command('update')
def update(
  project: str | None = PROJECT_OPTION,
  force: bool = typer.Option(False, '--force', help=f'Also overwrite copies carrying the line `{agents.LOCAL_MARKER}`.'),
):
  """Rewrite the vendored skills and rules from the installed toolchain.

  Writes `.agents/skills/README.md`, `.agents/skills/<name>/SKILL.md` for every skill the
  toolchain ships, and `.agents/rules/<language>.md` for every language `truewire.toml`
  declares that has rules, each stamped with the installed version. Removes a stamped copy
  the toolchain no longer ships, unless it was edited since. A copy marked as a local edit
  is kept unless `--force`; a file under `.agents/` the toolchain never wrote is not touched.
  Exits 1 when a copy's directory is a link leading outside the project, writing nothing there.
  """
  loaded = resolve_project(project)
  done = agents.update(loaded.root, declared_languages(loaded), force=force)
  toolchain = agents.installed_version()
  for relative in done.written:
    typer.echo(f'  updated  {relative}')
  for relative in done.overwrote:
    typer.echo(f'  overwrote {relative}  (edited since it was written; add the line `{agents.LOCAL_MARKER}` to keep an edit)')
  for relative in done.removed:
    typer.echo(f'  removed  {relative}  (no longer shipped)')
  for relative in done.kept:
    typer.echo(f'  kept     {relative}  (local edit; --force overwrites it)')
  for relative in done.orphaned:
    typer.echo(f'  kept     {relative}  (no longer shipped, but edited since it was written)')
  for relative in done.refused:
    typer.echo(f'  refused  {relative}  (its directory is a link leading outside the project)')
  if done.written or done.overwrote or done.removed:
    typer.echo(
      f'Vendored from truewire {toolchain}: {len(done.written) + len(done.overwrote)} file(s) written '
      f'({len(done.overwrote)} over an edit), {len(done.removed)} removed.'
    )
  elif not done.refused:
    typer.echo(f'Nothing changed: every vendored copy matches truewire {toolchain}.')
  if done.refused:
    raise typer.Exit(code=1)


@app.command('check')
def check(project: str | None = PROJECT_OPTION):
  """Fail when a vendored skill or rule differs from the installed toolchain's copy.

  Exits 1 on a copy that is missing, edited since it was written, written by a toolchain
  that shipped different text, or no longer shipped, naming each file. A copy carrying the line
  `<!-- truewire: local edit -->` is a deliberate edit and passes; so does one whose text
  matches and whose version line names an older toolchain.
  """
  loaded = resolve_project(project)
  copies = agents.survey(loaded.root, declared_languages(loaded))
  drifted = [copy for copy in copies if copy.drifted]
  for copy in copies:
    if copy.state == 'local':
      typer.echo(f'  local    {copy.path}  ({copy.detail})')
  for copy in drifted:
    typer.echo(f'  {copy.state:<8} {copy.path}  ({copy.detail})')
  toolchain = agents.installed_version()
  if drifted:
    typer.echo(
      f'{len(drifted)} vendored file(s) drifted from truewire {toolchain}. Run `truewire agents update`, '
      f'or keep a deliberate edit by adding the line `{agents.LOCAL_MARKER}` to the file.'
    )
    raise typer.Exit(code=1)
  typer.echo(f'OK: {len(copies)} vendored file(s) match truewire {toolchain}.')
