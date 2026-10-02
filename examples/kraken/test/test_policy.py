"""W15: `truewire.toml` refuses `spot.funding.withdraw`, so the generated method raises
`RefusedByPolicy` before any request. A loopback server counts every request that reaches
it: none for the refused endpoint, one for a sibling that is not refused."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest
from truewire_core.exceptions import Error, LogicError

from kraken import Kraken
from kraken.core.transport.http import HttpRpcClient
from kraken.core.transport.ws import KrakenSocketClient
from kraken.policy import RefusedByPolicy

from conftest import FAKE_CREDENTIALS


class CountingServer(ThreadingHTTPServer):
  requests = 0


class Handler(BaseHTTPRequestHandler):
  """Counts the request, then answers with a Kraken error envelope."""

  def do_POST(self):
    cast(CountingServer, self.server).requests += 1
    self.rfile.read(int(self.headers.get('Content-Length') or 0))
    body = b'{"error": ["EGeneral:Permission denied"]}'
    self.send_response(200)
    self.send_header('Content-Type', 'application/json')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  do_GET = do_POST

  def log_message(self, format, *args):
    pass


@pytest.fixture
def server() -> Iterator[CountingServer]:
  server = CountingServer(('127.0.0.1', 0), Handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    yield server
  finally:
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_a_refused_endpoint_raises_before_any_request(server: CountingServer):
  http = HttpRpcClient(
    base_url=f'http://127.0.0.1:{server.server_address[1]}', credentials=FAKE_CREDENTIALS, validate=True,
  )
  client = Kraken(
    spot_client=http,
    market_client=KrakenSocketClient.new('ws://unused', validate=True),
    private_client=KrakenSocketClient.new('ws://unused', validate=True),
  )

  with pytest.raises(RefusedByPolicy) as refused:
    await client.spot.funding.withdraw(asset='XBT', key='cold-storage', amount='0.5')
  assert refused.value.endpoint == 'spot.funding.withdraw'
  assert isinstance(refused.value, LogicError)
  with pytest.raises(RefusedByPolicy):
    await client.spot.funding.withdraw(asset='XBT', key='cold-storage', amount='0.5', validate=False)
  assert server.requests == 0

  # The server does see what the client sends: a sibling that is not refused reaches it.
  with pytest.raises(Error) as allowed:
    await client.spot.funding.withdraw_methods()
  assert not isinstance(allowed.value, RefusedByPolicy)
  assert server.requests == 1
