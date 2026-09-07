"""`truewire mock`: serve a project's recorded examples over HTTP and WebSocket."""
import time

import typer

from .common import PROJECT_OPTION, resolve_project


def mock(
  project: str | None = PROJECT_OPTION,
  host: str = typer.Option('127.0.0.1', '--host'),
  http_port: int = typer.Option(8321, '--http-port', help='HTTP port; 0 picks a free one.'),
  ws_port: int = typer.Option(8322, '--ws-port', help='WebSocket port; 0 picks a free one.'),
):
  """Replay the project's recorded examples from a local HTTP and WebSocket server.

  Requests are matched structurally against `examples/`: an unknown route answers 404, a
  known route with no matching example 422, and two matching examples 409. Point a
  generated client at the printed URLs and it runs without touching the network.
  """
  from truewire.mock import running_mock_servers

  loaded = resolve_project(project)
  with running_mock_servers(loaded, host=host, http_port=http_port, ws_port=ws_port) as servers:
    typer.echo(f'HTTP  {servers.http_base_url}')
    if servers.ws_server is not None:
      typer.echo(f'WS    {servers.ws_server.url}')
    else:
      typer.echo('WS    (no websocket examples in this project)')
    typer.echo('Serving recorded examples; Ctrl-C to stop.')
    try:
      while True:
        time.sleep(3600)
    except KeyboardInterrupt:
      typer.echo('Stopping.')
