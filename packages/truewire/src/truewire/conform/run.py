"""One conformance run over one project: every recorded HTTP request, called live once.

Each call goes through the project's own generated Python client and hand-written core,
with response validation on, inside `truewire_core.http.recording()`. The exchange that
belongs to the endpoint is picked out of what the core sent the way `truewire capture`
picks it (method and path, never position), and the one response is then judged three
ways (ADR 0012, TRU-15):

1. The raw wire body against the endpoint's response schema. A violation is `drift`:
   the API no longer sends what the spec says.
2. The same response as the generated client handled it. If the body passed step 1 and
   the client still raised, the finding is `client:python`: the API is as specced and
   our code is wrong. This split is what the report is for.
3. The raw body's shape against the recorded `<id>.response.json`: keys added and
   removed, type, nullability and element-shape changes, the status code. Values never.

Nothing is written under `spec/` and no recording is touched. A report quotes method,
path, status, exception class and shapes; never a header, a request body or a response
body, beyond an enum value the schema does not know.
"""

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timezone
from fnmatch import fnmatch
import inspect
import json
import os
from pathlib import Path
import re
import sys
import time
from typing_extensions import Any

from truewire.project import Project
from truewire.spec import Endpoint, ExampleRequest
from truewire.spec.repo import endpoint_records, load_shared_schemas
from truewire.spec.validation import http_response_schema, schema_document

from .report import EndpointResult, ExampleResult
from .schema import SchemaView
from .shape import Finding, combine, declared_pointer, schema_findings, shape_findings

READ_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
"""Methods phase one calls without being told to. Anything else could place an order or
delete a resource: write endpoints wait for scenarios (ADR 0012, phase two), and a read the
API happens to send as a POST (Kraken's private half) is named with `--allow`."""

MAX_DETAIL_CHARS = 200
"""A detail line (an exception the core raised before any response) is cut here."""

TRANSIENT_STATUSES = frozenset({408, 425, 429})
"""Statuses that say "not now" rather than "not like this": an `error`, not drift. Every
5xx is one too."""

EPOCH_SECONDS = (946684800, 4102444800)
"""2000-01-01 to 2100-01-01: an integer in this range, in seconds to nanoseconds
(`EPOCH_SCALES`), under a declared or time-like name, is read as a time when deciding
whether a recording has gone stale."""

TIME_WORDS = frozenset({'time', 'timestamp', 'datetime', 'ts', 'date', 'since', 'until'})
"""A parameter is time-like by name when one of its words is here (`startTime`, `dateFrom`,
`end_ts`), or when it has several words and the last is `at` (`createdAt`). Only then, or
when the spec declares its path a time (`TIME_FORMATS`), is a number read as a time: an
id, a hash or a row offset can fall in the epoch range too. A bare `start`, `end`, `from`
or `to` needs the declared format."""

TIME_FORMATS = frozenset({'epoch-seconds', 'epoch-millis', 'epoch-micros', 'epoch-nanos', 'date-time', 'date'})
"""Formats that declare a request parameter a time."""

EVERY = range(sys.maxsize)
"""In a declared time's path, any index of a list (its schema's `items`). Beside
`prefixItems`, `items` covers only the positions after them: `range(len(prefixItems), …)`."""

EPOCH_SCALES = (1, 10**3, 10**6, 10**9)
"""Seconds, milliseconds, microseconds, nanoseconds: the ranges do not overlap."""

FIRST_DAY = date(2000, 1, 1)
"""No time or date code before this day is read as one."""

DATE_CODE = re.compile(r'(?<![^-_])([0-9]{6}|[0-9]{8})(?![^-_])')
"""A `YYMMDD` or `YYYYMMDD` group inside an identifier, bounded by `-`, `_` or the value's
edge, as an option's expiry in `BTC-260810-65000-C`."""

CURSOR_ID = re.compile(r'^(from|start|since|after)_?id$', re.IGNORECASE)
"""A parameter that starts a read at an id in the API's history (`fromId`): it ages out of
the API's retention the way a time window does."""


def select_records(project: Project, only: list[str]) -> list[tuple[str, str, Path, Endpoint]]:
  """Every endpoint in the project, as (function, directory, endpoint.json, endpoint),
  in path order, narrowed to those matching one `only` glob (on the function or the
  directory under `spec/endpoints`) when any are given."""
  out = []
  for record in endpoint_records(project):
    function = record.endpoint.resolved_function(record.path, project.spec_dir)
    directory = record.path.parent.relative_to(project.endpoints_dir).as_posix()
    if only and not any(fnmatch(function, glob) or fnmatch(directory, glob) for glob in only):
      continue
    out.append((function, directory, record.path, record.endpoint))
  return out


def unsupported(endpoint: Endpoint) -> str | None:
  """Why phase one does not call this endpoint, or `None` when it does."""
  kind = endpoint.spec.kind
  if kind == 'stream':
    return 'stream: phase 2'
  if kind != 'rpc':
    return f'{kind}: not in phase 1'
  if 'http' not in endpoint.spec.transports:
    return 'ws: phase 2'
  return None


def recorded_requests(endpoint_path: Path) -> list[tuple[str, Path, Path | None]]:
  """(example id, request file, response file or `None`) for every recorded request."""
  examples = endpoint_path.parent / 'examples'
  if not examples.is_dir():
    return []
  out = []
  for request in sorted(examples.glob('*.request.json')):
    example_id = request.name[: -len('.request.json')]
    response = examples / f'{example_id}.response.json'
    out.append((example_id, request, response if response.is_file() else None))
  return out


def pinned(parameters: Any, today: date, times: frozenset[tuple[str | int | range, ...]] = frozenset()) -> list[str]:
  """What in a recorded request ages, each as `name (why)`: a time before `today` (an
  integer in epoch seconds to nanoseconds, at a path in `times` or under a time-like name,
  or an ISO date), an identifier with a date code before `today`, or a cursor id into the
  API's history. Empty when nothing does. A list element's path ends in its index, and its
  name is its list's."""
  pins: list[str] = []

  def walk(path: tuple[str | int, ...], name: str | None, value: Any) -> None:
    if isinstance(value, dict):
      for key, item in value.items():
        walk((*path, str(key)), str(key), item)
    elif isinstance(value, list):
      for index, item in enumerate(value):
        walk((*path, index), name, item)
    elif name is not None and CURSOR_ID.match(name) and value not in (None, ''):
      pins.append(f'{name} (an id into history)')
    elif (day := past_day(value, today, timelike=name is not None and (declared(path, times) or time_like(name)))) is not None:
      pins.append(f'{name or "a value"} ({day.isoformat()})')

  walk((), None, parameters)
  return pins


def declared(path: tuple[str | int, ...], times: frozenset[tuple[str | int | range, ...]]) -> bool:
  """Whether `path` is one of `times`, where a `range` stands for the list indices in it."""
  return any(
    len(time) == len(path)
    and all(
      want == got if not isinstance(want, range) else isinstance(got, int) and got in want
      for want, got in zip(time, path)
    )
    for time in times
  )


def declared_public(meta: Any) -> bool:
  """Whether an endpoint's `meta` says it needs no credentials: `public: true`,
  `private: false`, or `signed: false` with no key tier (`security` absent, `NONE` or
  `System`, as Binance's core dispatches: its `MARKET_DATA` and `USER_STREAM` are unsigned
  but need an API key). A spec that only marks its private endpoints says nothing either
  way of the rest."""
  if not isinstance(meta, dict):
    return False
  unsigned = meta.get('signed') is False and meta.get('security') in (None, 'NONE', 'System')
  return meta.get('public') is True or meta.get('private') is False or unsigned


def time_like(name: str) -> bool:
  """Whether a parameter's name says it holds a time: see `TIME_WORDS`."""
  words = [word.lower() for word in re.findall(r'[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])', name)]
  return not TIME_WORDS.isdisjoint(words) or (len(words) > 1 and words[-1] == 'at')


def declared_times(spec: Any) -> frozenset[tuple[str | int | range, ...]]:
  """Paths of the request properties the endpoint's spec declares a time (`TIME_FORMATS`),
  at any depth, under `request` or `parameters`: `('filter', 'start')` for a `start` inside
  `filter`. A tuple position (`prefixItems`) adds its index, and a list's `items` adds the
  `range` of indices it covers (`EVERY` with no `prefixItems`), as `pinned` walks them."""
  paths: set[tuple[str | int | range, ...]] = set()

  def walk(schema: Any, path: tuple[str | int | range, ...]) -> None:
    if not isinstance(schema, dict):
      return
    if path and schema.get('format') in TIME_FORMATS:
      paths.add(path)
    for key, child in (schema.get('properties') or {}).items():
      walk(child, (*path, key))
    for child in [*schema.get('anyOf', []), *schema.get('oneOf', []), *schema.get('allOf', [])]:
      walk(child, path)
    prefix = schema.get('prefixItems') or []
    walk(schema.get('items'), (*path, EVERY[len(prefix):]))
    for index, child in enumerate(prefix):
      walk(child, (*path, index))

  if isinstance(spec, dict):
    walk(spec.get('request'), ())
    walk(spec.get('parameters'), ())
  return frozenset(paths)


def past_day(value: Any, today: date, *, timelike: bool) -> date | None:
  """The day `value` names, when it is a time or a date code from 2000 on and before
  `today`. A number is a time only when `timelike`."""
  if isinstance(value, bool):
    return None
  if isinstance(value, str) and value.isdigit():
    value = int(value)
  day: date | None = None
  if isinstance(value, (int, float)):
    if timelike:
      for scale in EPOCH_SCALES:
        if EPOCH_SECONDS[0] <= value / scale < EPOCH_SECONDS[1]:
          day = datetime.fromtimestamp(value / scale, timezone.utc).date()
          break
  elif isinstance(value, str):
    if re.match(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
      day = parse_day(value[:10], '%Y-%m-%d')
    else:
      for code in DATE_CODE.findall(value):
        day = parse_day(code, '%y%m%d' if len(code) == 6 else '%Y%m%d')
        if day is not None and day >= FIRST_DAY:
          break
  return day if day is not None and FIRST_DAY <= day < today else None


def parse_day(text: str, pattern: str) -> date | None:
  try:
    return datetime.strptime(text, pattern).date()
  except ValueError:
    return None


def missing_secrets(project: Project) -> list[str]:
  """`[secrets].required` names not set in this environment. Names only, never values."""
  return [name for name in project.secrets.required if not os.environ.get(name)]


MEDIA_TYPE = re.compile(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+')


def clip(text: str) -> str:
  return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS] + '...'


def media_type(content_type: str | None) -> str | None:
  """`text/html` of `text/html; charset=utf-8`: a `Content-Type` without its parameters,
  which can carry anything, or `None` when the header is missing or not a media type."""
  if content_type is None:
    return None
  essence = content_type.split(';', 1)[0].strip().lower()
  return essence if MEDIA_TYPE.fullmatch(essence) else None


def error_types(exc: BaseException) -> str:
  """A pydantic error's types (`model_type, missing`), or `''` for anything else. Its
  `str()` holds the input it rejected, and its `loc` can name a map key of that input."""
  errors = getattr(exc, 'errors', None)
  if not callable(errors):
    return ''
  try:
    types = sorted({str(error.get('type', '')) for error in errors(include_input=False, include_url=False)})
  except Exception:  # noqa: BLE001 -- not a pydantic error after all
    return ''
  return ', '.join(t for t in types if t)


class Conform:
  """A run's fixed context: the project, its client, its shared schemas and its pacing."""

  def __init__(
    self, project: Project, client: Any, *, interval: float, timeout: float,
    allow: list[str] | None = None, echo: Callable[[str], None] = lambda line: None,
    today: date | None = None,
  ):
    self.project = project
    self.today = today or datetime.now(timezone.utc).date()
    self.allow = allow or []
    self.client = client
    self.interval = interval
    self.timeout = timeout
    self.echo = echo
    self.shared_schemas = load_shared_schemas(project)
    self.last_call: float | None = None

  async def run(self, only: list[str]) -> list[EndpointResult]:
    """Call every recorded request in scope, one at a time."""
    results: list[EndpointResult] = []
    async with self.client:
      for function, directory, endpoint_path, endpoint in select_records(self.project, only):
        result = EndpointResult(function=function, path=directory)
        reason = unsupported(endpoint)
        requests = recorded_requests(endpoint_path) if reason is None else []
        if reason is None and not requests:
          reason = 'no_recording'
          if endpoint.unverified is not None:
            result.detail = f'unverified: {endpoint.unverified.reason}'
        if reason is None and not self.may_call(function, directory, endpoint):
          reason = 'not_a_read'
          result.detail = (
            f'{endpoint.method or "no method"}: phase 1 calls GET only; '
            '--allow names a read the API sends as a POST'
          )
        if reason is not None:
          result.reason = reason
        else:
          for example_id, request_file, response_file in requests:
            example = await self.example(
              endpoint, endpoint_path, example_id, request_file, response_file,
            )
            result.examples.append(example)
            self.echo(f'{function}[{example_id}]: {example.status}'
                      + (f' ({len(example.findings)} findings)' if example.findings else '')
                      + (f' -- {example.detail}' if example.detail else ''))
        result.settle()
        if result.reason is not None and not result.examples:
          self.echo(f'{function}: skipped ({result.reason})')
        results.append(result)
    return results

  def may_call(self, function: str, directory: str, endpoint: Endpoint) -> bool:
    """Whether phase one may send this endpoint's recorded request: a GET, or named by `--allow`."""
    if (endpoint.method or '').upper() in READ_METHODS:
      return True
    return any(fnmatch(function, glob) or fnmatch(directory, glob) for glob in self.allow)

  async def pace(self) -> None:
    """Hold the calls `interval` seconds apart. The core's own rate limiter, where it
    has one, applies on top: this is the floor, not the ceiling."""
    if self.last_call is not None:
      wait = self.interval - (time.monotonic() - self.last_call)
      if wait > 0:
        await asyncio.sleep(wait)
    self.last_call = time.monotonic()

  async def example(
    self, endpoint: Endpoint, endpoint_path: Path, example_id: str,
    request_file: Path, response_file: Path | None,
  ) -> ExampleResult:
    import httpx
    from truewire.cli.capture import endpoint_route, exchange_lines, select_exchanges
    from truewire.examples import client_identifier, coerce_example_call, resolve_endpoint_function
    from truewire_core.exceptions import ApiError, AuthError
    from truewire_core.exceptions import ValidationError as ClientValidationError
    from truewire_core.http import recording

    result = ExampleResult(id=example_id, status='ok')
    recorded = json.loads(response_file.read_text()) if response_file is not None else None
    if isinstance(recorded, dict):
      result.recorded_status = recorded.get('status')
    request = ExampleRequest.model_validate(json.loads(request_file.read_text()))

    try:
      fn = resolve_endpoint_function(
        self.client, endpoint, endpoint_path=endpoint_path, spec_root=self.project.spec_dir,
      )
      args, kwargs = coerce_example_call(fn, request, identifier=client_identifier(self.project))
    except Exception as exc:  # noqa: BLE001 -- the recorded request no longer fits the client
      # The class and pydantic's error types only: the message quotes the recorded request.
      types = error_types(exc)
      result.status = 'error'
      result.detail = clip(f'the recorded request does not bind to the generated method: '
                           f'{type(exc).__name__}' + (f' ({types})' if types else ''))
      return result
    if 'validate' in inspect.signature(fn).parameters and 'validate' not in kwargs:
      # The response-validation override every generated method takes, unless the
      # recorded request already bound an API parameter of that name to it.
      kwargs['validate'] = True

    await self.pace()
    raised: BaseException | None = None
    with recording() as exchanges:
      try:
        await asyncio.wait_for(fn(*args, **kwargs), timeout=self.timeout)
      except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 -- every outcome is classified below
        raised = exc

    parameters = request.request if request.request is not None else request.kwargs
    route = endpoint_route(endpoint, parameters or {})
    matched, _ = select_exchanges(exchanges, route)
    if not matched:
      if isinstance(raised, AuthError):
        missing = missing_secrets(self.project)
        result.status = 'skipped'
        result.reason = 'missing_credentials'
        result.detail = (
          f'missing_credentials: {", ".join(missing)} not set' if missing
          else 'missing_credentials: the core raised AuthError before sending the request'
        )
        if missing and not declared_public(endpoint.meta):
          # The spec no longer marks the endpoint public and this run has no credentials: a
          # finding an earlier run made on it (while the spec did) cannot be judged again. A
          # refusal of an endpoint the spec still marks public is the core's doing, and
          # withdraws nothing.
          result.withdraws = 'not called: the spec declares it private and no credentials are set'
        return result
      result.status = 'error'
      if raised is not None:
        # Only a transport failure's message is kept (refused, timed out). Any other can
        # quote the recorded request (the client validating its arguments) or another
        # exchange's response (an ApiError from a token call).
        message = f': {raised}' if isinstance(raised, httpx.TransportError) and str(raised) else ''
        result.detail = clip(f'{type(raised).__name__}{message} (no response from {route.display})')
      else:
        sent = '; '.join(exchange_lines(exchanges)) or 'nothing'
        result.detail = clip(f'no request matched {route.display}; the core sent {sent}')
      return result

    exchange = matched[-1]
    status = exchange.response.status_code
    result.http_status = status
    findings: list[Finding] = []
    if status in (401, 403) and status != result.recorded_status:
      # "Not you" or "not now" (an expired key, GitHub's exhausted rate limit), not "the
      # API changed". Drift only when the recording itself is this refusal.
      if isinstance(raised, ApiError) and (missing := missing_secrets(self.project)):
        result.status = 'skipped'
        result.reason = 'missing_credentials'
        result.detail = f'missing_credentials: HTTP {status}, and {", ".join(missing)} not set'
        return result
      result.status = 'error'
      result.detail = f'HTTP {status} from {route.display}'
      return result
    if status >= 500 or status in TRANSIENT_STATUSES:
      result.status = 'error'
      result.detail = f'HTTP {status} from {route.display}'
      return result
    if 400 <= status < 500 and result.recorded_status is not None and 200 <= result.recorded_status < 300:
      if pins := pinned(parameters, self.today, declared_times(json.loads(endpoint_path.read_text()).get('spec'))):
        # The recorded request asks for something that has aged out (a window past the
        # API's lookback, an expired instrument, an id past retention): the API refusing
        # it says nothing about the API. Not a finding; the recording needs re-recording.
        result.status = 'skipped'
        result.reason = 'stale_recording'
        result.detail = (f'HTTP {status} where the recording has {result.recorded_status}, '
                         f'and the recorded request pins {", ".join(pins)}: re-record it')
        result.withdraws = f'stale recording: pins {", ".join(pins)}'
        return result
    result.judged.add('status')
    if result.recorded_status is not None and status != result.recorded_status:
      findings.append(Finding(
        'drift', 'status', '', result.recorded_status, status, 'recording',
        f'HTTP {status} where the recording has {result.recorded_status}',
      ))
    if not 200 <= status < 300:
      result.findings = findings
      result.client = f'raised {type(raised).__name__}' if raised is not None else 'accepted'
      result.status = 'drift' if findings else 'error'
      if not findings:
        result.detail = f'HTTP {status} from {route.display}, and no recorded status to compare'
      return result

    try:
      body = exchange.response.json()
    except ValueError:
      findings.append(Finding(
        'drift', 'not_json', '', 'application/json', media_type(exchange.response.headers.get('content-type')),
        'schema', 'the body is not JSON',
      ))
      result.findings = findings
      result.status = 'drift'
      return result

    result.body = body
    schema, missing = http_response_schema(json.loads(endpoint_path.read_text())['spec'], status)
    schema_errors: list[Finding] = []
    view = SchemaView(document={}, alternatives=())
    if schema is not None:
      from jsonschema import Draft202012Validator
      from referencing.exceptions import Unresolvable
      document = schema_document(schema, self.shared_schemas)
      view = SchemaView.root(document)
      try:
        errors = sorted(Draft202012Validator(document).iter_errors(body), key=lambda e: list(e.absolute_path))
      except Unresolvable as exc:
        result.status = 'error'
        result.detail = clip(f'the response schema has an unresolvable $ref: {exc}')
        return result
      schema_errors = schema_findings(errors, view)
    else:
      result.detail = f'no schema to validate against: {missing}'
    shape: list[Finding] = []
    if isinstance(recorded, dict) and 'payload' in recorded:
      shape = shape_findings(recorded['payload'], body, view)
    findings += combine(schema_errors, shape)

    if isinstance(raised, ApiError):
      # A 2xx the core read as an error (an in-band `{"error": [...]}`): the API refused
      # the recorded request tonight. Its message quotes the body, so only the class is kept.
      result.status = 'error'
      result.client = f'raised {type(raised).__name__}'
      result.detail = f'HTTP {status}, but the core raised {type(raised).__name__}: an error reported in the body'
      return result
    result.judged.add('body')
    if raised is None:
      result.client = 'accepted'
      result.judged.add('client')
    elif schema_errors:
      result.client = 'rejected (the body fails the schema)'
    else:
      result.client = 'rejected'
      result.judged.add('client')
      findings += client_findings(raised, endpoint, ClientValidationError, view)

    result.findings = findings
    if any(f.kind == 'drift' for f in findings):
      result.status = 'drift'
    elif findings:
      result.status = 'client'
    return result


def client_findings(
  raised: BaseException, endpoint: Endpoint, validation_error: type, view: SchemaView,
) -> list[Finding]:
  """What the generated client objected to in a body the schema accepts.

  A `ValidationError` from the client's validator becomes one finding per location and
  pydantic error type. Neither pydantic's input nor its message is read (a message can
  quote the input: `Input tag 'x' found using 'kind'`), and the location keeps only the
  keys `view`, the response schema, declares: a map key is data, so it is `*`. Anything
  else the client raised is one finding naming the exception's class, whose message can
  quote the body too.
  """
  prefix = ''
  if endpoint.envelope is not None and endpoint.envelope.payload:
    prefix, view = declared_pointer(prefix, endpoint.envelope.payload.split('.'), view)
  cause = raised.__cause__ if isinstance(raised, validation_error) else None
  if cause is not None and hasattr(cause, 'errors'):
    out: dict[tuple[str, str], Finding] = {}
    for error in cause.errors(include_input=False, include_url=False):
      pointer, _ = declared_pointer(prefix, error.get('loc', ()), view)
      kind = str(error.get('type', ''))
      key = (pointer, kind)
      if key in out:
        out[key].count += 1
        continue
      out[key] = Finding(
        'client:python', 'client', pointer, 'accepted by the schema', kind, 'client',
        f'the generated model rejects the value here ({kind})',
      )
    if out:
      return list(out.values())
  return [Finding(
    'client:python', 'client', prefix, 'accepted by the schema', type(raised).__name__, 'client',
    f'the client raised {type(raised).__name__}',
  )]
