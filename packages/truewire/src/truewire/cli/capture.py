"""`truewire capture`: record one live call as an example pair.

The call goes through the project's own generated client and core, so the request carries
the real headers, signing and envelope handling, and the recorded response is the wire
body the core saw, before any unwrapping (docs/spec/authoring.md rule 6). WebSocket
endpoints are not captured here; only `rpc` endpoints over HTTP are.

A hand-written core legitimately sends more than the one request the call is about: it
mints an OAuth token, refreshes an expired one, fetches a WebSocket ticket, retries after
a 401. So the exchange to record is chosen by what the endpoint declares -- its method
and its `path` filled from the call's own parameters, or, for a JSON-RPC-shaped endpoint,
its method name read off the frame -- never by position in the recording. Recording
whichever exchange happened to be last wrote a token endpoint's 200 body, an access
token, into `examples/<id>.response.json`, from where it was committed and published;
authoring rule 6 exists to keep exactly that out of a recording. When no exchange matches,
nothing is written: a wrong recording published as evidence is worse than no recording.
Every message here names methods and paths only -- a header, a request body and a
response body are the places a credential lives.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import typer
from typing_extensions import TYPE_CHECKING, Any, Mapping, Sequence

from truewire.spec import Endpoint, read_dotted_path, rpc_selector
from truewire.spec.request import PLACEHOLDER

from .common import PROJECT_OPTION, resolve_project

if TYPE_CHECKING:
  from truewire_core.http import Exchange

MAX_LISTED_EXCHANGES = 10
"""Exchanges listed in a report before the rest collapse into a count -- a chatty core
(one token call per request, say) should not bury the line that matters."""


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

  `truewire capture repos.list_commits --request '{"owner": "o", "repo": "r", "per_page": 3, "page": 1}' --id page1`
  writes `page1.request.json` and `page1.response.json` beside the endpoint's spec and
  runs `truewire check` on them. Record a paginated walk with every page index explicit,
  `page: 1` included: the generated `_paged` walk sends it, and the mock server matches
  the whole request. A non-2xx answer is printed and nothing is written:
  examples record what the API does on success; errors belong to the client core.

  The recorded exchange is the one that matches the endpoint's own method and path,
  wherever the core sent it in the sequence; the requests the core made around it (a
  token mint, a retry) are reported as skipped and never recorded. A call that reaches
  the API through nothing matching the endpoint records nothing at all.
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

  route = endpoint_route(record.endpoint, parameters)
  matched, skipped = select_exchanges(exchanges, route)
  if not matched:
    typer.echo(
      f'{function}: no request the core made matches this endpoint; nothing recorded', err=True,
    )
    typer.echo(f'  expected: {route.display}', err=True)
    typer.echo(f'  the call made {len(exchanges)} request{plural(exchanges)} through '
               'truewire_core.http.HttpClient:', err=True)
    for line in exchange_lines(exchanges):
      typer.echo(f'    {line}', err=True)
    typer.echo('  method and path only: a header, a request body and a response body are '
               'where a credential lives', err=True)
    if error is not None:
      typer.echo('  the call also raised ApiError; its message is withheld here because it '
                 'can quote a response body', err=True)
    raise typer.Exit(code=1)

  # More than one match is a retry (a 401 refreshed and re-sent) or a walk: every one of
  # them is this endpoint's own exchange against this endpoint's own parameters, so none
  # of them can be another endpoint's response, and the last is the attempt that stands --
  # the earlier ones are the ones that failed. Recorded, and said out loud, rather than
  # refused: refusing would make an ordinary retrying core uncapturable, and refusing is
  # only worth its cost where the alternative is recording something that isn't the
  # endpoint's at all, which is what `not matched` above already covers.
  exchange = matched[-1]
  status = exchange.response.status_code
  if status >= 300:
    typer.echo(f'{exchange.request.method} {exchange.request.url.path}: HTTP {status}', err=True)
    typer.echo(exchange.response.text[:1000], err=True)
    typer.echo('not recorded: examples keep 2xx responses only (authoring rule 0)', err=True)
    for line in selection_report(route, matched, skipped, verb='selected'):
      typer.echo(line, err=True)
    raise typer.Exit(code=1)
  if error is not None:
    # The endpoint answered, but the call did not complete -- a core that raised on a
    # later request of its own, or on the reply it went on to unwrap. Recording the pair
    # would record a call nobody can replay.
    typer.echo(f'{route.display}: HTTP {status}, but the call raised ApiError; its message '
               'is withheld here because it can quote a response body', err=True)
    typer.echo('not recorded: the call did not complete', err=True)
    for line in selection_report(route, matched, skipped, verb='selected'):
      typer.echo(line, err=True)
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
  for line in selection_report(route, matched, skipped, verb='recorded'):
    typer.echo(line)
  if drop_unverified(record.path):
    # The pair just written is the evidence `unverified` said was missing (ADR 0001);
    # left in place it would fail `truewire examples` on the next run.
    typer.echo(f'  removed the stale `unverified` declaration from {relative(record.path, loaded.root)}')

  if check:
    from .check import check as run_check
    run_check(project=str(loaded.root), path=str(record.path.parent), verbose=False)


@dataclass(frozen=True)
class EndpointRoute:
  """What the endpoint being captured looks like on the wire, as the endpoint declares it.

  Two shapes, the same split the mock server's own registry makes: a REST-shaped endpoint
  whose `path` is a URL path (matched by `pattern`), and a JSON-RPC-shaped one whose
  `path` is a method name carried inside the posted frame (matched by `rpc_method` at
  `selector`, every such call going to the one base URL with no path to tell it apart).
  """
  method: str | None
  """The declared HTTP method, upper-cased, or `None` where the endpoint declares none --
  an HTTP-transported JSON-RPC API that is uniformly POST leaves it to the core (ADR 0006),
  and a verb the spec never states cannot disqualify an exchange."""
  display: str
  """How the route reads in a report: `GET /pets/42`, or `POST pets_get` for a JSON-RPC
  method name. Built from the spec and the call's own parameters, never from the wire."""
  pattern: 're.Pattern[str] | None'
  """Regex matching the tail of a wire URL path, or `None` for a JSON-RPC-shaped endpoint."""
  rpc_method: str | None
  """The declared JSON-RPC method name, or `None` for a REST-shaped endpoint."""
  selector: str
  """Dotted path naming the operation inside a posted frame; read only when `rpc_method` is set."""


def endpoint_route(endpoint: Endpoint, parameters: Mapping[str, Any]) -> EndpointRoute:
  """Describe the one request this endpoint's generated method makes, from the spec and
  the parameters the call is being made with.

  Args:
    endpoint: The endpoint being captured; its `spec.path` and `spec.method`.
    parameters: The call's API-named parameters, as `--request` gave them -- the same
      names the `path` template's `{slots}` use (authoring rule 0).
  """
  method = endpoint.method.upper() if endpoint.method else None
  template = endpoint.path or '/'
  verb = method or 'any method'
  if not template.startswith('/'):
    # `path` is a JSON-RPC method name, not a URL path -- the same reading
    # `truewire.mock` makes of an endpoint whose path does not start with `/`.
    return EndpointRoute(
      method=method, display=f'{verb} {template} (JSON-RPC method)', pattern=None,
      rpc_method=template, selector=rpc_selector(endpoint.envelope),
    )
  filled = filled_path(template, parameters)
  return EndpointRoute(
    method=method, display=f'{verb} {filled}', pattern=path_pattern(filled),
    rpc_method=None, selector=rpc_selector(endpoint.envelope),
  )


def filled_path(template: str, parameters: Mapping[str, Any]) -> str:
  """Fill a `path` template's `{name}` slots from the call's own API-named parameters, the
  way a generated client's core does before it sends.

  A slot the call does not name is left as written; `path_pattern` then matches it against
  any one segment rather than inventing a value for it.

  Args:
    template: The endpoint's declared `path`.
    parameters: The call's API-named parameters.
  """
  def fill(match: 're.Match[str]') -> str:
    name = match.group(1)
    return str(parameters[name]) if name in parameters else match.group(0)
  return PLACEHOLDER.sub(fill, template)


def path_pattern(path: str) -> 're.Pattern[str]':
  """Compile a filled path into a regex matching the tail of a wire URL path.

  Anchored at the end and not at the start, because a base URL carries a path prefix of
  its own (`https://petstore.example/v1` + `/pets/42`) and an endpoint declares only its
  own path. A declared path always starts with `/`, so a tail match starts on a segment
  boundary and `/pets` can never match the `/v1/repets` of some other call. A trailing
  slash on the wire is accepted; a slot `filled_path` could not fill matches one segment.

  Args:
    path: The endpoint's `path`, with its slots filled where the call named them.
  """
  parts: list[str] = []
  cursor = 0
  for match in PLACEHOLDER.finditer(path):
    parts.append(re.escape(path[cursor:match.start()]))
    parts.append(r'[^/]+')
    cursor = match.end()
  parts.append(re.escape(path[cursor:]))
  return re.compile(''.join(parts) + r'/?\Z')


def select_exchanges(
  exchanges: 'Sequence[Exchange]', route: EndpointRoute,
) -> 'tuple[list[Exchange], list[Exchange]]':
  """Split what the core sent into the exchanges belonging to `route` and the rest.

  Both lists keep the order they were recorded in. Nothing here reads a response, and the
  request is read only for the JSON-RPC method name a JSON-RPC-shaped endpoint is
  identified by -- there is no other way to tell two frames posted to one URL apart.

  Args:
    exchanges: Everything `truewire_core.http.recording()` saw, in order.
    route: The endpoint's declared route.
  """
  matched: 'list[Exchange]' = []
  skipped: 'list[Exchange]' = []
  for exchange in exchanges:
    (matched if matches_route(exchange, route) else skipped).append(exchange)
  return matched, skipped


def matches_route(exchange: 'Exchange', route: EndpointRoute) -> bool:
  """Whether one recorded exchange is the endpoint's own call.

  Method and path (or JSON-RPC method name) only: a query string is deliberately not
  compared, since a core may add a signature, a nonce or a timestamp of its own to it
  (ADR 0007), and the endpoint is already identified without it.

  Args:
    exchange: One recorded request/response pair.
    route: The endpoint's declared route.
  """
  if route.method is not None and exchange.request.method.upper() != route.method:
    return False
  if route.pattern is not None:
    return route.pattern.search(unquote(exchange.request.url.path)) is not None
  try:
    frame = json.loads(exchange.request.content)
  except (ValueError, UnicodeDecodeError, RuntimeError):
    # Not a JSON frame, or a streaming request body that was never read back
    # (`httpx.RequestNotRead`, a `RuntimeError`): either way this is not the frame.
    return False
  return isinstance(frame, dict) and read_dotted_path(frame, route.selector) == route.rpc_method


def exchange_lines(exchanges: 'Sequence[Exchange]') -> list[str]:
  """Render exchanges for a report: method and path, in order, nothing else.

  Never a query string, a header or a body. A path is what identifies a request; the rest
  is where an access token, a signature or a session cookie would be, and this output is
  read (and pasted) by people debugging a capture that went wrong.

  Args:
    exchanges: The exchanges to list.
  """
  lines = [
    f'{exchange.request.method.upper()} {exchange.request.url.path}'
    for exchange in exchanges[:MAX_LISTED_EXCHANGES]
  ]
  if len(exchanges) > MAX_LISTED_EXCHANGES:
    lines.append(f'... and {len(exchanges) - MAX_LISTED_EXCHANGES} more')
  return lines


def selection_report(
  route: EndpointRoute, matched: 'Sequence[Exchange]', skipped: 'Sequence[Exchange]', *, verb: str,
) -> list[str]:
  """Say which exchange was taken and what was left, as indented lines.

  Printed on every capture, not only the surprising ones: silence about the other
  requests a core makes is how recording the wrong one stayed invisible.

  Args:
    route: The endpoint's declared route.
    matched: The exchanges belonging to the endpoint, in order.
    skipped: Everything else the core sent, in order.
    verb: What happened to the chosen exchange, `recorded` or `selected`.
  """
  lines = [f'  {verb} the exchange for {route.display}']
  if len(matched) > 1:
    lines.append(
      f'  {len(matched)} requests matched this endpoint (a retry, or a repeated attempt); '
      f'took the last'
    )
  if skipped:
    total = len(matched) + len(skipped)
    lines.append(
      f'  your core made {total} requests; the {len(skipped)} below are not this '
      'endpoint\'s and were skipped:'
    )
    lines.extend(f'    {line}' for line in exchange_lines(skipped))
  return lines


def plural(items: 'Sequence[Any]') -> str:
  """`''` for one item, `'s'` for any other count."""
  return '' if len(items) == 1 else 's'


def drop_unverified(endpoint_file: Path) -> bool:
  """Remove the `unverified` block from one `endpoint.json`, keeping every other key in
  its place and the file in the two-space form the other writers use (`migrate`).

  Returns:
    Whether the file declared one.
  """
  data = json.loads(endpoint_file.read_text())
  if not isinstance(data, dict) or 'unverified' not in data:
    return False
  del data['unverified']
  endpoint_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
  return True


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
