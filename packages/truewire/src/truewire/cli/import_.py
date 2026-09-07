"""`truewire import`: seed a project's spec from an OpenAPI document or a registry spec."""
from pathlib import Path

import typer

from .common import PROJECT_OPTION, resolve_project

app = typer.Typer(help="Seed a project's spec from an OpenAPI document or the registry.")

from .import_registry import registry  # noqa: E402

app.command('registry')(registry)


@app.command('openapi')
def openapi(
  document: Path = typer.Argument(..., help='OpenAPI 3.0/3.1 document, JSON or YAML.'),
  project: str | None = PROJECT_OPTION,
  style: str = typer.Option('tags', '--style', help="Directory layout: one group per first tag ('tags') or nested path segments ('path')."),
  core: str = typer.Option('root', '--core', help='Core name the root router.json declares.'),
  group_core: str = typer.Option('default', '--group-core', help='Core name each group router.json declares.'),
  upstream: str | None = typer.Option(None, '--upstream', help='Upstream docs URL for router.json, when the document has none.'),
):
  """Import an OpenAPI document into the project's `spec/` tree.

  Every operation becomes one `endpoint.json`; component schemas referenced more than
  once go to `schemas.json`; document examples become recorded examples, flagged as
  document-sourced in the endpoint's notes. The written tree is validated through the
  same loaders and checks `truewire check` runs, and the command fails on any error.
  """
  from truewire.openapi import (
    OpenApiImportError, import_openapi, load_document, validate_tree,
  )

  loaded = resolve_project(project)
  if style not in ('tags', 'path'):
    typer.echo(f'--style must be tags or path, not {style!r}', err=True)
    raise typer.Exit(code=1)
  try:
    doc = load_document(document)
    report = import_openapi(
      doc, out=loaded.root, function_style=style, core=core, group_core=group_core,  # type: ignore[arg-type]
      upstream=upstream,
    )
  except OpenApiImportError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  typer.echo(report.summary())
  validation = validate_tree(loaded.root)
  typer.echo(f'\nvalidation: {len(validation.errors)} errors, {len(validation.warnings)} warnings')
  for line in [*validation.errors, *validation.warnings]:
    typer.echo(f'  {line}')
  if not validation.ok:
    raise typer.Exit(code=1)
