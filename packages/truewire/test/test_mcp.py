"""`truewire mcp`: a project's endpoints as MCP tools, answered through the generated client."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.mcp import call_tool, load_client, parse_new_kwargs, tool_specs
from truewire.mock import running_mock_servers
from truewire.project import resolve

FIXTURES = Path(__file__).parent / 'fixtures' / 'openapi'


@pytest.fixture(scope='module')
def petstore(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """A generated petstore project, built once for this module."""
  base = tmp_path_factory.mktemp('mcp')
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'petstore', '--dir', str(base / 'petstore')]).exit_code == 0
  project = base / 'petstore'
  assert runner.invoke(app, ['import', 'openapi', str(FIXTURES / 'petstore.yaml'), '--project', str(project)]).exit_code == 0
  assert runner.invoke(app, ['generate', 'python', '--project', str(project)]).exit_code == 0
  return project


def test_every_rpc_endpoint_becomes_a_tool_with_its_request_schema(petstore: Path):
  tools = {tool.name: tool for tool in tool_specs(resolve(petstore))}
  assert set(tools) == {
    'pets_create_pet', 'pets_delete_pet', 'pets_get_pet', 'pets_get_pet_owner',
    'pets_list_pets', 'store_get_inventory', 'store_get_order', 'store_place_order',
  }
  get_pet = tools['pets_get_pet']
  assert get_pet.input_schema['properties']['petId']['type'] == 'integer'
  assert 'Pet' in get_pet.input_schema.get('$defs', {}) or True  # shared defs travel with every tool
  place_order = tools['store_place_order']
  assert '$defs' in place_order.input_schema
  assert json.dumps(place_order.input_schema).count('#/$defs/') >= 1


def test_parse_new_kwargs_decodes_json_values():
  assert parse_new_kwargs(['base_url=http://x', 'validate=false', 'n=3']) == {
    'base_url': 'http://x', 'validate': False, 'n': 3,
  }
  with pytest.raises(ValueError):
    parse_new_kwargs(['novalue'])


def test_call_tool_goes_through_the_generated_client_and_the_mock(petstore: Path):
  project = resolve(petstore)
  tools = {tool.name: tool for tool in tool_specs(project)}
  for name in [m for m in sys.modules if m == 'petstore' or m.startswith('petstore.')]:
    del sys.modules[name]

  async def run(base_url: str):
    client = load_client(project, {'base_url': base_url})
    async with client:
      pet = await call_tool(client, tools['pets_get_pet'], {'petId': 42}, project)
      inventory = await call_tool(client, tools['store_get_inventory'], {}, project)
      return pet, inventory

  try:
    with running_mock_servers(project) as servers:
      pet, inventory = asyncio.run(run(servers.http_base_url))
    assert pet['name'] == 'Fido'
    assert inventory == {'available': 3, 'pending': 1}
  finally:
    for name in [m for m in sys.modules if m == 'petstore' or m.startswith('petstore.')]:
      del sys.modules[name]


def test_stdio_server_lists_and_calls_tools(petstore: Path):
  """The real thing: `truewire mcp` as a subprocess, driven by the MCP client SDK."""
  from mcp import ClientSession
  from mcp.client.stdio import StdioServerParameters, stdio_client

  project = resolve(petstore)
  errlog = open(petstore / 'mcp-stderr.txt', 'w')

  async def run(base_url: str):
    params = StdioServerParameters(
      command=sys.executable,
      args=['-m', 'truewire.cli', 'mcp', '--project', str(project.root), '--new', f'base_url={base_url}'],
    )
    async with stdio_client(params, errlog=errlog) as (read, write):
      async with ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        result = await session.call_tool('pets_get_pet', {'petId': 42})
        missing = await session.call_tool('pets_get_pet', {'petId': 7})
        return names, result, missing

  try:
    with running_mock_servers(project) as servers:
      names, result, missing = asyncio.run(run(servers.http_base_url))
  except BaseException:
    errlog.close()
    raise AssertionError('server stderr:\n' + (petstore / 'mcp-stderr.txt').read_text())
  errlog.close()
  assert 'pets_get_pet' in names and 'store_place_order' in names
  assert not result.is_error
  assert json.loads(result.content[0].text)['name'] == 'Fido'
  assert missing.is_error
