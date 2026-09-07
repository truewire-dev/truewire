"""Shared fixtures: a real `Kraken` client pointed at the local mock servers."""

import pytest

from kraken import Kraken
from kraken.core.auth import Credentials
from kraken.core.transport.http import HttpRpcClient
from kraken.core.transport.ws import KrakenSocketClient
from pathlib import Path

from truewire.mock import running_mock_servers

FAKE_CREDENTIALS = Credentials(
  api_key='mock-api-key', private_key='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA='
)
"""Never real -- the mock server does not verify `API-Sign`, only the request shape."""


@pytest.fixture
def mock_servers():
  with running_mock_servers(Path(__file__).resolve().parents[1]) as servers:
    yield servers


@pytest.fixture
def client(mock_servers):
  """A real `Kraken` client, its HTTP and both WebSocket connections all pointed at the
  local mock servers -- built directly rather than through `Kraken.new()`, which has no
  `base_url` override. `Kraken` (generated) subclasses `KrakenBase` directly (design
  §5c) -- constructed from its three raw transports (`spot_client`/`market_client`/
  `private_client`), not the higher-level `spot`/`streams`/`trading_ws` objects
  `KrakenBase.new()` would otherwise build from real credentials/URLs.
  """
  http = HttpRpcClient(
    base_url=mock_servers.http_base_url, credentials=FAKE_CREDENTIALS, validate=True
  )
  ws_url = (
    mock_servers.ws_server.url if mock_servers.ws_server is not None else 'ws://unused'
  )
  market_client = KrakenSocketClient.new(ws_url, validate=True)
  private_client = KrakenSocketClient.new(ws_url, validate=True)
  yield Kraken(
    spot_client=http, market_client=market_client, private_client=private_client,
  )
