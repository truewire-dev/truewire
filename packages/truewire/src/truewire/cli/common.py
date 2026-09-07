"""Shared CLI plumbing: resolving the project a command runs against.

Every command takes `--project PATH` (a directory holding `truewire.toml`, or the file
itself). Without it, the nearest `truewire.toml` at or above the working directory is used,
the way `cargo` finds `Cargo.toml`. Spec commands additionally take `--path`, a project root
or one subdivision of its `<spec>/endpoints` tree, to scope a run.
"""
from pathlib import Path

import typer

from truewire.project import NotAProject, Project, find_project, load_project, resolve
from truewire.spec import NoEndpointSpecs, SpecScope, resolve_scope

PROJECT_OPTION = typer.Option(
  None, '--project', '-p',
  help='Project directory (holding truewire.toml). Defaults to the nearest one above the '
  'working directory.',
)
PATH_OPTION = typer.Option(
  None, '--path',
  help=(
    'Project root, or one subdivision under its `spec/endpoints`. A subdivision scopes '
    'the run to that subtree; a path with no `endpoints` tree above or below it fails.'
  ),
)


def resolve_project(project: str | None) -> Project:
  """Load the project `--project` names, or find the nearest one; exit with the reason otherwise."""
  try:
    if project is not None:
      return load_project(Path(project))
    return find_project()
  except NotAProject as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)


def resolve_spec_scope(project: str | None, path: str | None) -> tuple[Project, SpecScope]:
  """Resolve the project and the spec scope a spec command runs over.

  `--path` wins when given: a project root, or a subdivision of its endpoint tree (whose
  project is then loaded from that root). Otherwise the whole endpoint tree of the project
  `--project` names (or the nearest one) is the scope.
  """
  try:
    if path is not None:
      target = Path(path).expanduser().resolve()
      if not target.is_dir():
        typer.echo(f'Unknown path: {target}', err=True)
        raise typer.Exit(code=1)
      scoped = resolve_scope(target)
      return resolve(scoped.root), scoped
    loaded = resolve_project(project)
    return loaded, resolve_scope(loaded)
  except NoEndpointSpecs as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  except NotAProject as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
