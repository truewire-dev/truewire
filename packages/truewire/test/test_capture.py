"""`truewire capture`: one live call, recorded as an example pair through the real client.

The "live API" here is the project's own mock server serving the examples it already
has, so the capture is checked against a known answer: the recorded pair must equal what
the mock served, and `truewire check` must accept it.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from typer.testing import CliRunner
from typing_extensions import Iterator

from truewire.cli import app
from truewire.cli.capture import EndpointRoute, filled_path, matches_route, path_pattern
from truewire.mock import running_mock_servers
from truewire_core.http import Exchange

FIXTURES = Path(__file__).parent / 'fixtures' / 'openapi'


def quickstart_project(tmp_path: Path, monkeypatch) -> Path:
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'petstore', '--base-url', 'https://petstore.example/v1']).exit_code == 0
  project = tmp_path / 'petstore'
  imported = runner.invoke(app, ['import', 'openapi', str(FIXTURES / 'petstore.yaml'), '--project', str(project)])
  assert imported.exit_code == 0, imported.output
  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  return project


def test_capture_records_the_wire_pair_and_checks_it(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  served = json.loads((examples / 'default.response.json').read_text())

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--description', 'Recorded through the mock', '--new', f'base_url={servers.http_base_url}',
      '--project', str(project),
    ])
  assert result.exit_code == 0, result.output
  assert 'pets.get_pet[captured]: HTTP 200' in result.output

  recorded_request = json.loads((examples / 'captured.request.json').read_text())
  recorded_response = json.loads((examples / 'captured.response.json').read_text())
  assert recorded_request == {'description': 'Recorded through the mock', 'request': {'petId': 42}}
  assert recorded_response == {'status': served['status'], 'payload': served['payload']}
  assert 'Result: OK' in result.output


def test_capture_scrubs_named_keys(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'scrubbed',
      '--scrub', 'name', '--new', f'base_url={servers.http_base_url}', '--project', str(project),
      '--no-check',
    ])
  assert result.exit_code == 0, result.output
  recorded = json.loads((examples / 'scrubbed.response.json').read_text())
  assert recorded['payload']['name'] == 'REDACTED_NAME'


def test_capture_refuses_an_error_response(tmp_path: Path, monkeypatch):
  """A request the mock has no example for answers 422; nothing is written."""
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 999}', '--id', 'missing',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])
  assert result.exit_code == 1
  assert 'HTTP 422' in result.output
  assert not (examples / 'missing.response.json').exists()


def test_capture_rejects_an_unknown_function(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  result = CliRunner().invoke(app, ['capture', 'pets.no_such', '--project', str(project)])
  assert result.exit_code == 1
  assert 'no endpoint with function' in result.output


def test_capture_drops_the_stale_unverified_declaration(tmp_path: Path, monkeypatch):
  """The pair `capture` writes is the evidence `unverified` said was missing, so the block
  goes with it; left behind it failed `truewire examples` unconditionally. Every other
  key keeps its place."""
  project = quickstart_project(tmp_path, monkeypatch)
  endpoint_file = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'endpoint.json'
  items = list(json.loads(endpoint_file.read_text()).items())
  keys = [key for key, _ in items]
  items.insert(1, ('unverified', {'reason': 'not_captured', 'detail': 'imported'}))
  endpoint_file.write_text(json.dumps(dict(items), indent=2) + '\n')

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])
  assert result.exit_code == 0, result.output
  assert 'removed the stale `unverified` declaration from spec/endpoints/pets/get_pet/endpoint.json' in result.output
  assert 'Result: OK' in result.output
  rewritten = json.loads(endpoint_file.read_text())
  assert 'unverified' not in rewritten
  assert list(rewritten) == keys
  assert endpoint_file.read_text().startswith('{\n  "')
  assert endpoint_file.read_text().endswith('}\n')

  coverage = CliRunner().invoke(app, ['examples', '--project', str(project)])
  assert coverage.exit_code == 0, coverage.output


def test_capture_leaves_an_endpoint_without_unverified_alone(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  endpoint_file = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'endpoint.json'
  before = endpoint_file.read_bytes()

  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project), '--no-check',
    ])
  assert result.exit_code == 0, result.output
  assert 'unverified' not in result.output
  assert endpoint_file.read_bytes() == before


@contextmanager
def token_server(payload: dict) -> Iterator[str]:
  """Stand in for the token endpoint a real core mints against: one route, any method,
  answering `payload`. Its body is what must never reach a recorded example."""
  class Handler(BaseHTTPRequestHandler):
    def _answer(self):
      body = json.dumps(payload).encode()
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args):
      pass

  server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    yield f'http://127.0.0.1:{server.server_port}/oauth2/token'
  finally:
    server.shutdown()
    server.server_close()
    thread.join()


def patch_core(project: Path, *, before: str = '', after: str = '', path_suffix: str = '') -> None:
  """Rewrite the project's hand-written core so one call makes more than one request.

  `before`/`after` are statements spliced into `Transport.send` around the endpoint
  request; `path_suffix` is appended to the URL the endpoint request goes to, for the
  case where the core never sends the request the endpoint declares.
  """
  core = project / 'src' / 'petstore' / 'core' / '__init__.py'
  source = core.read_text()
  request_line = "      params=params or None, content=body, headers=self.headers(public=public),\n"
  assert request_line in source
  if path_suffix:
    source = source.replace(
      "      method, self.base_url.rstrip('/') + '/' + filled.lstrip('/'),\n",
      f"      method, self.base_url.rstrip('/') + '/' + filled.lstrip('/') + {path_suffix!r},\n",
    )
  if before:
    source = source.replace(
      '    response = await self.http.request(\n', before + '    response = await self.http.request(\n',
    )
  if after:
    source = source.replace(
      '    if response.status_code >= 400:\n', after + '    if response.status_code >= 400:\n',
    )
  core.write_text(source)


def token_call(url: str, indent: str = '    ') -> str:
  return f"{indent}await self.http.request('POST', {url!r}, json={{}})\n"


def files_under(root: Path) -> list[Path]:
  return [path for path in root.rglob('*') if path.is_file()]


def test_capture_records_the_endpoint_exchange_when_a_token_call_comes_last(tmp_path: Path, monkeypatch):
  """The reproduction. A core that mints a token *after* the endpoint call put that token's
  200 body in the recorded example, because `capture` took the last exchange; the pair was
  then committed and published. The endpoint's own response is recorded instead, and the
  token never reaches a file."""
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  served = json.loads((examples / 'default.response.json').read_text())
  secret = 'oauth-access-token-that-must-not-be-recorded'

  with token_server({'access_token': secret, 'token_type': 'bearer', 'expires_in': 3600}) as url:
    patch_core(project, after=token_call(url))
    with running_mock_servers(project) as servers:
      result = CliRunner().invoke(app, [
        'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
        '--new', f'base_url={servers.http_base_url}', '--project', str(project),
      ])

  assert result.exit_code == 0, result.output
  recorded = json.loads((examples / 'captured.response.json').read_text())
  assert recorded == {'status': served['status'], 'payload': served['payload']}
  assert 'access_token' not in json.dumps(recorded)
  assert not [path for path in files_under(project) if secret in path.read_text(errors='ignore')]
  assert 'recorded the exchange for GET /pets/42' in result.output
  assert 'your core made 2 requests' in result.output
  assert 'POST /oauth2/token' in result.output
  assert 'Result: OK' in result.output


def test_capture_records_the_endpoint_exchange_when_a_token_call_comes_first(tmp_path: Path, monkeypatch):
  """The same core with its minting in the order that happened to be safe: still the
  endpoint's own exchange, and the token call is still reported as skipped."""
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  served = json.loads((examples / 'default.response.json').read_text())

  with token_server({'access_token': 'minted-first', 'token_type': 'bearer'}) as url:
    patch_core(project, before=token_call(url))
    with running_mock_servers(project) as servers:
      result = CliRunner().invoke(app, [
        'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
        '--new', f'base_url={servers.http_base_url}', '--project', str(project),
      ])

  assert result.exit_code == 0, result.output
  recorded = json.loads((examples / 'captured.response.json').read_text())
  assert recorded == {'status': served['status'], 'payload': served['payload']}
  assert 'recorded the exchange for GET /pets/42' in result.output
  assert 'the one below is not this endpoint\'s and was skipped' in result.output
  assert 'POST /oauth2/token' in result.output


def test_capture_refuses_when_no_exchange_matches_the_endpoint(tmp_path: Path, monkeypatch):
  """A core that never sends the request the endpoint declares records nothing: the
  refusal names the endpoint, what was expected and what was seen, by method and path
  only, and withholds the `ApiError` message, which can quote a response body."""
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'

  with token_server({'access_token': 'minted'}) as url:
    patch_core(project, before=token_call(url), path_suffix='/raw')
    with running_mock_servers(project) as servers:
      result = CliRunner().invoke(app, [
        'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'missing',
        '--new', f'base_url={servers.http_base_url}', '--project', str(project),
      ])

  assert result.exit_code == 1
  assert 'pets.get_pet: no request the core made matches this endpoint; nothing recorded' in result.output
  assert 'expected: GET /pets/42' in result.output
  assert 'POST /oauth2/token' in result.output
  assert 'GET /pets/42/raw' in result.output
  assert 'its message is withheld' in result.output
  assert not (examples / 'missing.response.json').exists()
  assert not (examples / 'missing.request.json').exists()


def test_capture_records_the_last_of_several_attempts_at_the_same_endpoint(tmp_path: Path, monkeypatch):
  """Two attempts at the endpoint -- a first one the API refused and the retry that
  answered -- are both this endpoint's own exchanges, so the last one stands, and the
  output says how many matched."""
  project = quickstart_project(tmp_path, monkeypatch)
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  served = json.loads((examples / 'default.response.json').read_text())
  first_attempt = (
    "    await self.http.request(\n"
    "      method, self.base_url.rstrip('/') + '/' + filled.lstrip('/'),\n"
    "      params={'stale': 'token'},\n"
    "    )\n"
  )

  patch_core(project, before=first_attempt)
  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])

  assert result.exit_code == 0, result.output
  recorded = json.loads((examples / 'captured.response.json').read_text())
  assert recorded == {'status': served['status'], 'payload': served['payload']}
  assert '2 requests matched this endpoint' in result.output
  assert 'took the last' in result.output


def exchange(method: str, url: str, body: dict | None = None) -> Exchange:
  request = httpx.Request(method, url, json=body) if body is not None else httpx.Request(method, url)
  return Exchange(request=request, response=httpx.Response(200, request=request))


def test_matching_reads_method_and_path_only():
  """A base URL's own path prefix, a query string and a percent-encoded path parameter are
  all things the endpoint does not declare, and none of them decides the match."""
  route = EndpointRoute(
    method='GET', display='GET /pets/a b', pattern=path_pattern('/pets/a b'),
    rpc_method=None, selector='method',
  )
  assert matches_route(exchange('GET', 'https://petstore.example/v1/pets/a%20b?fields=all'), route)
  assert not matches_route(exchange('POST', 'https://petstore.example/v1/pets/a%20b'), route)
  assert not matches_route(exchange('GET', 'https://petstore.example/v1/pets/a%20b/raw'), route)
  assert not matches_route(exchange('GET', 'https://id.petstore.example/oauth2/token'), route)


def test_matching_a_json_rpc_endpoint_reads_the_method_name_off_the_frame():
  """Every JSON-RPC call goes to the one base URL, so the path cannot tell two apart; the
  declared method name at the envelope's selector does."""
  route = EndpointRoute(
    method='POST', display='POST pets_get (JSON-RPC method)', pattern=None,
    rpc_method='pets_get', selector='method',
  )
  url = 'https://petstore.example/rpc'
  assert matches_route(exchange('POST', url, {'jsonrpc': '2.0', 'id': 1, 'method': 'pets_get'}), route)
  assert not matches_route(exchange('POST', url, {'jsonrpc': '2.0', 'id': 1, 'method': 'auth_login'}), route)
  assert not matches_route(exchange('POST', url), route)


def test_an_unfilled_path_slot_matches_one_segment():
  """A call that names no value for a slot still identifies the route, and never matches
  across a segment boundary."""
  pattern = path_pattern(filled_path('/pets/{petId}/toys', {}))
  assert pattern.search('/v1/pets/42/toys')
  assert not pattern.search('/v1/pets/42/7/toys')
  assert filled_path('/pets/{petId}', {'petId': 42}) == '/pets/42'


def test_an_undeclared_method_does_not_disqualify_an_exchange():
  """`spec.method` is optional -- a uniformly-POST JSON-RPC API leaves the verb to its core
  (ADR 0006) -- and a verb the endpoint never states cannot decide a match."""
  route = EndpointRoute(
    method=None, display='any method /pets/42', pattern=path_pattern('/pets/42'),
    rpc_method=None, selector='method',
  )
  assert matches_route(exchange('GET', 'https://petstore.example/v1/pets/42'), route)
  assert matches_route(exchange('POST', 'https://petstore.example/v1/pets/42'), route)
