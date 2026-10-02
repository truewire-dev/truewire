"""
Pin packages clause P18: an explicit `proxy=` carries HTTP and WebSocket traffic alike.

A caller that cannot set the process environment (an agent sandbox, a library embedding
the client) must still be able to route both transports through a proxy. So every test
here clears the proxy variables first, starts a local proxy that records what it was
asked to reach, and gives it to the client only as `proxy=`.

The proxy speaks the two forms an HTTP proxy is sent: `CONNECT host:port` (what websockets
sends for any URL, and httpx for `https://`) and an absolute-form request line such as
`GET http://host:port/path` (what httpx sends for a plain `http://` URL). The upstream is
one `websockets` server that answers a plain GET on `/hello` and runs a subscribe dialect
on every WebSocket connection.
"""
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing_extensions import Any
from urllib.parse import urlsplit
import asyncio
import contextlib
import json
import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from truewire_core.exceptions import NetworkError
from truewire_core.http import HttpClient
from truewire_core.ws import SerialReplies, Streams
from truewire_core.ws.streams import Subscription

PROXY_VARIABLES = (
  'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'WS_PROXY', 'WSS_PROXY', 'SOCKS_PROXY',
)

@pytest.fixture(autouse=True)
def no_proxy_environment(monkeypatch: pytest.MonkeyPatch):
  """Nothing in the environment names a proxy, in either case."""
  for name in PROXY_VARIABLES:
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(name.lower(), raising=False)

@dataclass
class Proxy:
  """A local HTTP proxy that tunnels `CONNECT` and forwards absolute-form requests."""
  seen: list[tuple[str, str]] = field(default_factory=list)
  """`(method, target)` of every request line, in arrival order."""
  url: str = ''

  async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
      head = await reader.readuntil(b'\r\n\r\n')
      line, _, rest = head.partition(b'\r\n')
      method, target, version = line.decode().split(' ')
      self.seen.append((method, target))
      if method == 'CONNECT':
        host, port = target.rsplit(':', 1)
        upstream_reader, upstream_writer = await asyncio.open_connection(host, int(port))
        writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        await writer.drain()
      else:
        parts = urlsplit(target)
        upstream_reader, upstream_writer = await asyncio.open_connection(parts.hostname, parts.port)
        path = parts.path + (f'?{parts.query}' if parts.query else '')
        upstream_writer.write(f'{method} {path} {version}\r\n'.encode() + rest)
      await asyncio.gather(
        self.pipe(reader, upstream_writer),
        self.pipe(upstream_reader, writer),
      )
    except (asyncio.IncompleteReadError, ConnectionError):
      pass
    finally:
      writer.close()

  @staticmethod
  async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
      while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    except ConnectionError:
      pass
    finally:
      with contextlib.suppress(Exception):
        writer.close()

async def upstream_handler(ws: ServerConnection):
  """Ack each subscribe, then push two frames on its channel."""
  async for message in ws:
    frame = json.loads(message)
    if frame['type'] == 'subscribe':
      await ws.send(json.dumps({'type': 'subscribed', 'channel': frame['channel']}))
      for n in range(2):
        await ws.send(json.dumps({'channel': frame['channel'], 'n': n}))
    elif frame['type'] == 'unsubscribe':
      await ws.send(json.dumps({'type': 'unsubscribed', 'channel': frame['channel']}))

def plain_http(connection: ServerConnection, request: Request) -> Response | None:
  """Answer a GET on `/hello` as plain HTTP; anything else goes on to the WebSocket handshake."""
  if request.path == '/hello':
    return Response(200, 'OK', Headers({'Content-Type': 'text/plain', 'Content-Length': '5'}), b'hello')
  return None

@pytest.fixture
async def upstream() -> AsyncIterator[str]:
  """`host:port` of the upstream server."""
  async with serve(upstream_handler, '127.0.0.1', 0, process_request=plain_http) as server:
    host, port = next(iter(server.sockets)).getsockname()[:2]
    yield f'{host}:{port}'

@pytest.fixture
async def proxy() -> AsyncIterator[Proxy]:
  recorder = Proxy()
  server = await asyncio.start_server(recorder.handle, '127.0.0.1', 0)
  host, port = server.sockets[0].getsockname()[:2]
  recorder.url = f'http://{host}:{port}'
  async with server:
    yield recorder
    server.close()

@dataclass
class Connection(SerialReplies[dict[str, Any]], Streams[dict[str, Any], Mapping[str, Any], dict[str, Any], dict[str, Any]]):
  """The subscribe dialect `upstream_handler` speaks: acks by arrival order, pushes by channel."""

  async def send(self, msg: object):
    await (await self.ws).send(json.dumps(msg))

  async def request_subscription(self, channel: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return await self.request({'type': 'subscribe', 'channel': channel})

  async def request_unsubscription(self, channel: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return await self.request({'type': 'unsubscribe', 'channel': channel})

  def parse_msg(self, msg: str | bytes) -> Subscription[dict[str, Any]] | None:
    frame = json.loads(msg)
    if frame.get('type') in ('subscribed', 'unsubscribed'):
      self.replies.put_nowait(frame)
      return None
    return {'channel': frame['channel'], 'notification': frame}

async def test_http_request_goes_through_the_given_proxy(upstream: str, proxy: Proxy):
  async with HttpClient(proxy=proxy.url) as http:
    response = await http.request('GET', f'http://{upstream}/hello')
  assert (response.status_code, response.text) == (200, 'hello')
  assert proxy.seen == [('GET', f'http://{upstream}/hello')]

async def test_websocket_subscription_goes_through_the_given_proxy(upstream: str, proxy: Proxy):
  async with Connection(f'ws://{upstream}/', proxy=proxy.url) as conn:
    async with conn.subscribe('ticker') as stream:
      frames = [await anext(aiter(stream)) for _ in range(2)]
  assert frames == [{'channel': 'ticker', 'n': 0}, {'channel': 'ticker', 'n': 1}]
  assert proxy.seen == [('CONNECT', upstream)]

async def test_one_proxy_sees_both_transports(upstream: str, proxy: Proxy):
  """The P18 check: one client configuration, both transports, one proxy, no environment."""
  async with HttpClient(proxy=proxy.url) as http, Connection(f'ws://{upstream}/', proxy=proxy.url) as conn:
    assert (await http.request('GET', f'http://{upstream}/hello')).text == 'hello'
    async with conn.subscribe('trades') as stream:
      first = await anext(aiter(stream))
  assert first == {'channel': 'trades', 'n': 0}
  assert sorted(proxy.seen) == [('CONNECT', upstream), ('GET', f'http://{upstream}/hello')]

async def both(upstream: str, **client: Any):
  """One HTTP GET and one subscription, each transport built with `client`'s keywords."""
  async with HttpClient(**client) as http, Connection(f'ws://{upstream}/', **client) as conn:
    assert (await http.request('GET', f'http://{upstream}/hello')).text == 'hello'
    async with conn.subscribe('ticker') as stream:
      await anext(aiter(stream))

@pytest.mark.parametrize('client', [{}, {'proxy': ''}], ids=['none', 'empty'])
async def test_without_a_proxy_nothing_reaches_it(upstream: str, proxy: Proxy, client: dict[str, Any]):
  """No proxy and an empty environment connect directly: the recorder stays empty."""
  await both(upstream, **client)
  assert proxy.seen == []

@pytest.mark.parametrize('variable', ['HTTP_PROXY', 'http_proxy'])
async def test_without_a_proxy_the_environment_is_the_fallback(upstream: str, proxy: Proxy, monkeypatch: pytest.MonkeyPatch, variable: str):
  """`proxy=None` behaves as before this keyword existed: both transports read the environment."""
  monkeypatch.setenv(variable, proxy.url)
  await both(upstream)
  assert sorted(proxy.seen) == [('CONNECT', upstream), ('GET', f'http://{upstream}/hello')]

async def test_without_a_proxy_no_proxy_is_honoured(upstream: str, proxy: Proxy, monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setenv('HTTP_PROXY', proxy.url)
  monkeypatch.setenv('NO_PROXY', '127.0.0.1')
  await both(upstream)
  assert proxy.seen == []

async def test_an_explicit_proxy_ignores_no_proxy(upstream: str, proxy: Proxy, monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost,*')
  await both(upstream, proxy=proxy.url)
  assert sorted(proxy.seen) == [('CONNECT', upstream), ('GET', f'http://{upstream}/hello')]

async def test_an_explicit_proxy_beats_the_environment(upstream: str, proxy: Proxy, monkeypatch: pytest.MonkeyPatch):
  """The environment names a dead port; the explicit proxy is the one used."""
  monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:9')
  await both(upstream, proxy=proxy.url)
  assert sorted(proxy.seen) == [('CONNECT', upstream), ('GET', f'http://{upstream}/hello')]

def test_a_proxy_keeps_no_connection_alive():
  """An explicit proxy gets the same limits an environment one always did."""
  from truewire_core.http import _default_limits
  assert _default_limits('http://127.0.0.1:1').max_keepalive_connections == 0
  assert _default_limits(None).max_keepalive_connections != 0
  assert _default_limits('').max_keepalive_connections != 0

async def closed_port() -> int:
  """A local port nothing listens on, so connecting to it is refused."""
  closed = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
  port = closed.sockets[0].getsockname()[1]
  closed.close()
  await closed.wait_closed()
  return port

async def test_an_unreachable_proxy_is_a_connection_failure(upstream: str):
  """The proxy is really used: pointing it at a closed port fails the WebSocket connect."""
  conn = Connection(f'ws://{upstream}/', proxy=f'http://127.0.0.1:{await closed_port()}')
  with pytest.raises(NetworkError) as info:
    await conn.open()
  assert isinstance(info.value.__cause__, ConnectionRefusedError)

async def test_a_refused_direct_connect_is_a_connection_failure():
  """Without a proxy, a refused WebSocket connect is a `NetworkError` too, as on HTTP."""
  conn = Connection(f'ws://127.0.0.1:{await closed_port()}/')
  with pytest.raises(NetworkError) as info:
    await conn.open()
  assert isinstance(info.value.__cause__, ConnectionRefusedError)
