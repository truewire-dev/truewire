"""`truewire capture`: record one live call as an example pair.

The call goes through the project's own generated client and core, so the request carries
the real headers, signing and envelope handling, and the recorded response is the wire
body the core saw, before any unwrapping (docs/spec/authoring.md rule 6). WebSocket
endpoints are not captured here; only `rpc` endpoints over HTTP are.
"""

import asyncio
import json
from pathlib import Path

import typer

from .common import PROJECT_OPTION, resolve_project


def capture(
  function: str = typer.Argument(..., help='Endpoint function path, e.g. `repos.list_commits`.'),
  request: str = typer.Option(
    '{}', '--request', '-r', help='The call, as a JSON object of API-named parameters.',
  ),
  example_id: str = typer.Option('default', '--id', help='Example name; overwrites an existing pair.'),
  description: str | None = typer.Option(None, '--description', '-d', help='One line stored with the request.'),
  new: list[str] = typer.Option(
    [], '--new', help='key=value passed to the root client\'s `.new(...)`; JSON values are decoded. Repeatable.',
  ),
  scrub: list[str] = typer.Option(
    [], '--scrub', help='Response key whose value is replaced by `REDACTED_<KEY>` wherever it appears. Repeatable.',
  ),
  check: bool = typer.Option(True, '--check/--no-check', help='Validate the recorded pair against the spec afterwards.'),
  project: str | None = PROJECT_OPTION,
):
  """Call one endpoint against the live API and record the pair under its `examples/`.

  `truewire capture repos.list_commits --request '{"owner": "o", "repo": "r", "per_page": 3}' --id page1`
  writes `page1.request.json` and `page1.response.json` beside the endpoint's spec and
  runs `truewire check` on them. A non-2xx answer is printed and nothing is written:
  examples record what the API does on success; errors belong to the client core.
  """
  from truewire.examples import run_example_request
  from truewire.mcp import load_client, parse_new_kwargs
  from truewire.spec import ExampleRequest
  from truewire.spec.repo import endpoint_records
  from truewire_core.exceptions import ApiError
  from truewire_core.http import recording

  loaded = resolve_project(project)
  try:
    parameters = json.loads(request)
  except json.JSONDecodeError as exc:
    typer.echo(f'--request is not valid JSON: {exc}', err=True)
    raise typer.Exit(code=1)
  if not isinstance(parameters, dict):
    typer.echo('--request must be a JSON object', err=True)
    raise typer.Exit(code=1)
  try:
    new_kwargs = parse_new_kwargs(new)
  except ValueError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)

  spec_root = loaded.spec_dir
  record = next(
    (r for r in endpoint_records(loaded) if r.endpoint.resolved_function(r.path, spec_root) == function),
    None,
  )
  if record is None:
    typer.echo(f'no endpoint with function {function!r} in {spec_root}', err=True)
    raise typer.Exit(code=1)
  if record.endpoint.spec.kind != 'rpc' or 'http' not in record.endpoint.spec.transports:
    typer.echo(f'{function} is not an HTTP rpc endpoint; capture records HTTP request/reply pairs only', err=True)
    raise typer.Exit(code=1)

  client = load_client(loaded, new_kwargs)
  example = ExampleRequest(description=description, request=parameters)

  async def call():
    async with client:
      with recording() as exchanges:
        try:
          await run_example_request(
            client, record.endpoint, example, client_root=loaded, endpoint_path=record.path,
          )
        except ApiError as exc:
          return exchanges, exc
        return exchanges, None

  exchanges, error = asyncio.run(call())
  if not exchanges:
    typer.echo('the call made no HTTP request through truewire_core.http.HttpClient; nothing to record', err=True)
    raise typer.Exit(code=1)
  exchange = exchanges[-1]
  status = exchange.response.status_code
  if error is not None or status >= 300:
    typer.echo(f'{exchange.request.method} {exchange.request.url}: HTTP {status}', err=True)
    typer.echo(exchange.response.text[:1000], err=True)
    typer.echo('not recorded: examples keep 2xx responses only (authoring rule 0)', err=True)
    raise typer.Exit(code=1)
  try:
    payload = exchange.response.json()
  except ValueError:
    typer.echo(f'HTTP {status} body is not JSON; capture records JSON bodies only', err=True)
    raise typer.Exit(code=1)
  payload = scrub_keys(payload, set(scrub))

  out = record.path.parent / 'examples'
  out.mkdir(parents=True, exist_ok=True)
  request_file = out / f'{example_id}.request.json'
  response_file = out / f'{example_id}.response.json'
  request_file.write_text(json.dumps(
    {k: v for k, v in (('description', description), ('request', parameters)) if v is not None},
    indent=2,
  ) + '\n')
  response_file.write_text(json.dumps({'status': status, 'payload': payload}, indent=2) + '\n')
  typer.echo(f'{function}[{example_id}]: HTTP {status}, {len(exchange.response.content)} bytes')
  typer.echo(f'  {relative(request_file, loaded.root)}')
  typer.echo(f'  {relative(response_file, loaded.root)}')

  if check:
    from .check import check as run_check
    run_check(project=str(loaded.root), path=str(record.path.parent), verbose=False)


def scrub_keys(value, keys: set[str]):
  """Replace every value under one of `keys` with an obviously fake placeholder, recursively."""
  if not keys:
    return value
  if isinstance(value, dict):
    return {
      k: (f'REDACTED_{k.upper()}' if k in keys and v is not None else scrub_keys(v, keys))
      for k, v in value.items()
    }
  if isinstance(value, list):
    return [scrub_keys(v, keys) for v in value]
  return value


def relative(path: Path, root: Path) -> str:
  try:
    return str(path.relative_to(root))
  except ValueError:
    return str(path)
