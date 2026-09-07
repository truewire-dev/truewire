"""Serve a project's `rpc` endpoints as MCP tools, backed by its generated client.

One tool per endpoint: the tool's input schema is the endpoint's own request schema (with
shared `$ref`s carried as `$defs`), and a call resolves to the generated method the same way
the replay tests do -- API-named arguments translated to the method's Python parameters --
so an agent talks to the API through the same typed, validated path a human caller does.
"""
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing_extensions import Any

from truewire.examples import client_identifier, coerce_example_call, resolve_endpoint_function
from truewire.project import Project
from truewire.spec import Endpoint, ExampleRequest, endpoint_records, load_shared_schemas


@dataclass(frozen=True)
class ToolSpec:
  """One MCP tool derived from one `rpc` endpoint."""
  name: str
  """Tool name: the endpoint's function path with `.` replaced by `_`."""
  description: str
  input_schema: dict[str, Any]
  """The endpoint's request schema, shared `$ref`s resolvable through `$defs`."""
  endpoint: Endpoint
  endpoint_path: Path


def rewrite_refs(value: Any) -> Any:
  """Bare-id `$ref`s (`"Pet"`) to `#/$defs/Pet`, so a plain JSON Schema consumer resolves them."""
  if isinstance(value, dict):
    return {
      key: (
        f'#/$defs/{item}'
        if key == '$ref' and isinstance(item, str) and not item.startswith('#/')
        else rewrite_refs(item)
      )
      for key, item in value.items()
    }
  if isinstance(value, list):
    return [rewrite_refs(item) for item in value]
  return value


def tool_specs(project: Project) -> list[ToolSpec]:
  """Every `rpc` endpoint the project generates a method for, as a tool.

  Stream endpoints are skipped (a tool call is one request, one reply), as are endpoints
  declaring `surface.kind == 'absent'`.
  """
  shared = rewrite_refs(load_shared_schemas(project))
  tools: list[ToolSpec] = []
  for record in endpoint_records(project):
    endpoint = record.endpoint
    if endpoint.spec.kind != 'rpc':
      continue
    if endpoint.surface is not None and endpoint.surface.kind == 'absent':
      continue
    function = endpoint.resolved_function(record.path, project.spec_dir)
    request = getattr(endpoint.spec, 'request', None)
    schema: dict[str, Any] = (
      rewrite_refs(dict(request)) if request else {'type': 'object', 'properties': {}}
    )
    if shared:
      schema = {**schema, '$defs': shared}
    description = getattr(endpoint.spec, 'description', None) or function
    tools.append(ToolSpec(
      name=function.replace('.', '_'), description=description, input_schema=schema,
      endpoint=endpoint, endpoint_path=record.path,
    ))
  return tools


def root_class_name(project: Project) -> str:
  """The generated root client class: `[python].name`, else PascalCase of the project name."""
  python = project.config.python
  if python is not None and python.name:
    return python.name
  return ''.join(part.capitalize() for part in project.name.split('_'))


def load_client(project: Project, new_kwargs: dict[str, Any]) -> Any:
  """Import the generated package and build its root client via `<Root>.new(**new_kwargs)`."""
  src = str(project.python_src)
  if src not in sys.path:
    sys.path.insert(0, src)
  # Import the package as it is on disk now: a `generate` in this same process imported
  # the placeholder package first (the generator imports the target to resolve its core),
  # and that stale module would otherwise be returned from the import cache.
  package = project.package_name
  for name in [n for n in sys.modules if n == package or n.startswith(package + '.')]:
    del sys.modules[name]
  module = importlib.import_module(package)
  root = getattr(module, root_class_name(project))
  return root.new(**new_kwargs)


async def call_tool(client: Any, tool: ToolSpec, arguments: dict[str, Any], project: Project) -> Any:
  """Call `tool`'s generated method on `client` with API-named `arguments`."""
  fn = resolve_endpoint_function(
    client, tool.endpoint, endpoint_path=tool.endpoint_path, spec_root=project.spec_dir,
  )
  args, kwargs = coerce_example_call(
    fn, ExampleRequest(request=arguments), identifier=client_identifier(project),
  )
  return await fn(*args, **kwargs)


def to_json(value: Any) -> str:
  """Render a response for a tool result: dates as ISO strings, decimals as strings."""
  def default(item: Any) -> Any:
    if isinstance(item, (datetime, date)):
      return item.isoformat()
    if isinstance(item, Decimal):
      return str(item)
    if isinstance(item, (set, frozenset)):
      return sorted(item)
    return str(item)
  return json.dumps(value, default=default, indent=2)


def parse_new_kwargs(pairs: list[str]) -> dict[str, Any]:
  """`key=value` pairs for `<Root>.new(...)`; JSON values are decoded, others stay strings."""
  out: dict[str, Any] = {}
  for pair in pairs:
    if '=' not in pair:
      raise ValueError(f'expected key=value, got {pair!r}')
    key, _, raw = pair.partition('=')
    try:
      out[key] = json.loads(raw)
    except json.JSONDecodeError:
      out[key] = raw
  return out


async def serve(project: Project, new_kwargs: dict[str, Any]):
  """Run the MCP server over stdio until the client disconnects."""
  import mcp.types as types
  from mcp.server.lowlevel import Server
  from mcp.server.stdio import stdio_server

  tools = {tool.name: tool for tool in tool_specs(project)}
  client = load_client(project, new_kwargs)

  async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
      types.Tool(name=tool.name, description=tool.description, inputSchema=tool.input_schema)
      for tool in tools.values()
    ])

  async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    tool = tools.get(params.name)
    if tool is None:
      return types.CallToolResult(
        content=[types.TextContent(type='text', text=f'unknown tool: {params.name}')], isError=True,
      )
    try:
      result = await call_tool(client, tool, params.arguments or {}, project)
    except Exception as exc:  # noqa: BLE001 -- every failure is reported to the agent, never raised
      return types.CallToolResult(
        content=[types.TextContent(type='text', text=f'{type(exc).__name__}: {exc}')], isError=True,
      )
    return types.CallToolResult(content=[types.TextContent(type='text', text=to_json(result))])

  server: Any = Server(
    f'truewire-{project.name}', on_list_tools=on_list_tools, on_call_tool=on_call_tool,
  )
  async with client:
    async with stdio_server() as (read, write):
      await server.run(read, write, server.create_initialization_options())
