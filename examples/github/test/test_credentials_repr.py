"""The API key authenticates every call but never shows in repr, str or pprint.

The core hides `Transport.api_key` with `repr=False`. These tests seed a sentinel key and
fail if it shows up in the repr of the client, its routers, the transport, copies or an
`ApiError`, before or after the httpx client opens.
"""

import asyncio
import copy
import dataclasses
import json
import pprint
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from truewire_core.exceptions import ApiError

from github import GitHub

SENTINEL = 'SENTINEL-TOKEN-REPR'


@pytest.fixture
def server():
  seen: list[dict[str, str]] = []

  class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
      seen.append({k.lower(): v for k, v in self.headers.items()})
      status = 401 if self.path.startswith('/repos/deny') else 200
      body = json.dumps({'message': 'nope'} if status == 401 else {'id': 1}).encode()
      self.send_response(status)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
      pass

  srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
  threading.Thread(target=srv.serve_forever, daemon=True).start()
  yield f'http://127.0.0.1:{srv.server_address[1]}', seen
  srv.shutdown()


def shown(client: GitHub) -> list[str]:
  return [
    repr(client),
    str(client),
    f'{client!r}',
    pprint.pformat(client),
    repr(client.issues),
    repr(client.repos),
    repr(client.client),
    repr(client.client.http),
    repr(copy.copy(client)),
    repr(dataclasses.replace(client.client)),
  ]


def test_repr_hides_sentinel_and_shows_structure():
  client = GitHub.new(api_key=SENTINEL)
  for text in shown(client):
    assert SENTINEL not in text, text
  assert repr(client).startswith('GitHub(client=Transport(base_url=')
  assert repr(client.issues).startswith('Issues(client=Transport(')
  assert repr(client.repos).startswith('Repos(client=Transport(')
  # the key is still on the transport, and replace()/copy keep it
  assert client.client.api_key == SENTINEL
  assert dataclasses.replace(client.client).api_key == SENTINEL


def test_sentinel_authenticates_and_stays_out_of_repr_after_calls(server):
  url, seen = server

  async def run():
    async with GitHub.new(base_url=url, api_key=SENTINEL) as client:
      await client.repos.get(owner='o', repo='r', validate=False)
      await client.issues.list(owner='o', repo='r', validate=False)
      with pytest.raises(ApiError) as caught:
        await client.repos.get(owner='deny', repo='r', validate=False)
      # after the httpx client has opened
      return shown(client), str(caught.value), repr(caught.value)

  texts, err, err_repr = asyncio.run(run())
  assert [h['authorization'] for h in seen] == [f'Bearer {SENTINEL}'] * 3
  for text in [*texts, err, err_repr]:
    assert SENTINEL not in text, text


def test_no_key_sends_no_authorization(server):
  url, seen = server

  async def run():
    async with GitHub.new(base_url=url) as client:
      await client.repos.get(owner='o', repo='r', validate=False)

  asyncio.run(run())
  assert 'authorization' not in seen[0]
