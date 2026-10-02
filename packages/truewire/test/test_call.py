"""`truewire call python`: one call, or one subscription, through the generated client,
against `truewire mock` running as its own process."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.call import (
  UNSUBSCRIBE_TIMEOUT, CallError, build_client, client_kwargs, read_dotenv, secret_parameter,
  secret_values,
)
from truewire.cli import app
from truewire.mcp import load_root
from truewire.project import resolve

FIXTURES = Path(__file__).parent / 'fixtures'
TIMEOUT = 60


def truewire(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
  """Run the CLI as its own process, the way a person does."""
  return subprocess.run(
    [sys.executable, '-m', 'truewire.cli', *args],
    capture_output=True, text=True, timeout=TIMEOUT, env=environment(), **kwargs,
  )


def environment() -> dict[str, str]:
  """This interpreter's environment, with this checkout's `truewire` first on the path."""
  src = str(Path(__file__).resolve().parents[1] / 'src')
  path = os.environ.get('PYTHONPATH')
  return {**os.environ, 'PYTHONPATH': f'{src}{os.pathsep}{path}' if path else src}


@contextmanager
def mock(project: Path) -> Iterator[dict[str, str]]:
  """`truewire mock --json` on free ports; yields its ready line."""
  process = subprocess.Popen(
    [sys.executable, '-m', 'truewire.cli', 'mock', '--project', str(project), '--json',
     '--http-port', '0', '--ws-port', '0'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment(),
  )
  try:
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    assert ready['event'] == 'ready', ready
    yield ready
  finally:
    process.send_signal(signal.SIGINT)
    try:
      process.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
      process.kill()


@pytest.fixture(scope='module')
def petstore(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """A generated petstore project on the default (bearer) core."""
  project = tmp_path_factory.mktemp('call') / 'petstore'
  runner = CliRunner()
  assert runner.invoke(app, ['init', 'petstore', '--dir', str(project)]).exit_code == 0
  imported = runner.invoke(app, ['import', 'openapi', str(FIXTURES / 'openapi' / 'petstore.yaml'), '--project', str(project)])
  assert imported.exit_code == 0, imported.output
  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  return project


@pytest.fixture(scope='module')
def feed(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """A generated project on the `ws` core with one recorded stream, `streams.trades`."""
  project = tmp_path_factory.mktemp('call') / 'feed'
  runner = CliRunner()
  initialized = runner.invoke(app, ['init', 'feed', '--dir', str(project), '--template', 'ws'])
  assert initialized.exit_code == 0, initialized.output
  shutil.copytree(FIXTURES / 'init_templates' / 'ws' / 'spec', project / 'spec', dirs_exist_ok=True)
  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  return project


def recorded(project: Path, *parts: str) -> dict:
  """One recorded file under the project's endpoint tree."""
  return json.loads(project.joinpath('spec', 'endpoints', *parts).read_text())


def test_an_rpc_call_prints_the_recorded_response(petstore: Path):
  with mock(petstore) as ready:
    result = truewire('call', 'python', 'pets.get_pet', 'petId=42', '--base-url', ready['http'], '--project', str(petstore))
  assert result.returncode == 0, result.stderr
  expected = recorded(petstore, 'pets', 'get_pet', 'examples', 'default.response.json')['payload']
  assert json.loads(result.stdout) == expected
  assert expected['created_at'].endswith('Z')  # dumped through the type, as the wire writes it


def test_a_missing_required_argument_exits_1_naming_it(petstore: Path):
  result = truewire('call', 'python', 'pets.get_pet', '--base-url', 'http://127.0.0.1:9', '--project', str(petstore))
  assert result.returncode == 1
  assert 'missing required argument petId' in result.stderr
  assert result.stdout == ''


def test_an_unknown_argument_or_endpoint_or_language_exits_1(petstore: Path):
  unknown = truewire('call', 'python', 'pets.get_pet', 'petId=42', 'colour=red', '--project', str(petstore))
  assert unknown.returncode == 1
  assert 'unknown argument colour' in unknown.stderr
  endpoint = truewire('call', 'python', 'pets.nope', '--project', str(petstore))
  assert endpoint.returncode == 1
  assert "no endpoint with function 'pets.nope'" in endpoint.stderr
  language = truewire('call', 'go', 'pets.get_pet', 'petId=42', '--project', str(petstore))
  assert language.returncode == 1
  assert 'call supports python, typescript, rust, not go' in language.stderr
  undeclared = truewire('call', 'typescript', 'pets.get_pet', 'petId=42', '--project', str(petstore))
  assert undeclared.returncode == 1
  assert 'declares no [typescript] package' in undeclared.stderr


def test_a_subscription_prints_the_reply_and_messages_and_unsubscribes_on_sigint(feed: Path):
  with mock(feed) as ready:
    assert ready['ws'] is not None
    process = subprocess.Popen(
      [sys.executable, '-m', 'truewire.cli', 'call', 'python', 'streams.trades', 'id=ACME-1',
       '--base-url', ready['http'], '--ws-url', ready['ws'], '--project', str(feed)],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment(),
    )
    try:
      assert process.stdout is not None
      lines = [json.loads(process.stdout.readline()) for _ in range(3)]
      process.send_signal(signal.SIGINT)
      rest, stderr = process.communicate(timeout=TIMEOUT)
    finally:
      if process.poll() is None:
        process.kill()
  examples = ('streams', 'trades', 'examples')
  assert lines[0] == recorded(feed, *examples, 'default.reply.json')
  assert lines[1:] == recorded(feed, *examples, 'default.messages.json')
  assert process.returncode == 0, stderr
  assert rest == ''
  # The mock answered the unsubscribe frame with the recorded unsubscribe reply.
  unsubscribed = recorded(feed, *examples, 'default.unsubscribe_reply.json')
  assert f'unsubscribed: {json.dumps(unsubscribed)}' in stderr


def test_secret_names_map_to_the_keyword_they_end_with():
  keywords = ['base_url', 'api_key', 'api_secret', 'token', 'validate']
  assert secret_parameter('PETSTORE_API_KEY', keywords) == 'api_key'
  assert secret_parameter('API_SECRET', keywords) == 'api_secret'
  assert secret_parameter('GITHUB_TOKEN', keywords) == 'token'
  assert secret_parameter('PETSTORE_KEY', keywords) is None


def test_dotenv_reads_names_values_quotes_and_comments(tmp_path: Path):
  (tmp_path / '.env').write_text(
    '# local only\nexport A=1\nB="two words"\nC=three # note\n\nD=\'x=y\'\nnot a pair\n'
  )
  assert read_dotenv(tmp_path / '.env') == {'A': '1', 'B': 'two words', 'C': 'three', 'D': 'x=y'}
  assert read_dotenv(tmp_path / 'absent') == {}


def with_secrets(petstore: Path, tmp_path: Path, table: str) -> Path:
  """A copy of the petstore project whose `[secrets]` is `table`."""
  project = tmp_path / 'petstore'
  shutil.copytree(petstore, project)
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('[secrets]\nrequired = []\n', f'[secrets]\n{table}\n'))
  return project


def test_secrets_come_from_the_environment_then_dotenv_into_new(petstore: Path, tmp_path: Path):
  project = with_secrets(petstore, tmp_path, 'required = ["PETSTORE_API_KEY"]')
  loaded = resolve(project)
  with pytest.raises(CallError, match='PETSTORE_API_KEY: listed in \\[secrets\\].required'):
    secret_values(loaded, environ={})
  (project / '.env').write_text('PETSTORE_API_KEY=from-dotenv\n')
  assert secret_values(loaded, environ={}) == {'PETSTORE_API_KEY': 'from-dotenv'}
  assert secret_values(loaded, environ={'PETSTORE_API_KEY': 'from-env'}) == {'PETSTORE_API_KEY': 'from-env'}
  client = build_client(loaded, 'python', base_url='http://127.0.0.1:9', environ={})
  assert client.client.api_key == 'from-dotenv'
  assert client.client.base_url == 'http://127.0.0.1:9'


def test_a_missing_required_secret_exits_1_before_any_request(petstore: Path, tmp_path: Path):
  project = with_secrets(petstore, tmp_path, 'required = ["PETSTORE_API_KEY"]')
  env = {k: v for k, v in environment().items() if k != 'PETSTORE_API_KEY'}
  result = subprocess.run(
    [sys.executable, '-m', 'truewire.cli', 'call', 'python', 'pets.get_pet', 'petId=42', '--project', str(project)],
    capture_output=True, text=True, timeout=TIMEOUT, env=env,
  )
  assert result.returncode == 1
  assert 'PETSTORE_API_KEY' in result.stderr


def test_a_secret_matching_no_keyword_is_refused_not_dropped(petstore: Path, tmp_path: Path):
  project = with_secrets(petstore, tmp_path, 'optional = ["PETSTORE_KEY"]')
  loaded = resolve(project)
  root = load_root(loaded)
  with pytest.raises(CallError, match=r'PETSTORE_KEY \(from \[secrets\]\) matches no keyword of Petstore.new\(\)'):
    client_kwargs(root, secrets={'PETSTORE_KEY': 'k'})
  assert client_kwargs(root, secrets={'PETSTORE_KEY': 'k'}, secret_map={'api_key': 'PETSTORE_KEY'}) == {'api_key': 'k'}
  (project / '.env').write_text('PETSTORE_KEY=from-dotenv\n')
  client = build_client(loaded, 'python', secret_map={'api_key': 'PETSTORE_KEY'}, environ={})
  assert client.client.api_key == 'from-dotenv'
  with pytest.raises(CallError, match='OTHER is not in \\[secrets\\]'):
    build_client(loaded, 'python', secret_map={'api_key': 'OTHER'}, environ={'OTHER': 'x'})


def test_two_secrets_for_one_keyword_are_refused_until_one_is_chosen():
  kraken = resolve(Path(__file__).resolve().parents[3] / 'examples' / 'kraken')
  root = load_root(kraken)
  secrets = {'KRAKEN_API_KEY': 'spot-key', 'KRAKEN_FUTURES_API_KEY': 'futures-key'}
  with pytest.raises(CallError, match=r'KRAKEN_API_KEY and KRAKEN_FUTURES_API_KEY .* both go to Kraken.new\(api_key=...\)'):
    client_kwargs(root, secrets=secrets)
  assert client_kwargs(root, secrets=secrets, secret_map={'api_key': 'KRAKEN_API_KEY'}) == {'api_key': 'spot-key'}
  # `validate` is a bool: a variable named `..._VALIDATE` is not a credential for it.
  with pytest.raises(CallError, match='SKIP_VALIDATE .* matches no keyword'):
    client_kwargs(root, secrets={'SKIP_VALIDATE': '1'})


def test_a_bad_value_names_its_argument(petstore: Path):
  result = truewire('call', 'python', 'pets.list_pets', 'limit=abc', '--base-url', 'http://127.0.0.1:9', '--project', str(petstore))
  assert result.returncode == 1
  assert result.stderr.splitlines()[0].startswith('pets.list_pets: invalid argument limit=abc: '), result.stderr
  assert 'errors.pydantic.dev' not in result.stderr


def test_a_python_name_is_told_to_use_the_api_name(petstore: Path):
  result = truewire('call', 'python', 'pets.get_pet', 'pet_id=42', '--project', str(petstore))
  assert result.returncode == 1
  assert 'missing required argument petId; unknown argument pet_id; call takes API names: petId' in result.stderr


def test_dotenv_drops_a_comment_after_a_quoted_value(tmp_path: Path):
  (tmp_path / '.env').write_text('API_KEY="abc" # prod\nB=\'x # y\'\n')
  assert read_dotenv(tmp_path / '.env') == {'API_KEY': 'abc', 'B': 'x # y'}


FAKE_WS = '''
import asyncio, json, sys
import websockets

MODE = sys.argv[1]
TRADE = {'type': 'update', 'channel': 'trades', 'id': 'ACME-1', 'data': {'price': '1', 'size': '1'}}

async def handler(ws):
  pushing = None

  async def push():
    while True:
      await ws.send(json.dumps(TRADE))
      await asyncio.sleep(0.05)

  async for raw in ws:
    frame = json.loads(raw)
    print(frame.get('type'), flush=True)
    if MODE == 'silent':
      continue  # nothing is ever answered
    if frame.get('type') == 'subscribe':
      await ws.send(json.dumps({'type': 'subscribed', 'channel': 'trades', 'id': 'ACME-1'}))
      if MODE == 'deaf':
        await ws.send(json.dumps(TRADE))
      else:
        pushing = asyncio.create_task(push())
    elif frame.get('type') == 'unsubscribe' and MODE == 'live':
      if pushing is not None:
        pushing.cancel()
      await ws.send(json.dumps({'type': 'unsubscribed', 'channel': 'trades', 'id': 'ACME-1'}))

async def main():
  async with websockets.serve(handler, '127.0.0.1', 0) as server:
    print(next(iter(server.sockets)).getsockname()[1], flush=True)
    await asyncio.Future()

asyncio.run(main())
'''


@contextmanager
def ws_server(tmp_path: Path, mode: str) -> Iterator[tuple[str, subprocess.Popen[str]]]:
  """A WebSocket server for `streams.trades` that answers nothing (`silent`); acks the
  subscribe, pushes once and never answers the unsubscribe (`deaf`); or pushes a trade
  every 50 ms until it is unsubscribed (`live`). Yields its URL and the process, which
  prints the `type` of each frame it receives."""
  script = tmp_path / 'fake_ws.py'
  script.write_text(FAKE_WS)
  server = subprocess.Popen([sys.executable, str(script), mode], stdout=subprocess.PIPE, text=True)
  try:
    assert server.stdout is not None
    yield f'ws://127.0.0.1:{server.stdout.readline().strip()}', server
  finally:
    server.kill()
    server.wait()


def interrupt(
  feed: Path, deaf: tuple[str, subprocess.Popen[str]], *, lines: int, signals: int,
) -> tuple[subprocess.Popen[str], str, float]:
  """Subscribe, wait until the server has the subscribe frame and `lines` lines are out,
  then send `signals` SIGINTs a second apart. Returns the process, its stderr, and the
  seconds it took to exit after the first SIGINT."""
  ws_url, server = deaf
  process = subprocess.Popen(
    [sys.executable, '-m', 'truewire.cli', 'call', 'python', 'streams.trades', 'id=ACME-1',
     '--base-url', 'http://127.0.0.1:9', '--ws-url', ws_url, '--project', str(feed)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment(),
  )
  try:
    assert process.stdout is not None
    assert server.stdout is not None
    assert server.stdout.readline().strip() == 'subscribe'
    for _ in range(lines):
      process.stdout.readline()
    started = time.monotonic()
    for n in range(signals):
      if n:
        time.sleep(1)
      process.send_signal(signal.SIGINT)
    _, stderr = process.communicate(timeout=UNSUBSCRIBE_TIMEOUT + 10)
    return process, stderr, time.monotonic() - started
  finally:
    if process.poll() is None:
      process.kill()
      process.wait()


def test_ctrl_c_before_the_subscribe_is_answered_stops_at_once(feed: Path, tmp_path: Path):
  with ws_server(tmp_path, 'silent') as deaf:
    process, stderr, seconds = interrupt(feed, deaf, lines=0, signals=1)
  assert process.returncode == 0, stderr
  assert seconds < 5


def test_an_unanswered_unsubscribe_gives_up_after_the_timeout(feed: Path, tmp_path: Path):
  with ws_server(tmp_path, 'deaf') as deaf:
    process, stderr, seconds = interrupt(feed, deaf, lines=2, signals=1)
  assert process.returncode == 1
  assert f'no unsubscribe reply within {UNSUBSCRIBE_TIMEOUT:g} s' in stderr
  assert seconds < UNSUBSCRIBE_TIMEOUT + 5


def test_a_second_ctrl_c_does_not_wait_for_the_unsubscribe(feed: Path, tmp_path: Path):
  with ws_server(tmp_path, 'deaf') as deaf:
    process, stderr, seconds = interrupt(feed, deaf, lines=2, signals=2)
  assert process.returncode == 130, stderr
  assert seconds < 5


def test_a_stream_piped_into_head_unsubscribes_and_exits_0_quietly(feed: Path, tmp_path: Path):
  with ws_server(tmp_path, 'live') as (url, server):
    process = subprocess.Popen(
      [sys.executable, '-m', 'truewire.cli', 'call', 'python', 'streams.trades', 'id=ACME-1',
       '--base-url', 'http://127.0.0.1:9', '--ws-url', url, '--project', str(feed)],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment(),
    )
    try:
      assert process.stdout is not None and process.stderr is not None
      process.stdout.readline()
      process.stdout.close()  # `| head -1`; the next push finds the pipe closed
      process.wait(timeout=TIMEOUT)
      stderr = process.stderr.read()
    finally:
      if process.poll() is None:
        process.kill()
        process.wait()
    assert server.stdout is not None
    received = [server.stdout.readline().strip() for _ in range(2)]
  assert process.returncode == 0, stderr
  assert 'BrokenPipeError' not in stderr
  assert 'unsubscribed: ' in stderr
  assert received == ['subscribe', 'unsubscribe']
