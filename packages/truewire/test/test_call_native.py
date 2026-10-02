"""`truewire call typescript|rust`: arguments to wire values, `CoreOptions` from the core's
source, the driver's events printed as the Python call prints them, and the call itself
through `examples/github`'s TypeScript and Rust clients against `truewire mock`."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import http.server
import threading
import time
from importlib.resources import files
from pathlib import Path

import pytest

from truewire.call import CallError, find_target
from truewire.call.native import (
  admits_string, argument_properties, build_rust_driver, core_options, native_options, node_version,
  rust_driver_source, snake_case, wire_arguments, wire_value,
)
from truewire.cli.call import follow_native
from truewire.project import load_project
from truewire.test import package_directory

from test_call import environment, mock, truewire

EXAMPLES = Path(__file__).resolve().parents[3] / 'examples'
GITHUB = EXAMPLES / 'github'
KRAKEN = EXAMPLES / 'kraken'


@pytest.mark.parametrize(('schema', 'expected'), [
  ({'type': 'string'}, True),
  ({'type': ['integer', 'null']}, False),
  ({'type': ['string', 'null']}, True),
  ({'enum': ['open', 'closed']}, True),
  ({'const': 3}, False),
  ({'anyOf': [{'type': 'integer'}, {'type': 'string'}]}, True),
  ({'anyOf': [{'type': 'integer'}, {'type': 'boolean'}]}, False),
  ({'$ref': 'State'}, True),
  ({'$ref': 'Missing'}, None),
  ({}, None),
  (None, None),
])
def test_admits_string(schema, expected):
  assert admits_string(schema, {'State': {'type': 'string', 'enum': ['open']}}) is expected


def test_a_value_is_a_string_where_the_schema_takes_one_and_json_elsewhere():
  assert wire_value('42', {'type': 'string'}, {}) == '42'
  assert wire_value('42', {'type': 'integer'}, {}) == 42
  assert wire_value('true', {'type': 'boolean'}, {}) is True
  assert wire_value('["BTC/USD"]', {'type': 'array'}, {}) == ['BTC/USD']
  assert wire_value('two', {'type': 'integer'}, {}) == 'two'  # the codec names what is wrong
  assert wire_value('{"a": 1}', None, {}) == {'a': 1}


def test_wire_arguments_follow_the_endpoint_schema():
  project = load_project(GITHUB)
  target = find_target(project, 'issues.list')
  wired = wire_arguments(target, {'owner': '42', 'repo': 'truewire', 'per_page': '1', 'state': 'all'}, project)
  assert wired == {'owner': '42', 'repo': 'truewire', 'per_page': 1, 'state': 'all'}


@pytest.mark.parametrize(('root', 'language', 'fields'), [
  (GITHUB, 'typescript', {
    'baseUrl': 'string', 'token': 'string', 'validate': 'boolean', 'http': 'HttpClient', 'proxy': 'string',
  }),
  (GITHUB, 'rust', {
    'base_url': 'Option<String>', 'token': 'Option<String>', 'http': 'Option<HttpClient>', 'proxy': 'Option<String>',
  }),
  (KRAKEN, 'typescript', {
    'baseUrl': 'string', 'wsUrl': 'string', 'wsAuthUrl': 'string', 'credentials': 'Credentials',
    'validate': 'boolean', 'http': 'HttpClient', 'createWebSocket': "SocketOptions['createWebSocket']", 'proxy': 'string',
  }),
  (KRAKEN, 'rust', {
    'base_url': 'Option<String>', 'ws_url': 'Option<String>', 'ws_auth_url': 'Option<String>',
    'credentials': 'Option<Credentials>', 'http': 'Option<HttpClient>', 'proxy': 'Option<String>',
  }),
])
def test_core_options_are_read_from_the_core(root: Path, language: str, fields: dict[str, str]):
  assert core_options(load_project(root), language) == fields


def test_a_core_without_core_options_says_how_call_builds_the_client(tmp_path: Path):
  project = tmp_path / 'github'
  shutil.copytree(GITHUB, project, ignore=shutil.ignore_patterns('node_modules', 'target', '.truewire', 'spec'))
  (project / 'src' / 'github' / 'core' / 'index.ts').write_text('export class Core {}\n')
  with pytest.raises(CallError, match=r'no CoreOptions; call typescript builds the client as new GitHub\(new Core'):
    core_options(load_project(project), 'typescript')


def test_native_options_spell_each_field_as_the_core_does():
  project = load_project(GITHUB)
  secrets = {'GITHUB_TOKEN': 'secret'}
  assert native_options(project, 'typescript', secrets=secrets, base_url='http://mock', new={'validate': False}) == {
    'token': 'secret', 'baseUrl': 'http://mock', 'validate': False,
  }
  assert native_options(project, 'rust', secrets=secrets, base_url='http://mock') == {
    'token': 'secret', 'base_url': 'http://mock',
  }
  assert native_options(project, 'typescript', secrets={}, new={'base_url': 'http://a'}) == {'baseUrl': 'http://a'}


def test_native_options_refuse_what_core_options_does_not_take():
  project = load_project(GITHUB)
  with pytest.raises(CallError, match='--new colour: CoreOptions .* has no colour; it has baseUrl, token'):
    native_options(project, 'typescript', secrets={}, new={'colour': 'red'})
  with pytest.raises(CallError, match='--ws-url: CoreOptions .* takes no ws_url'):
    native_options(project, 'rust', secrets={}, ws_url='ws://mock')
  with pytest.raises(CallError, match=r'GITHUB_APP_ID \(from \[secrets\]\) matches no keyword of CoreOptions'):
    native_options(project, 'typescript', secrets={'GITHUB_APP_ID': '1'})


def test_snake_case():
  assert [snake_case(name) for name in ('apiKey', 'wsAuthUrl', 'base_url', 'token')] == [
    'api_key', 'ws_auth_url', 'base_url', 'token',
  ]


def test_the_rust_driver_sets_only_the_fields_given_and_dispatches_by_kind():
  project = load_project(GITHUB)
  job = {'function': 'repos.get', 'kind': 'rpc', 'arguments': {}, 'options': {'base_url': 'http://mock'}}
  source = rust_driver_source(project, 'github', job)
  assert 'use github::core::CoreOptions;' in source
  assert 'options.base_url = option(&job, "base_url");' in source
  assert 'options.token' not in source
  assert 'GitHub::new(options)' in source
  assert 'client.call(&function' in source and 'subscribe' not in source
  streaming = rust_driver_source(project, 'github', {**job, 'kind': 'stream'})
  assert 'client.subscribe(&function' in streaming and 'client.call(' not in streaming


def driver(script: str) -> subprocess.Popen[str]:
  """A stand-in driver: `script` run by this interpreter, printing events."""
  return subprocess.Popen(
    [sys.executable, '-c', script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
    start_new_session=True,
  )


def test_a_result_prints_as_the_python_call_prints_it(capsys: pytest.CaptureFixture[str]):
  process = driver('import json; print(json.dumps({"result": {"name": "caf\\u00e9", "n": 1}}))')
  assert follow_native(process, 'repos.get', stream=False) == 0
  assert capsys.readouterr().out == json.dumps({'name': 'café', 'n': 1}, indent=2, ensure_ascii=False) + '\n'


def test_an_error_event_exits_1_naming_the_function(capsys: pytest.CaptureFixture[str]):
  process = driver('import json; print(json.dumps({"error": {"type": "BadRequest", "message": "HTTP 422"}})); raise SystemExit(1)')
  assert follow_native(process, 'repos.get', stream=False) == 1
  captured = capsys.readouterr()
  assert captured.out == ''
  assert captured.err == 'repos.get: BadRequest: HTTP 422\n'


def test_a_driver_that_dies_without_an_event_passes_its_code_on(capsys: pytest.CaptureFixture[str]):
  assert follow_native(driver('raise SystemExit(3)'), 'repos.get', stream=False) == 3
  assert follow_native(driver('print("not json")'), 'repos.get', stream=False) == 1
  assert 'not an event: not json' in capsys.readouterr().err


STREAM_DRIVER = '''
import json, signal, sys, time
def out(**event):
  print(json.dumps(event), flush=True)
stopped = []
signal.signal(signal.SIGINT, lambda *_: stopped.append(1))
out(reply={"ok": True})
out(message={"n": 1})
open(sys.argv[1] if len(sys.argv) > 1 else "READY", "w").close()
while not stopped:
  time.sleep(0.01)
out(unsubscribed={"bye": True})
'''


def test_sigint_reaches_the_driver_once_and_it_unsubscribes(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
  ready = tmp_path / 'ready'
  process = subprocess.Popen(
    [sys.executable, '-c', STREAM_DRIVER, str(ready)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
    text=True, start_new_session=True,
  )

  def interrupt():
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
      time.sleep(0.01)
    os.kill(os.getpid(), signal.SIGINT)

  threading.Thread(target=interrupt, daemon=True).start()
  assert follow_native(process, 'streams.trades', stream=True) == 0
  captured = capsys.readouterr()
  assert captured.out == '{"ok": true}\n{"n": 1}\n'
  assert captured.err == 'unsubscribed: {"bye": true}\n'
  assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def github_ready(language: str) -> str | None:
  """Why `examples/github` cannot run `language` here, or `None` when it can."""
  if language == 'typescript':
    if shutil.which('node') is None:
      return 'node is not on PATH'
    if not (GITHUB / 'node_modules').is_dir():
      return 'examples/github has no node_modules'
  elif shutil.which('cargo') is None:
    return 'cargo is not on PATH'
  return None


def declared(reference, value):
  """`value` cut down to the keys `reference` has, recursively: what the other runtimes
  share with Python, which drops the keys its types do not declare."""
  if isinstance(reference, dict) and isinstance(value, dict):
    return {key: declared(reference[key], value[key]) for key in reference if key in value}
  if isinstance(reference, list) and isinstance(value, list):
    return [declared(a, b) for a, b in zip(reference, value)]
  return value


@pytest.mark.parametrize('language', ['typescript', 'rust'])
def test_the_call_matches_the_python_one_on_every_declared_key(language: str):
  if reason := github_ready(language):
    pytest.skip(reason)
  arguments = ['issues.list', 'owner=truewire-dev', 'repo=truewire', 'state=all', 'per_page=1', 'page=2']
  with mock(GITHUB) as ready:
    python = truewire('call', 'python', *arguments, '--base-url', ready['http'], '--project', str(GITHUB))
    native = subprocess.run(
      [sys.executable, '-m', 'truewire.cli', 'call', language, *arguments, '--base-url', ready['http'], '--project', str(GITHUB)],
      capture_output=True, text=True, timeout=900, env=environment(),
    )
  assert python.returncode == 0, python.stderr
  assert native.returncode == 0, native.stderr
  expected, got = json.loads(python.stdout), json.loads(native.stdout)
  assert declared(expected, got) == expected  # timestamps too, digit for digit
  assert native.stdout.startswith('[\n  {\n')  # indented as the Python call indents


@pytest.mark.parametrize('language', ['typescript', 'rust'])
def test_an_ill_typed_argument_exits_1_naming_it(language: str):
  if reason := github_ready(language):
    pytest.skip(reason)
  result = subprocess.run(
    [sys.executable, '-m', 'truewire.cli', 'call', language, 'issues.list', 'owner=a', 'repo=b', 'page=two',
     '--base-url', 'http://127.0.0.1:9', '--project', str(GITHUB)],
    capture_output=True, text=True, timeout=900, env=environment(),
  )
  assert result.returncode == 1
  assert result.stdout == ''
  assert result.stderr.startswith('issues.list: ValidationError: ')
  assert '/page' in result.stderr


def children(pid: int) -> list[bytes]:
  """The command lines of `pid`'s child processes, read from `/proc`."""
  found = []
  for stat in Path('/proc').glob('[0-9]*/stat'):
    try:
      if int(stat.read_text().rsplit(')', 1)[1].split()[1]) == pid:
        found.append((stat.parent / 'cmdline').read_bytes())
    except (OSError, ValueError, IndexError):
      continue
  return found


@pytest.mark.skipif(not Path('/proc/self/stat').is_file(), reason='reads command lines from /proc')
@pytest.mark.parametrize(('language', 'driver_name'), [('typescript', b'call.mjs'), ('rust', b'truewire-call')])
def test_the_job_and_its_secrets_never_reach_the_driver_s_command_line(language: str, driver_name: bytes):
  if reason := github_ready(language):
    pytest.skip(reason)
  secret = f'ghp_argv{os.urandom(8).hex()}'
  with socket.create_server(('127.0.0.1', 0)) as server:
    server.settimeout(900)  # the first `call rust` builds the driver
    call = subprocess.Popen(
      [sys.executable, '-m', 'truewire.cli', 'call', language, 'repos.get', 'owner=a', 'repo=b',
       '--base-url', f'http://127.0.0.1:{server.getsockname()[1]}', '--new', f'token={secret}',
       '--project', str(GITHUB)],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment(),
    )
    try:
      connection, _ = server.accept()
      with connection:  # never answered, so the driver is still running
        request = b''
        while b'\r\n\r\n' not in request and (chunk := connection.recv(65536)):
          request += chunk
        assert secret.encode() in request  # the job did reach the driver, on stdin
        lines = children(call.pid)
        assert any(driver_name in line for line in lines), lines
        assert not any(secret.encode() in line or b'"options"' in line for line in lines), lines
    finally:
      call.terminate()
      call.communicate(timeout=60)


BIG = 9007199254740993
"""2^53 + 1: the first integer a JavaScript `number` cannot hold."""

ECHO_PACKAGE = {
  'index.ts': (
    "import * as echo from './echo.js'\n"
    "export class Echo {\n"
    "  constructor(private core: any) {}\n"
    "  echo(request: unknown) { return this.core.request({ request, responseCodec: echo.Response }) }\n"
    "}\n"
  ),
  'core/index.ts': 'export class Core {\n  async request(call: { request: unknown }) { return call.request }\n}\n',
  'echo.ts': (
    "export const Request = { parse: (v: any) => ({ ...v, seen: { big: typeof v.big, small: typeof v.small } }) }\n"
    "export const Response = { dump: (v: unknown) => v }\n"
  ),
}


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not on PATH')
def test_an_integer_past_2_53_keeps_its_digits_into_the_ts_client_and_back(tmp_path: Path):
  for name, text in ECHO_PACKAGE.items():
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(text)
  arguments = {'big': BIG, 'negative': -BIG, 'small': 7, 'real': 1.5, 'huge': 1e300}
  job = {'packageDir': str(tmp_path), 'root': 'Echo', 'function': 'echo', 'kind': 'rpc', 'arguments': arguments, 'options': {}}
  script = files('truewire').joinpath('resources', 'call.mjs')
  # The job is one line, and stdin stays open while the call runs: its end stops the driver.
  process = subprocess.Popen(
    ['node', '--experimental-transform-types', '--no-warnings', str(script)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
  )
  assert process.stdin is not None and process.stdout is not None
  process.stdin.write(json.dumps(job) + '\n')
  process.stdin.flush()
  line = process.stdout.readline()
  stdout, stderr = process.communicate(timeout=60)
  driven = subprocess.CompletedProcess(process.args, process.returncode, line + stdout, stderr)
  assert driven.returncode == 0, driven.stdout + driven.stderr
  assert json.loads(driven.stdout) == {'result': {**arguments, 'seen': {'big': 'bigint', 'small': 'number'}}}
  assert f'"big":{BIG},"negative":-{BIG},' in driven.stdout  # digits, not a string


DATE_PACKAGE = {
  'index.ts': (
    "import * as when from './when.js'\n"
    "export class When {\n"
    "  constructor(private core: any) {}\n"
    "  when() { return this.core.request({ responseCodec: when.Response }) }\n"
    "}\n"
  ),
  'core/index.ts': (
    'export class Core {\n'
    '  async request(_call: unknown) { return { at: new Date(Date.UTC(2026, 8, 7, 0, 12, 2)) } }\n'
    '}\n'
  ),
  'when.ts': "export const Response = { dump: (v: { at: Date }) => ({ at: v.at.toISOString().replace('.000Z', 'Z') }) }\n",
}


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not on PATH')
def test_a_result_goes_out_through_the_codec_the_method_handed_its_core(tmp_path: Path):
  # The codec is recorded during the call, so it is read after it: a `Date` the codec
  # did not dump would print as `toISOString`'s `2026-09-07T00:12:02.000Z`.
  for name, text in DATE_PACKAGE.items():
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(text)
  job = {'packageDir': str(tmp_path), 'root': 'When', 'function': 'when', 'kind': 'rpc', 'arguments': {}, 'options': {}}
  process = subprocess.Popen(
    ['node', '--experimental-transform-types', '--no-warnings', str(files('truewire').joinpath('resources', 'call.mjs'))],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
  )
  assert process.stdin is not None and process.stdout is not None
  process.stdin.write(json.dumps(job) + '\n')
  process.stdin.flush()
  line = process.stdout.readline()
  stdout, stderr = process.communicate(timeout=60)
  assert process.returncode == 0, line + stdout + stderr
  assert json.loads(line) == {'result': {'at': '2026-09-07T00:12:02Z'}}


SWITCH_PACKAGE = {
  'index.ts': (
    "import * as at from './at.js'\n"
    "export class Switch {\n"
    "  constructor(private core: any) {}\n"
    "  get(options?: { validate?: boolean }) {\n"
    "    return this.core.request({ responseCodec: at.Value, validate: options?.validate })\n"
    "  }\n"
    "  watch(options?: { validate?: boolean }) {\n"
    "    return this.core.subscribe({ replyCodec: at.Value, messageCodec: at.Value, validate: options?.validate })\n"
    "  }\n"
    "}\n"
  ),
  # Decodes as a core does, `call.validate ?? this.validate`: a `Date`, else the raw string.
  'core/index.ts': (
    "const value = (on: boolean) => ({ at: on ? new Date(Date.UTC(2026, 8, 7, 0, 12, 2)) : '2026-09-07T00:12:02Z' })\n"
    "export class Core {\n"
    "  readonly validate: boolean\n"
    "  constructor(options: { validate?: boolean } = {}) { this.validate = options.validate ?? true }\n"
    "  async request(call: { validate?: boolean }) { return value(call.validate ?? this.validate) }\n"
    "  subscribe(call: { validate?: boolean }) {\n"
    "    const on = call.validate ?? this.validate\n"
    "    return { open: async () => ({ reply: value(on), async *[Symbol.asyncIterator]() { yield value(on) } }) }\n"
    "  }\n"
    "}\n"
  ),
  'at.ts': (
    "export const Value = { dump: (v: { at: Date }) => {\n"
    "  if (!(v.at instanceof Date)) throw new TypeError('dump takes a Date')\n"
    "  return { at: v.at.toISOString().replace('.000Z', 'Z') }\n"
    "} }\n"
  ),
}


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not on PATH')
@pytest.mark.parametrize(('options', 'arguments'), [
  ({}, {}),
  ({'validate': False}, {}),
  ({}, {'validate': False}),
  ({'validate': False}, {'validate': True}),
], ids=['default', 'core-off', 'call-off', 'call-on-over-core-off'])
@pytest.mark.parametrize('kind', ['rpc', 'stream'])
def test_a_value_is_dumped_only_when_the_core_decoded_it(tmp_path: Path, kind: str, options: dict, arguments: dict):
  # The call's own `validate`, else the core's, decides: a decoded `Date` goes through the
  # codec, raw JSON (a string the codec refuses) is printed as the core returned it.
  for name, text in SWITCH_PACKAGE.items():
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(text)
  function = 'get' if kind == 'rpc' else 'watch'
  job = {'packageDir': str(tmp_path), 'root': 'Switch', 'function': function, 'kind': kind, 'arguments': arguments, 'options': options}
  driven = subprocess.run(
    ['node', '--experimental-transform-types', '--no-warnings', str(files('truewire').joinpath('resources', 'call.mjs'))],
    input=json.dumps(job) + '\n', capture_output=True, text=True, timeout=60,
  )
  assert driven.returncode == 0, driven.stdout + driven.stderr
  events = [json.loads(line) for line in driven.stdout.splitlines()]
  value = {'at': '2026-09-07T00:12:02Z'}
  assert events == ([{'result': value}] if kind == 'rpc' else [{'reply': value}, {'message': value}])


@pytest.mark.parametrize('validate', [(), ('--new', 'validate=false')], ids=['validated', 'raw'])
def test_a_github_timestamp_prints_as_recorded_with_validation_on_or_off(validate: tuple[str, ...]):
  # Validated, the core returns a `Date` the driver dumps through the response codec; with
  # `validate=false` it returns the raw JSON, a string the codec cannot dump, printed as is.
  if reason := github_ready('typescript'):
    pytest.skip(reason)
  with mock(GITHUB) as ready:
    called = truewire(
      'call', 'typescript', 'repos.get', 'owner=truewire-dev', 'repo=truewire', *validate,
      '--project', str(GITHUB), '--base-url', ready['http'],
    )
  assert called.returncode == 0, called.stderr
  assert json.loads(called.stdout)['created_at'] == '2026-09-07T00:12:02Z'


class Int64Rows(http.server.BaseHTTPRequestHandler):
  """Answers every GET with one GitHub issue that carries an undeclared `global_id` past 2^53."""
  def do_GET(self):
    example = json.loads((GITHUB / 'spec' / 'endpoints' / 'issues' / 'list' / 'examples' / 'page2.response.json').read_text())
    body = json.dumps([{**example['payload'][0], 'global_id': BIG}]).encode()
    self.send_response(200)
    self.send_header('Content-Type', 'application/json')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def log_message(self, format, *args):
    pass


def test_an_integer_past_2_53_in_a_response_prints_as_its_digits():
  if reason := github_ready('typescript'):
    pytest.skip(reason)
  server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Int64Rows)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    result = subprocess.run(
      [sys.executable, '-m', 'truewire.cli', 'call', 'typescript', 'issues.list', 'owner=o', 'repo=r',
       '--base-url', f'http://127.0.0.1:{server.server_address[1]}', '--project', str(GITHUB)],
      capture_output=True, text=True, timeout=300, env=environment(),
    )
  finally:
    server.shutdown()
    server.server_close()
  assert result.returncode == 0, result.stderr
  assert json.loads(result.stdout)[0]['global_id'] == BIG
  assert f'"global_id": {BIG}' in result.stdout


def test_each_field_set_gets_its_own_rust_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
  project = tmp_path / 'github'
  shutil.copytree(GITHUB, project, ignore=shutil.ignore_patterns('node_modules', 'target', '.truewire'))
  builds: list[list[str]] = []
  monkeypatch.setattr('truewire.call.native.shutil.which', lambda _: '/bin/cargo')
  monkeypatch.setattr('truewire.call.native.subprocess.run', lambda argv, **_: builds.append(argv) or subprocess.CompletedProcess(argv, 0, '', ''))
  monkeypatch.delenv('CARGO_TARGET_DIR', raising=False)
  loaded = load_project(project)
  crate = package_directory(loaded, 'rust')
  job = {'function': 'issues.list', 'kind': 'rpc', 'arguments': {}, 'options': {'base_url': 'http://mock', 'token': 'B'}}
  both = build_rust_driver(loaded, crate, job)
  url_only = build_rust_driver(loaded, crate, {**job, 'options': {'base_url': 'http://mock'}})
  assert both != url_only
  assert both.parent == url_only.parent == crate / 'target' / 'debug'
  manifests = [Path(argv[argv.index('--manifest-path') + 1]) for argv in builds]
  assert manifests[0] != manifests[1]
  assert f'name = "{both.name}"' in manifests[0].read_text()
  assert 'options.token' in (manifests[0].parent / 'src' / 'main.rs').read_text()
  assert 'options.token' not in (manifests[1].parent / 'src' / 'main.rs').read_text()
  assert build_rust_driver(loaded, crate, {**job, 'arguments': {'owner': 'a'}}) == both  # the same source, the same build


@pytest.mark.parametrize('text', ['NaN', 'Infinity', '-Infinity', '[1, NaN]'])
def test_a_non_finite_number_stays_a_string_for_the_codec_to_refuse(text: str):
  assert wire_value(text, {'type': 'integer'}, {}) == text


def test_a_union_request_gives_each_name_every_branch_s_schema():
  shared = {'ByName': {'type': 'object', 'properties': {'q': {'type': 'string'}, 'sort': {'type': 'boolean'}}}}
  schema = {'anyOf': [
    {'type': 'object', 'properties': {'repository_id': {'type': 'integer'}, 'q': {'type': 'integer'}}},
    {'$ref': 'ByName'},
  ]}
  properties = argument_properties(schema, shared)
  assert properties['repository_id'] == {'type': 'integer'}
  assert [wire_value(text, properties[name], shared) for name, text in (('repository_id', '7'), ('q', '42'), ('sort', 'true'))] == [
    7, '42', True,
  ]


def test_node_version():
  assert node_version('v22.14.0') == (22, 14) < (22, 15) <= node_version('v24.21.0')
  assert node_version('') == (0, 0)


def kraken_ready(language: str) -> str | None:
  """Why `examples/kraken` cannot run `language` here, or `None` when it can."""
  if language == 'typescript':
    if shutil.which('node') is None:
      return 'node is not on PATH'
    if not (KRAKEN / 'node_modules').is_dir():
      return 'examples/kraken has no node_modules'
  elif shutil.which('cargo') is None:
    return 'cargo is not on PATH'
  return None


def running(pid: int) -> bool:
  """Whether `pid` is alive and not a zombie."""
  try:
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
  except OSError:
    return False


def child_pids(pid: int) -> list[int]:
  pids = []
  for stat in Path('/proc').glob('[0-9]*/stat'):
    try:
      if int(stat.read_text().rsplit(')', 1)[1].split()[1]) == pid:
        pids.append(int(stat.parent.name))
    except (OSError, ValueError, IndexError):
      continue
  return pids


@pytest.mark.skipif(not Path('/proc/self/stat').is_file(), reason='reads processes from /proc')
@pytest.mark.parametrize('signum', [signal.SIGHUP, signal.SIGKILL])
@pytest.mark.parametrize('language', ['typescript', 'rust'])
def test_the_driver_does_not_outlive_the_cli(language: str, signum: signal.Signals):
  """The driver runs in its own session, so a closed terminal never reaches it, and a
  killed CLI forwards nothing: the end of its stdin is what stops it (TRU-244)."""
  if reason := kraken_ready(language):
    pytest.skip(reason)
  with mock(KRAKEN) as ready:
    cli = subprocess.Popen(
      [sys.executable, '-m', 'truewire.cli', 'call', language, 'streams.market_data.ticker', 'symbol=["BTC/USD"]',
       '--ws-url', ready['ws'], '--project', str(KRAKEN)],
      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=environment(),
      # As from a terminal: under `nohup` (a detached suite) SIGHUP would be inherited ignored.
      preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL),
    )
    drivers: list[int] = []
    try:
      assert cli.stdout is not None
      assert cli.stdout.readline()  # the reply: subscribed
      drivers = child_pids(cli.pid)
      assert drivers
      cli.send_signal(signum)
      cli.wait(timeout=30)
      deadline = time.monotonic() + 5
      while any(map(running, drivers)) and time.monotonic() < deadline:
        time.sleep(0.05)
      assert not any(map(running, drivers)), f'{signum.name}: the driver outlived the CLI'
    finally:
      if cli.poll() is None:
        cli.kill()
      for pid in filter(running, drivers):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize('language', ['typescript', 'rust'])
def test_sigterm_stops_a_call_started_with_sigint_ignored(language: str):
  """A script's `&` job starts with SIGINT ignored. The driver gets SIGINT back at its
  default, so the SIGINT the CLI forwards on SIGTERM stops a call still waiting (TRU-251)."""
  if reason := github_ready(language):
    pytest.skip(reason)
  with socket.create_server(('127.0.0.1', 0)) as server:
    server.settimeout(900)  # the first `call rust` builds the driver
    call = subprocess.Popen(
      [sys.executable, '-m', 'truewire.cli', 'call', language, 'repos.get', 'owner=a', 'repo=b',
       '--base-url', f'http://127.0.0.1:{server.getsockname()[1]}', '--project', str(GITHUB)],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment(),
      preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
    )
    try:
      connection, _ = server.accept()
      with connection:  # the request arrived and is never answered
        call.send_signal(signal.SIGTERM)
        assert call.wait(timeout=30) == 130
    finally:
      if call.poll() is None:
        call.kill()
      call.communicate(timeout=60)
