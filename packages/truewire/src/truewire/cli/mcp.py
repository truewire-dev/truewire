"""`truewire mcp`: expose a project's endpoints to an agent as MCP tools."""
import asyncio

import typer

from .common import PROJECT_OPTION, resolve_project


def mcp(
  project: str | None = PROJECT_OPTION,
  new: list[str] = typer.Option(
    [], '--new', help='key=value passed to the root client\'s `.new(...)`; JSON values are decoded. Repeatable.',
  ),
  list_tools: bool = typer.Option(False, '--list', help='Print the tools and exit instead of serving.'),
):
  """Serve the project's `rpc` endpoints as MCP tools over stdio.

  One tool per endpoint, named after its function path (`pets.get_pet` becomes
  `pets_get_pet`), taking the endpoint's own request schema. Calls go through the
  generated client, so responses are validated the same way they are for any caller.
  Point an MCP client at `truewire mcp --project <dir> --new base_url=...`.
  """
  from truewire.mcp import parse_new_kwargs, serve, tool_specs

  loaded = resolve_project(project)
  if list_tools:
    for tool in tool_specs(loaded):
      params = ', '.join(tool.input_schema.get('properties', {}).keys()) or '(no parameters)'
      typer.echo(f'{tool.name}: {tool.description.splitlines()[0]}')
      typer.echo(f'  {params}')
    return
  try:
    kwargs = parse_new_kwargs(new)
  except ValueError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  try:
    import mcp  # noqa: F401
  except ImportError:
    typer.echo('The `mcp` package is not installed: pip install "truewire[mcp]"', err=True)
    raise typer.Exit(code=1)
  asyncio.run(serve(loaded, kwargs))
