"""
Pin `HttpClient(rate=, retry=)`: the runtime side of `[policy].rate` and `[policy].retry`
(workspace clause W15).

Every test talks to a real local HTTP server that answers from a script, so pacing is
measured on requests that crossed a socket and a retry is a second request the server saw.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email.utils import format_datetime, parsedate_to_datetime
from datetime import datetime, timedelta, timezone
import asyncio
import socket
import time
import pytest
import httpx

from truewire_core.exceptions import NetworkError
from truewire_core.http import RETRY_ATTEMPTS, HttpClient, recording, retry_after


@dataclass
class Server:
  """Answers each request with the next `(status, headers)` of `script`, then 200s."""
  script: list[tuple[int, dict[str, str]]] = field(default_factory=list)
  starts: list[float] = field(default_factory=list)
  """`time.monotonic()` of every request's arrival, in order."""
  url: str = ''

  async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
      while True:
        try:
          await reader.readuntil(b'\r\n\r\n')
        except asyncio.IncompleteReadError:
          return
        self.starts.append(time.monotonic())
        status, headers = self.script.pop(0) if self.script else (200, {})
        lines = [f'HTTP/1.1 {status} X', 'Content-Length: 2', *(f'{k}: {v}' for k, v in headers.items())]
        writer.write(('\r\n'.join(lines) + '\r\n\r\nok').encode())
        await writer.drain()
    finally:
      writer.close()


@asynccontextmanager
async def serve(*script: tuple[int, dict[str, str]], port: int = 0) -> AsyncIterator[Server]:
  server = Server(script=list(script))
  listener = await asyncio.start_server(server.handle, '127.0.0.1', port)
  server.url = f'http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/'
  async with listener:
    yield server


def free_port() -> int:
  with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    return probe.getsockname()[1]


class TestRate:
  async def test_ten_calls_at_rate_5_take_at_least_1_8_s(self):
    async with serve() as server, HttpClient(rate=5) as http:
      began = time.monotonic()
      for _ in range(10):
        assert (await http.request('GET', server.url)).status_code == 200
      elapsed = time.monotonic() - began
    assert elapsed >= 1.8, elapsed
    assert len(server.starts) == 10

  async def test_concurrent_callers_share_the_pace(self):
    async with serve() as server, HttpClient(rate=5) as http:
      began = time.monotonic()
      replies = await asyncio.gather(*(http.request('GET', server.url) for _ in range(10)))
      elapsed = time.monotonic() - began
    assert [r.status_code for r in replies] == [200] * 10
    assert elapsed >= 1.8, elapsed

  async def test_a_fractional_rate_spaces_by_its_inverse(self):
    async with serve() as server, HttpClient(rate=2.5) as http:
      began = time.monotonic()
      for _ in range(3):
        await http.request('GET', server.url)
      assert time.monotonic() - began >= 0.8

  async def test_no_rate_sends_at_once(self):
    async with serve() as server, HttpClient() as http:
      began = time.monotonic()
      for _ in range(10):
        await http.request('GET', server.url)
      assert time.monotonic() - began < 1.0

  @pytest.mark.parametrize('rate', [0, -1, float('inf'), float('nan')])
  def test_a_rate_that_is_not_positive_and_finite_is_refused(self, rate: float):
    with pytest.raises(ValueError, match='rate must be a positive number'):
      HttpClient(rate=rate)


class TestRetry:
  async def test_503_then_200_succeeds_with_retry(self):
    async with serve((503, {}), (200, {})) as server, HttpClient(retry=True) as http:
      response = await http.request('GET', server.url)
    assert response.status_code == 200
    assert len(server.starts) == 2

  async def test_503_then_200_fails_without_retry(self):
    async with serve((503, {}), (200, {})) as server, HttpClient(retry=False) as http:
      response = await http.request('GET', server.url)
    assert response.status_code == 503
    assert len(server.starts) == 1

  async def test_429_is_retried_after_its_retry_after_seconds(self):
    async with serve((429, {'Retry-After': '1'})) as server, HttpClient(retry=True) as http:
      response = await http.request('POST', server.url, content=b'{}')
    assert response.status_code == 200
    assert server.starts[1] - server.starts[0] >= 0.95

  async def test_retry_after_as_an_http_date(self):
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=2), usegmt=True)
    deadline = time.monotonic() + parsedate_to_datetime(when).timestamp() - time.time()
    async with serve((503, {'Retry-After': when})) as server, HttpClient(retry=True) as http:
      response = await http.request('GET', server.url)
    assert response.status_code == 200
    # Check the absolute server deadline; client construction can consume part of the wait.
    assert server.starts[1] >= deadline - 0.05

  async def test_a_retry_after_over_the_cap_returns_the_reply(self):
    async with serve((503, {'Retry-After': '3600'})) as server, HttpClient(retry=True) as http:
      response = await http.request('GET', server.url)
    assert response.status_code == 503
    assert len(server.starts) == 1

  async def test_attempts_are_bounded(self):
    script = [(503, {'Retry-After': '0'})] * (RETRY_ATTEMPTS + 1)
    async with serve(*script) as server, HttpClient(retry=True) as http:
      response = await http.request('GET', server.url)
    assert response.status_code == 503
    assert len(server.starts) == RETRY_ATTEMPTS

  @pytest.mark.parametrize('status', [500, 502, 504, 401, 404])
  async def test_other_statuses_are_not_retried(self, status: int):
    async with serve((status, {'Retry-After': '0'})) as server, HttpClient(retry=True) as http:
      response = await http.request('GET', server.url)
    assert response.status_code == status
    assert len(server.starts) == 1

  async def test_every_attempt_is_recorded(self):
    async with serve((503, {'Retry-After': '0'})) as server, HttpClient(retry=True) as http:
      with recording() as exchanges:
        await http.request('GET', server.url)
    assert [x.response.status_code for x in exchanges] == [503, 200]

  async def test_retries_are_paced(self):
    async with serve((503, {'Retry-After': '0'})) as server, HttpClient(rate=2, retry=True) as http:
      began = time.monotonic()
      await http.request('GET', server.url)
      # Timed at the caller: pacing spaces the sends, and a late first arrival shortens the
      # gap the server sees.
      assert time.monotonic() - began >= 0.49
    assert len(server.starts) == 2

  async def test_a_refused_connection_is_retried(self):
    port = free_port()

    async def late_server():
      await asyncio.sleep(0.2)
      async with serve(port=port) as server:
        while not server.starts:
          await asyncio.sleep(0.05)

    task = asyncio.create_task(late_server())
    async with HttpClient(retry=True) as http:
      response = await http.request('GET', f'http://127.0.0.1:{port}/')
    await task
    assert response.status_code == 200

  async def test_a_refused_connection_raises_without_retry(self):
    async with HttpClient() as http:
      with pytest.raises(NetworkError):
        await http.request('GET', f'http://127.0.0.1:{free_port()}/')


async def test_cancelled_sleeping_and_queued_waiters_release_slots():
  async with serve() as server, HttpClient(rate=2) as http:
    await http.request('GET', server.url)
    waiters = [asyncio.create_task(http.request('GET', server.url)) for _ in range(4)]
    await asyncio.sleep(0.05)
    for waiter in waiters:
      waiter.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)
    began = time.monotonic()
    await http.request('GET', server.url)
    assert time.monotonic() - began < 0.7
    assert len(server.starts) == 2
    assert server.starts[1] - server.starts[0] >= 0.45


@pytest.mark.parametrize('status', [303, 307])
async def test_default_redirect_policy_does_not_repeat_a_post_after_connect_failure(status):
  target = f'http://127.0.0.1:{free_port()}/'
  async with serve((status, {'Location': target})) as server, HttpClient(retry=True) as http:
    client = await http.client
    client.follow_redirects = True
    with pytest.raises(NetworkError):
      await http.request('POST', server.url)
    assert len(server.starts) == 1


async def test_redirected_503_does_not_repeat_the_original_post():
  async with serve((503, {'Retry-After': '0'})) as target:
    async with serve((303, {'Location': target.url})) as origin, HttpClient(retry=True) as http:
      response = await http.request('POST', origin.url, follow_redirects=True)
      assert response.status_code == 503
      assert len(origin.starts) == len(target.starts) == 1


def test_superscript_retry_after_is_unreadable():
  response = httpx.Response(503, headers=[(b'Retry-After', b'\xb2')])
  assert retry_after(response) is None
