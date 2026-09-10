"""The `truewire` command line: one project, one `truewire.toml`, every command below it."""
from importlib.metadata import PackageNotFoundError, version

import typer

from .capture import capture
from .check import check
from .docs import app as docs_app
from .examples import examples
from .generate import generate
from .import_ import app as import_app
from .init import init
from .mcp import mcp
from .migrate import migrate
from .mock import mock
from .plan import plan
from .standards import standards
from .surface import surface

app = typer.Typer(help='Typed clients, true to the wire.', no_args_is_help=True)


def _print_version(value: bool) -> None:
  if not value:
    return
  try:
    installed = version('truewire')
  except PackageNotFoundError:  # a source checkout that was never installed
    installed = 'unknown'
  typer.echo(f'truewire {installed}')
  raise typer.Exit()


@app.callback()
def _root(
  version: bool = typer.Option(  # noqa: ARG001 - consumed by the callback
    False,
    '--version',
    '-V',
    help='Print the installed toolchain version and exit.',
    callback=_print_version,
    is_eager=True,
  ),
) -> None:
  """Typed clients, true to the wire."""
app.command('init')(init)
app.add_typer(import_app, name='import')
app.command('check')(check)
app.command('capture')(capture)
app.command('examples')(examples)
app.command('surface')(surface)
app.command('generate')(generate)
app.command('mock')(mock)
app.command('plan')(plan)
app.command('mcp')(mcp)
app.command('migrate')(migrate)
app.command('standards')(standards)
app.add_typer(docs_app, name='docs')
