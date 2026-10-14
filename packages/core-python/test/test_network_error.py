"""
Pin what a failed send's `NetworkError` says.

`httpx.ReadTimeout` and its siblings usually carry an empty message, and `HttpClient.request`
used to pass the HTTPX error's `args` through as they were, so a timeout read
`NetworkError(Error sending request to GET <url>, )` and only `__cause__` told what happened
(TRU-1064). The message now names the HTTPX error's kind when it has none of its own; the
cause chain stays.
"""
import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from truewire_core.exceptions import NetworkError
from truewire_core.http import HttpClient


@pytest.fixture
async def silent_server() -> AsyncIterator[str]:
  """A local server that accepts connections and never answers; yields its base URL."""
  held: list[asyncio.StreamWriter] = []

  async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    held.append(writer)

  server = await asyncio.start_server(accept, '127.0.0.1', 0)
  port = server.sockets[0].getsockname()[1]
  try:
    yield f'http://127.0.0.1:{port}'
  finally:
    for writer in held:
      writer.close()
    server.close()
    await server.wait_closed()


async def test_a_read_timeout_names_itself(silent_server: str):
  """A server that never answers: the message says it timed out, the cause is the `ReadTimeout`."""
  async with HttpClient() as client:
    with pytest.raises(NetworkError) as info:
      await client.request('GET', f'{silent_server}/radar/queues/rds', timeout=0.2)
  err = info.value
  assert isinstance(err.__cause__, httpx.ReadTimeout)
  assert 'timed out (ReadTimeout)' in str(err)
  assert f'GET {silent_server}/radar/queues/rds' in str(err)
  assert not str(err).endswith(', )')


def raising(exc: httpx.HTTPError) -> HttpClient:
  """An `HttpClient` whose every send raises `exc`."""
  def handler(request: httpx.Request) -> httpx.Response:
    raise exc
  return HttpClient(_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize('exc, reason', [
  (httpx.ConnectTimeout(''), 'timed out (ConnectTimeout)'),
  (httpx.ConnectError(''), 'ConnectError'),
  (httpx.RemoteProtocolError(''), 'RemoteProtocolError'),
])
async def test_an_empty_httpx_error_names_its_kind(exc: httpx.HTTPError, reason: str):
  """Both branches, the connect one and the rest, fill an empty message with the error's kind."""
  async with raising(exc) as client:
    with pytest.raises(NetworkError) as info:
      await client.request('GET', 'http://example.invalid/x')
  assert info.value.__cause__ is exc
  assert str(info.value) == f'NetworkError(Error sending request to GET http://example.invalid/x, {reason})'


async def test_an_httpx_message_is_kept():
  """An HTTPX error that says something is passed through as before."""
  exc = httpx.ConnectError('All connection attempts failed')
  async with raising(exc) as client:
    with pytest.raises(NetworkError) as info:
      await client.request('GET', 'http://example.invalid/x')
  assert info.value.__cause__ is exc
  assert str(info.value) == 'NetworkError(Error sending request to GET http://example.invalid/x, All connection attempts failed)'
