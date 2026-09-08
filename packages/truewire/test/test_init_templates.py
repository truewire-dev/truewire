"""`truewire init --template <name>` writes a core that a fresh project can build on without
editing: every template passes `truewire check` and `truewire generate python` with pyright
clean on its own code, and the two templates that carry wire logic of their own (`hmac`
signing, `jsonrpc` envelope) are unit-tested by importing that logic out of the written
project. The HTTP templates ride the petstore OpenAPI flow `test_openapi_import.py` proves;
`jsonrpc` and `ws` get one fixture endpoint each under `fixtures/init_templates/`, served by
the mock server to a client built on the template.
"""
import asyncio
import hashlib
import hmac
import importlib
import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing_extensions import Iterator

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.cli.core_templates import TEMPLATES
from truewire.project import resolve
from truewire_core.exceptions import ApiError, AuthError, BadRequest, RateLimited

from conftest import forbid_import

OPENAPI = Path(__file__).parent / 'fixtures' / 'openapi' / 'petstore.yaml'
FIXTURES = Path(__file__).parent / 'fixtures' / 'init_templates'
BASE_URL = 'https://petstore.example/v1'


def init_project(tmp_path: Path, name: str, template: str) -> Path:
  """Run `truewire init --template <template>` in `tmp_path` and return the project root,
  with a `pyrightconfig.json` so the coming `generate` type-checks the template's own core
  against the interpreter running this suite."""
  runner = CliRunner()
  result = runner.invoke(app, ['init', name, '--dir', str(tmp_path / name), '--base-url', BASE_URL, '--template', template])
  assert result.exit_code == 0, result.output
  project = tmp_path / name
  venv = Path(sys.prefix)
  (project / 'pyrightconfig.json').write_text(json.dumps({
    'include': ['src'], 'extraPaths': ['src'],
    'venvPath': str(venv.parent), 'venv': venv.name, 'typeCheckingMode': 'standard',
  }))
  return project


def import_petstore(project: Path):
  """Seed the project's spec from the petstore OpenAPI fixture."""
  imported = CliRunner().invoke(app, ['import', 'openapi', str(OPENAPI), '--project', str(project)])
  assert imported.exit_code == 0, imported.output


def check_and_generate(project: Path, package: str):
  """`truewire check`, then `truewire generate python` with the package refused from
  `sys.path` (ADR 0011) and pyright asserted to have run clean."""
  runner = CliRunner()
  checked = runner.invoke(app, ['check', '--project', str(project)])
  assert checked.exit_code == 0, checked.output
  with forbid_import(package):
    generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  assert '0 errors' in generated.output, generated.output


@contextmanager
def importable(project: Path, package: str) -> Iterator[None]:
  """Make the written project's package importable inside the block, and forget it after."""
  sys.path.insert(0, str(project / 'src'))
  try:
    yield
  finally:
    sys.path.remove(str(project / 'src'))
    for name in [m for m in list(sys.modules) if m == package or m.startswith(package + '.')]:
      del sys.modules[name]


def seed(project: Path, fixture: str):
  """Copy one `fixtures/init_templates/<fixture>/spec` tree over the project's `spec/`."""
  shutil.copytree(FIXTURES / fixture / 'spec', project / 'spec', dirs_exist_ok=True)


def test_help_lists_every_template():
  result = CliRunner().invoke(app, ['init', '--help'])
  assert result.exit_code == 0
  for name in TEMPLATES:
    assert f'`{name}`' in result.output


def test_unknown_template_is_refused(tmp_path: Path):
  result = CliRunner().invoke(app, ['init', 'demo', '--dir', str(tmp_path / 'demo'), '--template', 'soap'])
  assert result.exit_code == 1
  assert 'unknown template' in result.output
  assert not (tmp_path / 'demo').exists()


@pytest.mark.parametrize('template', ['bearer', 'hmac', 'jsonrpc'])
def test_http_templates_init_import_check_and_generate_petstore(tmp_path: Path, template: str):
  """The README quickstart on each HTTP template: init, seed from OpenAPI, check, generate,
  pyright clean. `bearer` and `hmac` then answer a REST call through the mock; `jsonrpc`
  posts JSON-RPC frames, which the REST petstore cannot answer, and gets its own call test
  below."""
  project = init_project(tmp_path, 'petstore', template)
  import_petstore(project)
  check_and_generate(project, 'petstore')
  assert (project / 'src' / 'petstore' / 'pets' / 'get_pet.py').is_file()
  if template == 'jsonrpc':
    return

  from truewire.mock import running_mock_servers

  with importable(project, 'petstore'):
    petstore = importlib.import_module('petstore')

    async def call(base_url: str):
      async with petstore.Petstore.new(base_url=base_url, api_key='k', **({'api_secret': 's'} if template == 'hmac' else {})) as client:
        return await client.pets.get_pet(pet_id=42), await client.store.get_inventory()

    with running_mock_servers(resolve(project)) as servers:
      pet, inventory = asyncio.run(call(servers.http_base_url))
  assert pet['name'] == 'Fido'
  assert inventory['available'] == 3


def test_hmac_core_signs_timestamp_method_path_and_body(tmp_path: Path):
  """The signing recipe, imported from the written project: a pure message function, a
  pure HMAC, and headers that only appear on non-public calls."""
  project = init_project(tmp_path, 'signed', 'hmac')
  import_petstore(project)
  check_and_generate(project, 'signed')
  with importable(project, 'signed'):
    core = importlib.import_module('signed.core')

    message = core.signature_message('1700000000000', 'post', '/orders?limit=2', b'{"qty": 1}')
    assert message == b'1700000000000POST/orders?limit=2{"qty": 1}'
    assert core.signature_message('1', 'GET', '/pets', None) == b'1GET/pets'
    assert core.sign('secret', message) == hmac.new(b'secret', message, hashlib.sha256).hexdigest()

    transport = core.Transport(base_url=BASE_URL, api_key='key', api_secret='secret', timestamp=lambda: '1700000000000')
    headers = transport.headers('POST', '/orders?limit=2', b'{"qty": 1}', public=False)
    assert headers == {
      'X-API-Key': 'key',
      'X-Timestamp': '1700000000000',
      'X-Signature': core.sign('secret', message),
    }
    assert transport.headers('GET', '/pets', None, public=True) == {}
    with pytest.raises(AuthError):
      core.Transport(base_url=BASE_URL).headers('GET', '/me', None, public=False)


def test_jsonrpc_core_builds_frames_and_maps_errors(tmp_path: Path):
  """The envelope logic, imported from the written project: the request frame, `result`
  unwrapping, id checking, and the code-to-exception mapping."""
  project = init_project(tmp_path, 'chain', 'jsonrpc')
  seed(project, 'jsonrpc')
  check_and_generate(project, 'chain')
  with importable(project, 'chain'):
    core = importlib.import_module('chain.core')

    assert core.build_request(7, 'getBalance', {'address': '0x1'}) == {
      'jsonrpc': '2.0', 'id': 7, 'method': 'getBalance', 'params': {'address': '0x1'},
    }
    assert 'params' not in core.build_request(1, 'getTime', None)
    assert core.unwrap({'jsonrpc': '2.0', 'id': 7, 'result': '0x10'}, id=7, method='getBalance') == '0x10'

    with pytest.raises(ApiError, match='does not match'):
      core.unwrap({'jsonrpc': '2.0', 'id': 8, 'result': '0x10'}, id=7, method='getBalance')
    with pytest.raises(ApiError, match='neither result nor error'):
      core.unwrap({'jsonrpc': '2.0', 'id': 7}, id=7, method='getBalance')
    with pytest.raises(ApiError, match='not a JSON-RPC reply'):
      core.unwrap('nope', id=7, method='getBalance')

    def error(code: int, message: str = 'boom', **rest):
      return {'jsonrpc': '2.0', 'id': 7, 'error': {'code': code, 'message': message, **rest}}

    with pytest.raises(BadRequest, match=r'invalid params \(code -32602\)'):
      core.unwrap(error(-32602, 'invalid params'), id=7, method='getBalance')
    with pytest.raises(AuthError):
      core.unwrap(error(-32001, 'unauthorized'), id=7, method='getBalance')
    with pytest.raises(RateLimited):
      core.unwrap(error(429, 'slow down'), id=7, method='getBalance')
    with pytest.raises(ApiError, match='"reason": "custom"') as raised:
      core.unwrap(error(-32000, 'server error', data={'reason': 'custom'}), id=7, method='getBalance')
    assert type(raised.value) is ApiError


def test_jsonrpc_template_calls_a_fixture_method_through_the_mock(tmp_path: Path):
  """One JSON-RPC endpoint (`path` is the method name, `envelope.payload` is `result`):
  the mock matches the posted `method`/`params`, echoes the id, and the core hands back
  the unwrapped, validated `result`."""
  project = init_project(tmp_path, 'chain', 'jsonrpc')
  seed(project, 'jsonrpc')
  check_and_generate(project, 'chain')

  from truewire.mock import running_mock_servers
  from truewire_core.http import recording

  with importable(project, 'chain'):
    chain = importlib.import_module('chain')

    async def call(base_url: str):
      async with chain.Chain.new(base_url=base_url) as client:
        with recording() as exchanges:
          pet = await client.pets.get_pet(pet_id=42)
        return pet, exchanges

    with running_mock_servers(resolve(project)) as servers:
      pet, exchanges = asyncio.run(call(servers.http_base_url))
  assert pet == {'id': 42, 'name': 'Fido'}
  sent = json.loads(exchanges[-1].request.content)
  assert sent == {'jsonrpc': '2.0', 'id': 1, 'method': 'pets_get', 'params': {'petId': 42}}
  assert exchanges[-1].request.method == 'POST'


def test_ws_template_subscribes_to_a_fixture_stream_through_the_mock(tmp_path: Path):
  """One stream endpoint under the `streams` core: `init` wired `streams/router.json` and
  `[python.cores]` `children` to the root's socket, `generate` type-checks the socket
  client, and the mock's default subscribe dialect drives a full subscribe, push,
  unsubscribe round trip."""
  project = init_project(tmp_path, 'feed', 'ws')
  assert json.loads((project / 'spec' / 'endpoints' / 'streams' / 'router.json').read_text())['core'] == 'streams'
  seed(project, 'ws')
  check_and_generate(project, 'feed')
  assert (project / 'src' / 'feed' / 'streams' / 'trades.py').is_file()

  from truewire.mock import running_mock_servers

  with importable(project, 'feed'):
    feed = importlib.import_module('feed')

    async def subscribe(http_url: str, ws_url: str):
      received = []
      async with feed.Feed.new(base_url=http_url, ws_url=ws_url) as client:
        assert client.streams.client is client.socket
        async with client.streams.trades(id='ACME-1') as stream:
          reply = stream.reply
          async for message in stream:
            received.append(message)
            if len(received) == 2:
              break
      return reply, received

    with running_mock_servers(resolve(project)) as servers:
      assert servers.ws_server is not None
      reply, received = asyncio.run(subscribe(servers.http_base_url, servers.ws_server.url))
  assert reply == {'type': 'subscribed', 'channel': 'trades', 'id': 'ACME-1'}
  assert [str(m['data']['price']) for m in received] == ['100.5', '100.75']
  assert received[0]['channel'] == 'trades'
