"""The `truewire` command line: one project, one `truewire.toml`, every command below it."""
import typer

from .capture import capture
from .check import check
from .docs import app as docs_app
from .examples import examples
from .generate import generate
from .import_ import app as import_app
from .init import init
from .mcp import mcp
from .mock import mock
from .standards import standards
from .surface import surface

app = typer.Typer(help='Typed clients, true to the wire.', no_args_is_help=True)
app.command('init')(init)
app.add_typer(import_app, name='import')
app.command('check')(check)
app.command('capture')(capture)
app.command('examples')(examples)
app.command('surface')(surface)
app.command('generate')(generate)
app.command('mock')(mock)
app.command('mcp')(mcp)
app.command('standards')(standards)
app.add_typer(docs_app, name='docs')
