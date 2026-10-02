"""Review repros for PR #31 (TRU-270): `HttpClient(rate=, retry=)` and the cores that use it.

Every test asserts the behaviour the review asks for, so each one fails at f7311cc4.
"""
import asyncio
import re
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

from truewire.cli.core_templates import HMAC_CORE
from truewire_core.exceptions import AuthError
from truewire_core.http import HttpClient


REPO = Path(__file__).resolve().parents[3]


def hmac_template() -> types.ModuleType:
  """The `hmac` init template's core, loaded as a module (its `..meta` import stubbed)."""
  source = HMAC_CORE.replace('$package', 'demo').replace('from ..meta import DefaultMeta as Meta', 'Meta = dict')
  module = types.ModuleType('hmac_core')
  exec(compile(source, 'hmac_core.py', 'exec'), module.__dict__)
  return module


class Server:
  """Answers `script`'s `(status, headers)` in order, then 200; records each request's
  arrival time and raw head+body."""

  def __init__(self, *script: tuple[int, dict[str, str]]):
    self.script = list(script)
    self.seen: list[tuple[float, bytes]] = []
    self.url = ''

  async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
      while True:
        try:
          head = await reader.readuntil(b'\r\n\r\n')
        except asyncio.IncompleteReadError:
          return
        length = re.search(rb'(?i)content-length: (\d+)', head)
        body = await reader.readexactly(int(length[1])) if length else b''
        self.seen.append((time.time(), head + body))
        status, headers = self.script.pop(0) if self.script else (200, {})
        lines = [f'HTTP/1.1 {status} X', 'Content-Length: 2', *(f'{k}: {v}' for k, v in headers.items())]
        writer.write('\r\n'.join(lines).encode('latin-1') + b'\r\n\r\n{}')
        await writer.drain()
    finally:
      writer.close()

  async def __aenter__(self):
    self.listener = await asyncio.start_server(self.handle, '127.0.0.1', 0)
    self.url = f'http://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}'
    return self

  async def __aexit__(self, *exc):
    self.listener.close()


def header(raw: bytes, name: str) -> str:
  return re.search(rf'(?im)^{name}: (.*?)\r$'.encode(), raw)[1].decode()


# 1. Pacing and retry run below the signature ------------------------------------------

async def test_hmac_template_paced_requests_are_signed_when_sent_not_when_queued():
  """rate=2, six concurrent signed calls: the last leaves ~2.5 s after its X-Timestamp was
  signed. A 1 s receive window (Binance and Bybit default to 5 s, at rate=1 that is the
  sixth queued call) refuses it."""
  core = hmac_template()
  async with Server() as server:
    transport = core.Transport(base_url=server.url, api_key='k', api_secret='s', http=HttpClient(rate=2))
    await asyncio.gather(*(
      transport.send('POST', '/order', params={}, body=b'{}', public=False) for _ in range(6)
    ))
  lags = [arrived - int(header(raw, 'X-Timestamp')) / 1000 for arrived, raw in server.seen]
  assert max(lags) < 1.0, [round(lag, 2) for lag in lags]


async def test_hmac_template_retry_signs_again():
  """A 429 with `Retry-After: 2` is retried with the first attempt's timestamp and signature."""
  core = hmac_template()
  async with Server((429, {'Retry-After': '2'})) as server:
    transport = core.Transport(base_url=server.url, api_key='k', api_secret='s', http=HttpClient(retry=True))
    await transport.send('POST', '/order', params={}, body=b'{}', public=False)
  stamps = [header(raw, 'X-Timestamp') for _, raw in server.seen]
  assert len(stamps) == 2
  assert stamps[0] != stamps[1], 'the retry re-sent a 2 s old signed timestamp'


async def test_kraken_retry_takes_a_fresh_nonce():
  """Kraken refuses a nonce not above the last one it accepted; the retry re-sends the old one."""
  sys.path.insert(0, str(REPO / 'examples' / 'kraken' / 'src'))
  from kraken.core.auth import Credentials
  from kraken.core.transport.http import HttpRpcClient

  async with Server((503, {'Retry-After': '0'})) as server:
    client = HttpRpcClient(
      base_url=server.url, credentials=Credentials('key', 'c2VjcmV0'), http=HttpClient(retry=True),
    )
    try:
      await client.authed_request('/0/private/Balance')
    except Exception:
      pass  # the stub's `{}` is no Kraken envelope; only the nonces sent matter
  nonces = [re.search(rb'nonce=(\d+)', raw)[1] for _, raw in server.seen]
  assert len(nonces) == 2
  assert nonces[0] != nonces[1], 'the retry re-sent the same nonce'


# 2. "Nothing was sent" is not true after a redirect ------------------------------------

async def test_a_connect_error_after_a_followed_redirect_does_not_repeat_the_post():
  refused = httpx.URL('http://127.0.0.1:9/')  # discard port: nothing listens
  async with Server((307, {'Location': str(refused)})) as server:
    async with HttpClient(retry=True) as http:
      try:
        await http.request('POST', server.url + '/order', content=b'{"buy": 1}', follow_redirects=True)
      except Exception:
        pass  # a NetworkError is the right outcome; a second POST at the origin is not
  assert len(server.seen) == 1, f'the origin acted on the POST {len(server.seen)} times'


# 3. Retry-After parsing ----------------------------------------------------------------



# 4. A caller cancelled while paced gives its slot back ---------------------------------

async def test_a_cancelled_waiter_does_not_delay_the_next_caller():
  async with Server() as server:
    async with HttpClient(rate=2) as http:
      await http.request('GET', server.url)          # slot t0
      waiter = asyncio.create_task(http.request('GET', server.url))  # slot t0 + 0.5
      await asyncio.sleep(0.1)
      waiter.cancel()
      began = time.monotonic()
      await http.request('GET', server.url)          # could take t0 + 0.5; waits for t0 + 1.0
      assert time.monotonic() - began < 0.6


# 5. A core that does not pass RATE/RETRY on is not detected ----------------------------



@pytest.mark.parametrize(('api_key', 'api_secret'), [(None, None), ('test-key', None), (None, 'test-secret')])
async def test_missing_credentials_fail_before_pacing_without_spending_a_slot(api_key, api_secret):
  core = hmac_template()
  async with Server() as server, HttpClient(rate=1) as http:
    transport = core.Transport(base_url=server.url, http=http, api_key=api_key, api_secret=api_secret)
    await transport.send('GET', '/time', params={}, body=None, public=True)
    start = time.monotonic()
    with pytest.raises(AuthError):
      await transport.send('POST', '/order', params={}, body=b'{}', public=False)
    refused_after = time.monotonic() - start
    await transport.send('GET', '/time', params={}, body=None, public=True)
    next_after = time.monotonic() - start
  assert refused_after < 0.2
  assert next_after < 1.2
  assert len(server.seen) == 2
