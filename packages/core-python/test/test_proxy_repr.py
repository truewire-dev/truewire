"""Proxy credentials stay usable without leaking through runtime or client repr."""

from pathlib import Path
import subprocess
import sys

from truewire_core.http import HttpClient
from truewire_core.ws.socket import Socket

PROXY = 'http://synthetic-proxy-user:synthetic-proxy-password@proxy.invalid:8080'


class QuietSocket(Socket):
  def on_msg(self, msg: str | bytes):
    pass


def assert_proxy_hidden(value: object):
  shown = repr(value)
  assert 'synthetic-proxy-user' not in shown
  assert 'synthetic-proxy-password' not in shown
  assert PROXY not in shown


def test_http_client_repr_hides_proxy_credentials():
  client = HttpClient(proxy=PROXY)
  assert client.proxy == PROXY
  assert_proxy_hidden(client)


def test_socket_repr_hides_proxy_credentials():
  socket = QuietSocket('wss://example.invalid', proxy=PROXY)
  assert socket.proxy == PROXY
  assert_proxy_hidden(socket)


def test_generated_client_nested_repr_hides_proxy_credentials():
  """Exercise the checked-in generated GitHub root and its nested endpoint transport."""
  source = Path(__file__).resolve().parents[3] / 'examples' / 'github' / 'src'
  result = subprocess.run(
    [
      sys.executable,
      '-c',
      """
import sys
sys.path.insert(0, sys.argv[1])
from github import GitHub

client = GitHub.new(proxy=sys.argv[2])
assert client.client.http.proxy == sys.argv[2]
print(repr(client))
print(repr(client.repos))
""",
      str(source),
      PROXY,
    ],
    capture_output=True,
    text=True,
    check=True,
  )
  assert 'HttpClient(' in result.stdout
  assert_proxy_hidden(result.stdout)
