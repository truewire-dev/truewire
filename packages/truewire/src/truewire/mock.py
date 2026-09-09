import asyncio
import contextlib
import json
import queue
import re
import threading
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing_extensions import Any
from urllib.parse import parse_qsl, urlparse

import httpx
import websockets
from websockets.asyncio.server import Server as WsServer

from truewire.generation.schema import Reference, Schema

from .project import Project, resolve, root_of
from .spec import client_http_examples, client_ws_examples
from .spec import HttpExample as SpecHttpExample
from .spec import WsExample as SpecWsExample
from .spec import (
  AfterRpcPush,
  ConnectPush,
  EnvelopeSpec,
  Push,
  RpcEnvelopeSpec,
  StreamEnvelopeSpec,
  read_dotted_path,
  rpc_selector,
  write_dotted_path,
)
from .spec.request import PLACEHOLDER, split_request_by_location


EXPECTED_PARAM_STATUS = 422
AMBIGUOUS_PARAM_STATUS = 409
WS_ERROR_TYPE = 'error'
DEFAULT_WS_PATH = '/ws'


def serve_response(payload: Any, request: Any, envelope: EnvelopeSpec | None) -> Any:
  """
  Prepare one recorded raw response for replay: apply declared correlation, nothing else.

  `payload` (`EnvelopeSpec.extract_value`, `truewire.spec.endpoint`) is a `truewire check`/docs
  concern, not a mock-serving one -- the live client under test runs its own real core,
  which unwraps the raw response exactly as it would against the upstream API.

  Args:
    payload: The recorded raw wire response.
    request: The incoming request body (HTTP) or parsed frame (WS), read for a
      correlation value.
    envelope: The matched example's endpoint-declared envelope, or `None`.
  """
  if envelope is None or envelope.correlate_paths is None:
    return payload
  paths = envelope.correlate_paths
  value = read_dotted_path(request, paths.request)
  if value is None or not isinstance(payload, dict):
    return payload
  return write_dotted_path(payload, paths.response, value)


@dataclass(frozen=True)
class MockResponse:
  status: int
  payload: Any


@dataclass(frozen=True)
class MockMatch:
  response: MockResponse
  endpoint_function: str
  example_id: str
  envelope: EnvelopeSpec | None = None
  rpc_method: str | None = None


@dataclass(frozen=True)
class EndpointExample:
  """Runtime-ready HTTP example derived from one `endpoint.json` plus one example pair.

  Request matching is split into path params, query params, and body because the
  recorded examples mix all three inside a single source envelope.
  """

  endpoint_path: Path
  function: str
  method: str
  route_pattern: re.Pattern[str]
  route_display: str
  example_id: str
  expected_path_params: dict[str, str]
  expected_query: dict[str, Any]
  redacted_names: frozenset[str]
  """Key names the endpoint declares as `redacted` -- transport-injected, never present in
  a recorded example, and stripped from the incoming request before comparing."""
  expected_body: Any
  raw_request: Any
  response: MockResponse
  envelope: EnvelopeSpec | None = None
  rpc_method: str | None = None

  def request_summary(self) -> Any:
    return self.raw_request


@dataclass(frozen=True)
class WsEndpointExample:
  """Runtime-ready websocket example built from one parameters/reply/messages triplet."""

  endpoint_path: Path
  function: str
  channel: str
  example_id: str
  expected_parameters: dict[str, Any]
  expected_payload: dict[str, Any] | None
  reply: Any
  messages: list[Any]
  message_frames: list[bytes]
  redacted_names: frozenset[str]
  """Key names the endpoint declares as `redacted`, stripped from both sides before a
  structural comparison -- see `_redact`."""
  rpc_method: str | None = None
  """The declared JSON-RPC method name, when this is a `kind: 'rpc'` example. `None` for
  a subscription or a non-JSON-RPC command."""
  envelope: EnvelopeSpec | None = None
  """This endpoint's own declared envelope, read straight off its `Endpoint`. `None` for
  the large majority of WS examples -- per `docs/spec/spec.md`'s WS Commands section, a
  command reply schema already describes the whole frame, so this is normally unset even
  for a `kind: 'rpc'` example; it exists for the rare API whose WS reply also needs a
  `correlate` substitution (JSON-RPC-style multiplexed `id` correlation)."""
  unsubscribe_reply: Any | None = None
  """The API's own captured unsubscribe acknowledgement
  (`spec.WsExample.unsubscribe_reply`), when this example recorded one. `None` for every
  example that hasn't -- a pure additive tier ahead of the declared-channel reply-reuse and
  synthesized fallbacks in `_handle`'s unsubscribe branch."""
  push: Push | None = None
  """This endpoint's own declared `push` trigger (`docs/spec/authoring.md` rule 11), read
  straight off its `Endpoint`. `None` for the large majority of WS examples, matched by an
  ordinary subscribe or RPC frame instead of pushed unprompted."""

  def request_summary(self) -> Any:
    return _ws_subscribe_message(self)


@dataclass(frozen=True)
class WsMockMatch:
  example: WsEndpointExample


@dataclass
class RunningWsServer:
  """Handle for a background websocket mock server.

  The server runs its own event loop in a thread so synchronous pytest fixtures
  can manage it with ordinary context managers.
  """

  client: str
  host: str
  port: int
  path: str
  loop: asyncio.AbstractEventLoop
  server: WsServer
  thread: threading.Thread

  @property
  def url(self) -> str:
    return f'ws://{self.host}:{self.port}{self.path}'

  def close(self):
    """Stop the websocket server and join its background thread."""

    async def shutdown():
      self.server.close()
      await self.server.wait_closed()

    future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
    future.result(timeout=5)
    self.loop.call_soon_threadsafe(self.loop.stop)
    self.thread.join(timeout=5)


@dataclass(frozen=True)
class RunningMockServers:
  """Convenience wrapper returned when HTTP and websocket mocks are started together."""

  http_server: ThreadingHTTPServer
  ws_server: RunningWsServer | None = None

  @property
  def http_base_url(self) -> str:
    host, port = self.http_server.server_address
    return f'http://{host}:{port}'


def _route_pattern(path: str) -> re.Pattern[str]:
  """Compile an endpoint path template into a regex with named path captures."""
  parts: list[str] = []
  cursor = 0
  for match in re.finditer(r'\{([^/}]+)\}', path):
    parts.append(re.escape(path[cursor : match.start()]))
    parts.append(f'(?P<{match.group(1)}>[^/]+)')
    cursor = match.end()
  parts.append(re.escape(path[cursor:]))
  return re.compile(rf'^{"".join(parts)}$')


def _normalize_json(value: Any) -> Any:
  if isinstance(value, dict):
    return {key: _normalize_json(value[key]) for key in sorted(value)}
  if isinstance(value, list):
    return [_normalize_json(item) for item in value]
  return value


def _normalize_query(value: Any) -> list[tuple[str, str]]:
  if not isinstance(value, dict):
    return []
  params = httpx.QueryParams(value)
  return sorted((key, item) for key, item in params.multi_items())


def query_matches(
  actual: list[tuple[str, str]], expected: list[tuple[str, str]]
) -> bool:
  """Compare a request's query against a recorded example's, by value rather than by text.

  Both sides are already normalized and sorted, so this is a pairwise comparison — with one
  exception. A typed client renders a `number` parameter from a Python `float`, so an
  example recording `amount: 10` is replayed as `amount=10.0` and never matches by text,
  even though the request is exactly the one recorded. The alternative is asking every
  spec to write `10.0` in JSON, which records the client's rendering rather than the API's
  call.

  Numbers are compared as numbers only when both sides parse as one, so a string parameter
  whose value happens to look numeric still has to match exactly. The same tolerance
  applies to a bool: a form-encoded body built with `urllib.parse.urlencode` renders
  `True`/`False` (Python's own `str()`), while `_normalize_query`'s `httpx.QueryParams`
  path renders `true`/`false` -- a form-encoded private POST hits this once query/body
  pooling (below) runs its literal-parameter values through this same comparison.

  Args:
    actual: Query items parsed off the incoming request.
    expected: Query items built from the example's `parameters`.
  """
  if len(actual) != len(expected):
    return False
  bools = {'true', 'false'}
  for (actual_key, actual_value), (expected_key, expected_value) in zip(
    actual, expected
  ):
    if actual_key != expected_key:
      return False
    if actual_value == expected_value:
      continue
    if actual_value.lower() in bools and expected_value.lower() in bools:
      if actual_value.lower() != expected_value.lower():
        return False
      continue
    try:
      if float(actual_value) != float(expected_value):
        return False
    except ValueError:
      return False
  return True


def _normalize_path_params(value: dict[str, Any]) -> dict[str, str]:
  return {key: str(item) for key, item in sorted(value.items())}


def _redact(value: Any, names: frozenset[str]) -> Any:
  """Strip declared credential/injected key names out of a dict before a structural compare.

  Args:
    value: The value to strip. Returned unchanged when it isn't a dict.
    names: Key names to remove. `value` is returned unchanged when this is empty.
  """
  if not names or not isinstance(value, dict):
    return value
  return {key: item for key, item in value.items() if key not in names}


def _flat_body_parameter_names(endpoint: SpecHttpExample) -> frozenset[str]:
  """Property names of a flat (non-union) `requestBody` record, recognized as additional
  query-role parameters for HTTP mock matching.

  Rule 0 (`docs/spec/authoring.md`) keeps `requestBody` for a discriminated-union payload
  (an `anyOf` of variant objects -- an order-placing `buy`/`sell`), which this deliberately does
  not touch: those parameters aren't individually named at all, so there's nothing here to
  recognize. A *flat* `requestBody` (one plain object schema, no `anyOf`) is a different
  case a REST client can still have -- its properties are real, individually named
  parameters that just happen to travel in the body rather than the query string, the same
  `in: 'query'`-is-a-role-not-a-wire-location idea ADR 0006 states for parameters proper.
  Mirrors what such a project's own codegen backend does at generation time (expanding the
  flat body into named parameters).

  Not keyed to `application/json`: a signed REST API's flat body may travel form-encoded
  on the wire (`application/x-www-form-urlencoded` declared on every flat `requestBody`,
  `application/json` reserved for JSON-RPC-shaped params), so the sole declared media type
  is read regardless of its key. More than one media type is a shape this doesn't have an
  answer for and is left alone, same as no media type at all.

  Kept for the legacy (`openapi`-shaped) branch of `_mock_http_example`, even though the
  end state -- every project migrated off `openapi`/`requestBody` -- has no need for it.
  Deleting it broke a real, verified case: a spec recording a flat body boolean
  (`new: true`) for a core that sends every private POST body form-urlencoded regardless of
  the spec's declared `application/json` content type, so the real wire value arrives as
  the literal string `"True"`. Without this function, `query_parameter_names` loses `new`,
  the endpoint falls through to the `rest_body_first` fallback, and `expected_body` becomes
  a non-`None` dict compared via `_normalize_json`'s exact-type equality -- `True != "True"`
  -- instead of `query_matches`'s bool-string-tolerant comparison, which is exactly what
  this same pooling mechanism was extended to tolerate.

  Args:
    endpoint: The loaded example, whose endpoint carries the operation's `requestBody`.
  """
  operation = endpoint.endpoint.openapi
  request_body = operation.request_body if operation is not None else None
  if isinstance(request_body, Reference) or request_body is None:
    return frozenset()
  content = request_body.content
  if len(content) != 1:
    return frozenset()
  schema = next(iter(content.values())).schema_
  if not isinstance(schema, Schema) or schema.properties is None or schema.anyOf:
    return frozenset()
  return frozenset(schema.properties.keys())


def _params_key(envelope: EnvelopeSpec | None) -> str:
  """Dotted path naming an RPC-shaped frame's arguments, declared or JSON-RPC's default."""
  if isinstance(envelope, RpcEnvelopeSpec) and envelope.params is not None:
    return envelope.params
  return 'params'


def _channel_key(envelope: EnvelopeSpec | None) -> str | None:
  """Dotted path into an outgoing subscribe frame naming its channel identity, when declared."""
  if isinstance(envelope, StreamEnvelopeSpec) and envelope.channel is not None:
    return envelope.channel
  return None


def _channel_matches(value: Any, channel: str) -> bool:
  """Compare a channel identity read off an incoming frame against one an example declares.

  `value` may be a list rather than a scalar -- a JSON-RPC dialect's subscribe/unsubscribe
  frames may carry `channels: [...]`, always one element for a per-channel stream method -- in
  which case this
  checks membership; any other shape compares by equality, unchanged from before this existed.
  """
  if isinstance(value, list):
    return channel in value
  return value == channel


def _verb_value(envelope: EnvelopeSpec | None, message: dict[str, Any]) -> str | None:
  """
  This message's own subscribe/unsubscribe intent, read off a declared `envelope.verb`.

  `None` means one of two different things the caller cannot tell apart from this return
  value alone: the endpoint declares no `verb` at all (hasn't migrated off mock.py's
  pre-declaration heuristics -- see `_connection_verb`), or it does declare one and this
  message's value at `verb.path` matches neither literal (malformed frame, or simply the
  wrong endpoint to ask). Both cases mean "this classifier has nothing to say," which is
  exactly the right thing for a caller trying several examples in turn.
  """
  if not isinstance(envelope, StreamEnvelopeSpec) or envelope.verb is None:
    return None
  value = read_dotted_path(message, envelope.verb.path)
  if value == envelope.verb.subscribe:
    return 'subscribe'
  if value == envelope.verb.unsubscribe:
    return 'unsubscribe'
  return None


def _connection_verb(
  examples: list['WsEndpointExample'], message: dict[str, Any]
) -> str | None:
  """
  This frame's declared subscribe/unsubscribe intent, tried against every example in the
  client's own corpus.

  A client speaks one wire dialect across its stream endpoints, so the first declared `verb`
  that resolves a value off this message settles it for the whole connection -- there is no
  per-endpoint ambiguity to resolve here, only whether this client's dialect has been
  declared at all. `None` when nothing in the corpus declares `verb` (this client hasn't
  migrated -- ADR 0004), which is exactly when the caller should fall back to `mock.py`'s
  pre-declaration heuristics (`type` sniffing, active-subscription-membership guessing)
  instead of refusing to serve anything.

  Args:
    examples: The client's full loaded example corpus (`WsMockRegistry.examples`).
    message: The parsed incoming WS frame.
  """
  for example in examples:
    verb = _verb_value(example.envelope, message)
    if verb is not None:
      return verb
  return None


def _expected_unsubscribe_frame(example: 'WsEndpointExample') -> dict[str, Any] | None:
  """
  The real unsubscribe frame for a declared-verb example, built by writing its own
  `verb.unsubscribe` literal to `verb.path` on a copy of the recorded subscribe payload.

  A precise, declared substitution -- not `_unsubscribe_payload_from_subscribe`'s blind
  recursive scan for any string that happens to read `"subscribe"`. `None` when the example
  declares no `verb` (not migrated -- caller falls back to the blind-swap heuristic) or has
  no recorded `payload` to copy in the first place.

  Args:
    example: The candidate subscribe example.
  """
  envelope = example.envelope
  if not isinstance(envelope, StreamEnvelopeSpec) or envelope.verb is None:
    return None
  if example.expected_payload is None:
    return None
  return write_dotted_path(example.expected_payload, envelope.verb.path, envelope.verb.unsubscribe)


def _json_rpc_call_matches(
  actual: Any,
  expected: Any,
  *,
  selector: str = 'method',
  params: str = 'params',
  redacted: frozenset[str] = frozenset(),
) -> bool:
  """
  Compare two RPC-shaped call frames by selector + params, ignoring `id` on both sides.

  Shared by HTTP's request-body match and WS's parsed-frame match -- `docs/spec/spec.md`'s
  documented rule that a recorded example's `id` is never part of the comparison, since a
  caller regenerates it per call. Defaults to JSON-RPC's own `method`/`params` keys; an API
  whose RPC frame uses different keys declares them via `envelope.selector`/`envelope.params`.

  When both sides' `params` parse as a dict (named/keyword-object RPC params), `redacted`
  keys are stripped from *both* sides before comparing, the same as every other redaction call
  site in this file -- a recorded example's own `params` dict can itself carry a placeholder
  for a transport-injected credential (an `authed_subscribe` recording a literal
  `access_token` placeholder inside its `payload`), and only stripping the actual side would
  leave that key uncancelled on the expected side. A positional (list) `params` is never
  redacted -- a redacted key is always keyed/named, never a bare positional element.

  Args:
    actual: The incoming request body or WS frame.
    expected: The recorded example's expected call, in the same shape as `actual`.
    selector: Dotted path naming the operation, read off both sides.
    params: Dotted path naming the arguments, read off both sides.
    redacted: Key names stripped from both sides' `params`, when both parse as a dict.
  """
  if not isinstance(actual, dict) or not isinstance(expected, dict):
    return False
  if read_dotted_path(actual, selector) != read_dotted_path(expected, selector):
    return False
  actual_params = read_dotted_path(actual, params)
  expected_params = read_dotted_path(expected, params)
  if isinstance(actual_params, dict) and isinstance(expected_params, dict):
    actual_params = _redact(actual_params, redacted)
    expected_params = _redact(expected_params, redacted)
  if not isinstance(actual_params, list) or not isinstance(expected_params, list):
    return actual_params == expected_params
  return _normalize_json(actual_params) == _normalize_json(expected_params)


def _request_match(
  example: EndpointExample, path: str, query_items: list[tuple[str, str]], body: Any
) -> bool:
  """Match one incoming HTTP request against one recorded example.

  Important matching rules:
  - JSON-RPC examples compare `method` plus the full `params` value, positional array
    or named object alike. A recorded `payload` is wrapped in a single-element list
    before comparing only when the real request's own `params` is itself a list (the
    API's method is genuinely positional, per spec-authoring rule 3) and the
    recorded payload isn't already one -- a named-object `params` (e.g.
    `{"currency": "BTC"}`) must reach `_json_rpc_call_matches` unwrapped, or it never
    matches the dict a real positional-free call actually sends. This covers 0-ary,
    single-object positional, N-ary positional, and named-object JSON-RPC methods
    with one comparison.
  - Path params are matched only when the example recorded them explicitly.
    This lets clients bake values like API keys into the base URL without
    forcing every example to repeat them.
  - Query params and body are matched independently so path substitutions do not
    pollute request-body comparisons -- *unless* the example recorded no `payload` at
    all (`expected_body is None`) and the real body is a dict, in which case query and
    body items are pooled before matching against `expected_query`: a `query`-role
    parameter's real wire channel is API/verb-dependent (ADR 0006), not something a
    `parameters`-recorded example claims either way.
  """
  if example.rpc_method is not None:
    selector = rpc_selector(example.envelope)
    params = _params_key(example.envelope)
    actual_params = read_dotted_path(body, params) if isinstance(body, dict) else None
    expected_params = example.expected_body
    if isinstance(actual_params, list) and not isinstance(expected_params, list):
      expected_params = [expected_params]
    return _json_rpc_call_matches(
      body,
      {selector: example.rpc_method, params: expected_params},
      selector=selector,
      params=params,
      redacted=example.redacted_names,
    )

  path_match = example.route_pattern.match(path)
  if not path_match:
    return False
  actual_path_params = _normalize_path_params(path_match.groupdict())
  expected_path_params = _normalize_path_params(example.expected_path_params)
  if any(
    actual_path_params.get(key) != value for key, value in expected_path_params.items()
  ):
    return False

  consumed_body = False
  if example.expected_body is None and isinstance(body, dict):
    # ADR 0006: `in: 'query'` states a parameter's *role*, not its literal wire
    # placement -- a query-role parameter can travel in the query string for one HTTP
    # verb and a form-encoded body for another (a signed POST/PUT), and a
    # `parameters`-recorded example (no `payload`) never claims which channel carried
    # it. Pool both into one set of items to match against `expected_query` rather than
    # requiring the query string alone to carry everything.
    # `_normalize_query`, not a bare `str(value)` -- a list-valued body field (a
    # `parameters`-recorded array) needs the same wire-shaped multi-item expansion the
    # "expected" side gets a few lines below, not Python's own `repr` of the list, which
    # can never equal it.
    body_items = _normalize_query(
      {key: value for key, value in body.items() if key not in example.redacted_names}
    )
    # `query_matches` does a pairwise compare against a pre-sorted `expected` side
    # (`_normalize_query` sorts it) -- `query_items` arrives pre-sorted from
    # `MockRequestHandler._handle` for the same reason, and appending unsorted
    # `body_items` after it silently breaks that invariant, failing the match for
    # any body carrying more than one parameter.
    query_items = sorted(query_items + body_items)
    consumed_body = True

  query_items = [
    (key, value) for key, value in query_items if key not in example.redacted_names
  ]
  # Symmetric with the actual side above: a redacted key that's also a genuine bindable
  # parameter (`docs/spec/spec.md`'s Declared Redaction section) is legitimately recorded
  # in `expected_query` too. Stripping only the actual side would force an exact match on a
  # value `redacted` says is never fixed -- the opposite of "redaction only widens what the
  # mock accepts" -- and make such an example permanently unmatchable.
  expected_query = _redact(example.expected_query, example.redacted_names)
  if not query_matches(query_items, _normalize_query(expected_query)):
    return False

  if example.expected_body is not None:
    # Symmetric with `expected_query` above, for the same reason: a redacted key legitimately
    # recorded in the body too must not be forced into an exact match.
    redacted_body = _redact(body, example.redacted_names)
    expected_body = _redact(example.expected_body, example.redacted_names)
    return _normalize_json(redacted_body) == _normalize_json(expected_body)

  return consumed_body or body is None


def load_http_examples(root: Path | Project) -> list[EndpointExample]:
  """Load all recorded HTTP examples for one project into matcher-friendly objects.

  Args:
    root: Project, or the project root directory holding its spec.
  """
  project = resolve(root)
  if not project.root.is_dir():
    raise ValueError(f'Unknown project root: {project.root}')

  examples: list[EndpointExample] = []
  for spec_example in client_http_examples(project):
    examples.append(_mock_http_example(spec_example, spec_root=project.spec_dir))
  return examples


def _mock_http_example(spec_example: SpecHttpExample, *, spec_root: Path) -> EndpointExample:
  """Convert a loaded spec HTTP example into mock matcher data.

  Args:
    spec_example: The loaded spec example.
    spec_root: The project's `spec/` directory -- resolves `endpoint.function`
      for a migrated endpoint that no longer authors one, the same way
      `resolve_endpoint_function` does. Read only as a fallback; an endpoint that still
      authors `function` keeps using that value unchanged.

  Branches on `endpoint.openapi`: a legacy `openapi`-shaped endpoint keeps the exact
  original logic (dual-shape). A new-shape endpoint has no `Operation` to read
  `parameters`/`requestBody` from -- its property names are split into path-templated vs.
  everything else by `split_request_by_location` instead, and every non-path-templated name is pooled into `expected_query`, never
  `expected_body`: `_request_match`'s query/body pooling, previously gated on
  `expected_body is None`, now fires unconditionally for one of these, since it is never
  set below. A `request` schema with no flat top-level `properties` (a discriminated
  `anyOf`, or no schema at all) has no individually named fields to pool, so the whole
  recorded value becomes `expected_body` instead -- mirroring how the legacy branch
  already treats a `requestBody`+`anyOf` operation.

  `spec_example.request.request` is preferred over the legacy
  `parameters`/`payload`/`kwargs`/`args` split wherever this function reads named request
  fields, in both branches: a collapsed-shape example carries every request field -- path,
  query, and body alike -- in that one dict, the same role `parameters` (and, in the legacy
  branch's own request body, `payload`) played before it. `request_parameters` tries
  `.request` before `.parameters` for path/query pooling and for the new-shape branch's
  whole-body fallback; the legacy `openapi` branch's own `request_body` tries `.request`
  before `.payload`, ahead of its pre-existing `.parameters` -> `.kwargs` -> `.args` -> `{}`
  chain. Falling back to each branch's already-existing order
  when `.request` is unset keeps every already-recorded example matching unchanged.
  """
  endpoint = spec_example.endpoint
  if endpoint.path is None or endpoint.method is None:
    raise ValueError(f'{spec_example.endpoint_path}: expected HTTP endpoint spec')

  path_spec = endpoint.path
  method = endpoint.method.upper()
  route_display = path_spec if path_spec.startswith('/') else '/'
  rpc_method = None if path_spec.startswith('/') else path_spec
  route_pattern = _route_pattern(route_display)
  request_parameters = (
    spec_example.request.request
    if spec_example.request.request is not None
    else spec_example.request.parameters
  )

  if endpoint.openapi is not None:
    parameters = endpoint.openapi.parameters or []
    path_parameter_names = [
      parameter.name for parameter in parameters if parameter.in_ == 'path'
    ]
    query_parameter_names = [
      parameter.name for parameter in parameters if parameter.in_ == 'query'
    ] + list(_flat_body_parameter_names(spec_example))
    request_body = (
      spec_example.request.request
      if spec_example.request.request is not None
      else spec_example.request.payload
    )
    rest_body_first = (
      rpc_method is None
      and endpoint.openapi.request_body is not None
      and not query_parameter_names
    )
    if (rpc_method is not None or rest_body_first) and request_body is None:
      # No `request`/`payload` and no (non-empty) `parameters` recorded -- fall back to
      # `kwargs` (a named-object call specified that way instead) or `args` (a positional
      # one), and only once none of the three carries anything does this settle on `{}`,
      # the wire shape of a genuinely argument-less JSON-RPC call. Dumping the whole
      # `ExampleRequest` model here (the previous fallback) leaked `description` into the
      # expected wire body, which could never match a real request and 422'd every 0-ary
      # rpc endpoint. `kwargs`/`args` are slated for removal (legacy,
      # superseded by `parameters`, itself superseded by `request`) -- once they're gone
      # this collapses to two branches (`parameters`/`{}`). `request_parameters` is already
      # `.request`-preferred (above), but `.request` is known `None` by this point (the
      # `request_body` assignment above already tried it), so this reduces to `.parameters`.
      if request_parameters:
        request_body = request_parameters
      elif spec_example.request.kwargs:
        request_body = spec_example.request.kwargs
      elif spec_example.request.args:
        request_body = spec_example.request.args
      else:
        request_body = {}
    expected_path_params = {
      name: request_parameters[name]
      for name in path_parameter_names
      if name in request_parameters
    }
    expected_query = {
      name: request_parameters[name]
      for name in query_parameter_names
      if name in request_parameters
    }
    expected_body = request_body
  else:
    request_schema = endpoint.request
    if rpc_method is not None and (request_schema or {}).get('properties'):
      # A JSON-RPC operation has no query string at all -- `_request_match`'s own rpc
      # branch (`example.rpc_method is not None`) compares `expected_body` against the
      # real wire `params` via `_json_rpc_call_matches` and never reads `expected_query`/
      # `expected_path_params`, so those stay empty and `request_parameters` (the whole
      # flat dict) becomes `expected_body` directly -- the same value the legacy
      # `openapi`-shaped branch above already builds for a `requestBody`-shaped RPC
      # operation. Falling through to the REST-shaped `split_request_by_location` branch
      # below (this design's original shape) always set `expected_body = None`
      # regardless of `rpc_method`, so no JSON-RPC operation with a flat `request` object
      # -- the common, default JSON-RPC wire shape (a whole `dict(request)`, wrapped in a
      # one-element `params` array) -- could ever match here; only a titled-`anyOf` or
      # parameterless RPC request happened to fall into the one branch that did set it.
      expected_path_params = {}
      expected_query = {}
      expected_body = request_parameters
    elif (request_schema or {}).get('properties'):
      locations = split_request_by_location(path_spec, request_schema)
      expected_path_params = {
        name: request_parameters[name]
        for name in locations.path_params
        if name in request_parameters
      }
      expected_query = {
        name: request_parameters[name]
        for name in locations.rest
        if name in request_parameters
      }
      expected_body = None
    else:
      # A discriminated-union (`anyOf`) `request`, or no `request` schema at all --
      # nothing individually named to split by location.
      expected_path_params = {}
      expected_query = {}
      if rpc_method is not None and not request_parameters and spec_example.request.payload is None:
        # A genuinely 0-ary JSON-RPC method (`spec.request is None`, e.g. a
        # `public/get_time`) records nothing under `request`/`parameters`, so
        # `request_parameters` is `{}` -- a real, correct value (the wire body always
        # sends `params: {}` for a parameterless call, confirmed against
        # `core/transport/http.py`'s own `build_body`), not a signal to fall through.
        # Collapsing it to `None` here (this branch's own bug until now, invisible to
        # every already-migrated client because none of them has a genuinely 0-ary
        # rpc-kind endpoint reaching this exact branch) makes `_json_rpc_call_matches`
        # compare the real `{}` against a recorded `None`, which never matches --
        # `UnexpectedRequestParameters` on every 0-ary call. Gated on `rpc_method is not
        # None`: a REST-shaped 0-ary `GET` (a `market.time`, also parameterless
        # and also reaching this same branch) sends no body at all, wire-`None`, not
        # `{}` -- forcing `{}` there regressed exactly that endpoint's own match.
        expected_body = request_parameters
      else:
        expected_body = request_parameters or spec_example.request.payload or None

  return EndpointExample(
    endpoint_path=spec_example.endpoint_path,
    function=endpoint.function or endpoint.resolved_function(spec_example.endpoint_path, spec_root),
    method=method,
    route_pattern=route_pattern,
    route_display=route_display,
    example_id=spec_example.example_id,
    expected_path_params=expected_path_params,
    expected_query=expected_query,
    redacted_names=endpoint.redacted_names,
    expected_body=expected_body,
    raw_request=spec_example.request.model_dump(exclude_none=True),
    response=MockResponse(
      status=spec_example.response.status,
      payload=spec_example.response.payload,
    ),
    envelope=endpoint.envelope,
    rpc_method=rpc_method,
  )


def load_ws_examples(root: Path | Project) -> list[WsEndpointExample]:
  """Load all recorded websocket examples for one project.

  Websocket examples are only considered complete when all three files exist:
  parameters, initial reply, and streamed messages.

  Args:
    root: Project, or the project root directory holding its spec.
  """
  project = resolve(root)
  if not project.root.is_dir():
    raise ValueError(f'Unknown project root: {project.root}')

  examples: list[WsEndpointExample] = []
  for spec_example in client_ws_examples(project):
    examples.append(_mock_ws_example(spec_example, spec_root=project.spec_dir))
  return examples


def _mock_ws_example(spec_example: SpecWsExample, *, spec_root: Path) -> WsEndpointExample:
  """Convert a loaded spec WS example into mock matcher data.

  Args:
    spec_example: The loaded spec example.
    spec_root: The project's `spec/` directory -- see `_mock_http_example`'s identical
      parameter for why.
  """
  endpoint = spec_example.endpoint
  if endpoint.channel is None:
    raise ValueError(f'{spec_example.endpoint_path}: expected WS endpoint spec')
  return WsEndpointExample(
    endpoint_path=spec_example.endpoint_path,
    function=endpoint.function or endpoint.resolved_function(spec_example.endpoint_path, spec_root),
    channel=endpoint.channel,
    example_id=spec_example.example_id,
    expected_parameters=spec_example.parameters.parameters,
    expected_payload=spec_example.parameters.payload,
    reply=spec_example.reply,
    unsubscribe_reply=spec_example.unsubscribe_reply,
    messages=spec_example.messages,
    message_frames=spec_example.message_frames,
    redacted_names=endpoint.redacted_names,
    rpc_method=endpoint.channel if endpoint.spec.kind == 'rpc' else None,
    envelope=endpoint.envelope,
    push=endpoint.push,
  )


class MockRegistry:
  """In-memory HTTP example index used by the mock server."""

  def __init__(self, root: Path | Project):
    self.root = root_of(root)
    self.examples = load_http_examples(root)

  def match(
    self, method: str, path: str, query_items: list[tuple[str, str]], body: Any
  ) -> MockMatch | None:
    """Return the one matching example, or raise when zero or several candidates match.

    `None` means the route (or JSON-RPC method) itself is unknown -- there is nothing here
    to compare against, and the caller (`MockRequestHandler._handle`) reports its own 404.
    Once at least one candidate is found, exactly one of them structurally matching is the
    only success; zero raises `UnexpectedRequestParameters`, more than one raises
    `AmbiguousRequestMatch` -- silently returning the first match (or the first candidate on
    no match) would hide a spec bug either way.
    """
    route_candidates = [
      example
      for example in self.examples
      if example.method == method and example.route_pattern.match(path)
    ]
    if not route_candidates:
      rpc_candidates = [
        example
        for example in self.examples
        if example.method == method and example.rpc_method is not None
      ]
      if isinstance(body, dict):
        route_candidates = [
          example
          for example in rpc_candidates
          if read_dotted_path(body, rpc_selector(example.envelope)) == example.rpc_method
        ]

    if not route_candidates:
      return None

    passing = [
      example
      for example in route_candidates
      if _request_match(example, path, query_items, body)
    ]
    if len(passing) == 1:
      example = passing[0]
      return MockMatch(
        response=example.response,
        endpoint_function=example.function,
        example_id=example.example_id,
        envelope=example.envelope,
        rpc_method=example.rpc_method,
      )
    if not passing:
      raise UnexpectedRequestParameters(route_candidates, method=method, path=path)
    raise AmbiguousRequestMatch(passing, method=method, path=path)


CHANNEL_PARAM = PLACEHOLDER
"""Placeholder in a channel template -- the one canonical `{placeholder}` rule
(`truewire.spec.request.PLACEHOLDER`). Previously excluded `/` in addition to `{}`
(`r'\\{([^/}]+)\\}'`); confirmed dead against real specs -- no project's `channel`
ever places a `/` or a nested `{` inside a placeholder's own braces (only outside them,
as in `/market/ticker:{symbol}`) -- so unifying onto the canonical pattern is
behavior-preserving."""


def _interpolated_channel(example: WsEndpointExample) -> str:
  """The literal channel identity a real subscribe frame carries for this example.

  `example.channel` alone is the endpoint's template (`/market/ticker:{symbol}`), never
  what actually goes on the wire. Two shapes of recorded example resolve it two ways:

  - Most APIs record the template's own placeholders as separate named parameters
    (`symbol`), so this substitutes each `{name}` from `expected_parameters`.
  - An API whose subscribe frame carries only the already-composed channel string, never
    its parts (`channels: ["book.BTC-PERPETUAL.100ms"]` -- there is no separate
    `instrument_name`/`interval` field on the wire to decompose it from) records that
    string directly, under whatever key `envelope.channel`'s own last path segment names.
    Reading it there first, before ever trying to substitute a template that has nothing
    to substitute from, is what a declared-channel example needs.
  """
  channel_key = _channel_key(example.envelope)
  if channel_key is not None:
    name = channel_key.rsplit('.', 1)[-1]
    value = example.expected_parameters.get(name)
    if isinstance(value, list) and value and isinstance(value[0], str):
      return value[0]
    if isinstance(value, str):
      return value

  def substitute(match: re.Match[str]) -> str:
    value = example.expected_parameters.get(match.group(1))
    return str(value) if value is not None else match.group(0)

  return CHANNEL_PARAM.sub(substitute, example.channel)


async def _push_example_messages(
  connection: 'websockets.ServerConnection', example: WsEndpointExample
):
  """
  Send one example's own declared push messages, verbatim, in recorded order.

  Shared by every path that pushes a matched subscription's messages -- the ordinary
  subscribe-match branch in `handler`, and the two `push`-triggered paths (`connect`,
  `after_rpc`) that never go through a subscribe match at all. Binary frames win when
  present, the same precedence `handler`'s own subscribe branch already used before this
  was extracted.

  Args:
    connection: The live websocket connection to push onto.
    example: The matched or push-triggered example whose messages to send.
  """
  if example.message_frames:
    for frame in example.message_frames:
      await connection.send(frame)
  else:
    # Replayed verbatim, exactly like an HTTP response: `messages.json` stores the
    # complete raw wire frame per entry (`docs/spec/spec.md`'s "Declared Envelope
    # Extraction" section), not an unwrapped value needing reconstruction.
    for notification in example.messages:
      await connection.send(json.dumps(notification))


def _synthesized_ack(example: WsEndpointExample, message: dict[str, Any]) -> Any | None:
  """
  A generic subscribe acknowledgement, for a declared-envelope stream with no recorded
  `reply`.

  A project whose stream endpoints declare no `reply` schema at all has nothing
  captured to replay -- the same gap `_ws_unsubscribe_message` already fills for unsubscribe
  acks it has no recorded data for either. `id` is echoed back when the incoming frame
  carried one, matching a real ack's own behavior.

  Args:
    example: The matched subscription example.
    message: The incoming subscribe frame.
  """
  if not isinstance(example.envelope, StreamEnvelopeSpec) or example.envelope.channel is None:
    return None
  ack: dict[str, Any] = {'type': 'ack'}
  if (message_id := message.get('id')) is not None:
    ack['id'] = message_id
  return ack


def _ws_subscription_key(channel: str, id: str | None) -> str:
  return f'{channel}:{id}' if id is not None else channel


def _ws_subscribe_message(example: WsEndpointExample) -> dict[str, Any]:
  if example.expected_payload is not None:
    return example.expected_payload
  msg: dict[str, Any] = {
    'type': 'subscribe',
    'channel': example.channel,
  }
  if example.expected_parameters.get('batched'):
    msg['batched'] = True
  if (channel_id := example.expected_parameters.get('id')) is not None:
    msg['id'] = channel_id
  return msg


def _subscribe_matches(example: WsEndpointExample, message: dict[str, Any]) -> bool:
  """Whether an incoming subscribe frame structurally matches one candidate example.

  A *declared-channel* example (`envelope.channel` set) with no recorded
  `payload` always matches once it is a candidate at all -- `channel` is the only thing its
  endpoint declared, so there is nothing else recorded to compare the live,
  per-connection `id`/`privateChannel`/`response` noise against, and a structural compare
  against the mock's own generic `_ws_subscribe_message` reconstruction would be meaningless
  (it cannot know that wire shape at all). A fallback-dialect example with no `payload` gets
  no such bypass -- `_ws_subscribe_message` correctly reconstructs the exact generic frame
  the client actually sends for it (`type`/`channel`, plus `id`/`batched` read off
  `expected_parameters`), so a full structural compare is meaningful and is what
  disambiguates two examples on the same channel by their recorded `id`.

  Otherwise both sides are compared structurally after stripping `example.redacted_names`
  from each -- symmetric, not actual-side-only, because the constructed expected frame
  (`_ws_subscribe_message`) can itself be built from a recorded `payload` carrying a
  placeholder for a transport-injected credential, the same reasoning as
  `_json_rpc_call_matches`.

  Args:
    example: The candidate example.
    message: The incoming subscribe frame.
  """
  if example.expected_payload is None and _channel_key(example.envelope) is not None:
    return True
  redacted_message = _redact(message, example.redacted_names)
  redacted_expected = _redact(_ws_subscribe_message(example), example.redacted_names)
  return _normalize_json(redacted_message) == _normalize_json(redacted_expected)


def _ws_unsubscribe_message(
  example: WsEndpointExample, message: dict[str, Any]
) -> dict[str, Any]:
  """
  Build the reply to an unsubscribe request.

  A declared-channel example gets the same generic `{type: 'ack'}` its real
  protocol expects for both subscribe and unsubscribe acks -- `_synthesized_ack`'s reasoning
  applies identically here; there is no separate "unsubscribed" concept on that wire dialect,
  and a client whose `parse_msg` only recognizes `ack`/`error` for a frame with no `topic`
  would otherwise wait forever on this reply. Every other dialect keeps the mock's
  own synthetic `{'type': 'unsubscribed', ...}` shape, unchanged.
  """
  if _channel_key(example.envelope) is not None:
    return _synthesized_ack(example, message) or {'type': 'ack'}
  if payload := example.expected_payload:
    return {'type': 'unsubscribed', 'request': payload}
  msg: dict[str, Any] = {
    'type': 'unsubscribe',
    'channel': example.channel,
  }
  if (channel_id := example.expected_parameters.get('id')) is not None:
    msg['id'] = channel_id
  return msg


def _ws_error_message(
  *,
  connection_id: str,
  message_id: int,
  message: str,
  details: dict[str, Any] | None = None,
  request: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Build an error frame for the WS mock server.

  Args:
    connection_id: The mock's own per-connection identifier.
    message_id: The mock's own synthetic per-connection message counter.
    message: A short machine-readable error code.
    details: Extra diagnostic data, when the caller has any.
    request: The incoming frame that triggered this error, read for its own top-level `id`
      (when present) and echoed back as `id` alongside `message_id` -- a JSON-RPC-shaped
      client's `parse_msg` only recognizes a frame as a
      `Response` when it carries a top-level `id`; without this, every mock WS error frame
      is silently dropped by such a client instead of surfacing as a fast, visible failure.
  """
  payload: dict[str, Any] = {
    'connection_id': connection_id,
    'message_id': message_id,
    'type': WS_ERROR_TYPE,
    'message': message,
  }
  if request is not None and (id := request.get('id')) is not None:
    payload['id'] = id
  if details is not None:
    payload['details'] = details
  return payload


def _ws_error_candidates(candidates: list[WsEndpointExample]) -> list[dict[str, Any]]:
  """Build the `candidates` list shared by the WS mock's subscribe/RPC error frames.

  Args:
    candidates: The examples an unexpected- or ambiguous-match exception carries.
  """
  return [
    {
      'function': candidate.function,
      'example_id': candidate.example_id,
      'channel': candidate.channel,
      'expected_request': candidate.request_summary(),
    }
    for candidate in candidates
  ]


def _ws_example_key(example: WsEndpointExample) -> str:
  return _ws_subscription_key(
    _interpolated_channel(example),
    example.expected_parameters.get('id'),
  )


def _unsubscribe_payload_from_subscribe(payload: Any) -> Any | None:
  """Best-effort, API-agnostic guess at an unsubscribe request's shape, derived from a
  captured subscribe `payload`: swap every string value that reads literally `"subscribe"`
  for `"unsubscribe"`, recursively through dicts and lists. Returns `None` when no such
  value is found anywhere in the payload.

  Superseded, for a migrated endpoint, by `_expected_unsubscribe_frame`'s precise write to a
  declared `envelope.verb.path` (ADR 0004) -- this function is now purely the fallback for a
  client whose stream endpoints don't declare `verb` yet, kept exactly as it worked before
  that field existed. It stays a blind guess for exactly that reason: without a declaration
  to read, there is nothing else to go on. `_match_active_subscription` only reaches this
  once `_expected_unsubscribe_frame` has already returned `None` for the same example.

  This was originally written as the convention a wide range of APIs already use for a
  subscribe/unsubscribe pair that otherwise shares one frame shape (`{"op":
  "subscribe", ...}` -> `{"op": "unsubscribe", ...}` was the motivating case, since migrated
  onto a real `verb` declaration instead of this guess). An API whose dialect doesn't follow
  this convention (a different literal, or no shared frame shape at all) simply finds nothing
  to swap, and `payload` matching for its unsubscribe frames stays exactly as unsupported as
  before this existed -- this is purely additive, never a wrong guess that replaces a real one.

  Args:
    payload: A recorded subscribe request, structurally.
  """
  found = False

  def swap(value: Any) -> Any:
    nonlocal found
    if isinstance(value, dict):
      return {k: swap(v) for k, v in value.items()}
    if isinstance(value, list):
      return [swap(v) for v in value]
    if value == 'subscribe':
      found = True
      return 'unsubscribe'
    return value

  swapped = swap(payload)
  return swapped if found else None


def _match_active_subscription(
  active: dict[str, WsEndpointExample], message: dict[str, Any]
) -> str | None:
  """
  Find `active`'s key for an incoming unsubscribe frame.

  Mirrors `WsMockRegistry.match_subscribe`'s three routing paths (declared-channel,
  payload-derived, generic-dialect), since the active-subscription key an example was
  stored under (`_ws_example_key`, set at subscribe time) and the identity an unsubscribe
  frame carries are the same shapes `_subscribe_matches` already recognizes for the
  original subscribe request.

  Args:
    active: Subscriptions currently open on this connection, keyed by `_ws_example_key`.
    message: The incoming unsubscribe frame.
  """
  channel = message.get('channel')
  for key, example in active.items():
    channel_key = _channel_key(example.envelope)
    if channel_key is not None:
      if _channel_matches(read_dotted_path(message, channel_key), _interpolated_channel(example)):
        return key
    elif (expected := _expected_unsubscribe_frame(example)) is not None:
      # Declared-verb path (ADR 0004): precise, not a guess -- see `_expected_unsubscribe_
      # frame`. Tried before the blind-swap fallback below for every migrated example.
      redacted_message = _redact(message, example.redacted_names)
      redacted_expected = _redact(expected, example.redacted_names)
      if _normalize_json(redacted_message) == _normalize_json(redacted_expected):
        return key
    elif example.expected_payload is not None:
      derived = _unsubscribe_payload_from_subscribe(example.expected_payload)
      if derived is not None:
        redacted_message = _redact(message, example.redacted_names)
        redacted_derived = _redact(derived, example.redacted_names)
        if _normalize_json(redacted_message) == _normalize_json(redacted_derived):
          return key
    elif key == _ws_subscription_key(str(channel or ''), message.get('id')):
      return key
  return None


class WsMockRegistry:
  """In-memory websocket example index used by the WS mock server."""

  def __init__(self, root: Path | Project):
    self.root = root_of(root)
    self.examples = load_ws_examples(root)

  def has_examples(self) -> bool:
    return bool(self.examples)

  def has_payload_examples(self) -> bool:
    """Return whether any example records the API's own subscribe frame.

    An example carrying a `payload` replays the frame the API really receives —
    one API sends `{'method': 'sub.depth', 'param': {...}}`, another sends
    `{'method': 'SUBSCRIPTION', 'params': [...]}`. Those frames carry no `type`
    field, because only the mock's own synthetic dialect (built by
    `_ws_subscribe_message` from `parameters` alone) has one. The WS handler
    therefore routes on this rather than assuming every subscribe frame is
    self-describing.
    """
    return any(example.expected_payload is not None for example in self.examples)

  def push_only_examples(self) -> list[WsEndpointExample]:
    """Return every example declaring a `push` trigger (`docs/spec/authoring.md` rule 11).

    A push-only example is never reached through `match_subscribe`/`match_rpc` -- it has no
    subscribe frame of its own to be matched against -- so `start_ws_server` computes this
    once, the same way it computes `has_payload_examples()`, and partitions the result by
    trigger before the per-connection handler ever runs. Empty for the large majority of
    clients, which declare no `push` at all.
    """
    return [example for example in self.examples if example.push is not None]

  def match_rpc(self, message: dict[str, Any]) -> WsMockMatch | None:
    """
    Match an incoming JSON-RPC-shaped WS call against declared `kind: 'rpc'` examples.

    Candidates are identified by rekeying off the *incoming* message's own selector value,
    not an example's own recorded payload: reading the selector off the example's payload
    (the previous approach) can never recognize a genuine zero-arg command, since there is
    no recorded `payload` at all to read a method name out of, and `None` never equals a
    real method name. A project with no WS RPC examples (every project but a
    JSON-RPC-over-WS one) never produces a candidate, and this stays non-raising in that case -- a genuine
    subscribe/unsubscribe frame must fall through to the normal dispatch untouched.

    Once at least one candidate recognizes the method, exactly one structurally matching is
    the only success. Zero raises `UnexpectedSubscriptionParameters` -- the method is known
    but no candidate's params match, previously a silently swallowed `None` that left a
    caller's WS RPC call hanging forever waiting for a reply that would never come. More than
    one raises `AmbiguousSubscriptionMatch`.

    Args:
      message: The parsed incoming WS frame.
    """
    candidates = [
      example
      for example in self.examples
      if example.rpc_method is not None
      and read_dotted_path(message, rpc_selector(example.envelope))
      == example.rpc_method
    ]
    if not candidates:
      return None

    passing = []
    for example in candidates:
      selector = rpc_selector(example.envelope)
      params = _params_key(example.envelope)
      # A genuine zero-arg command records no `payload` at all -- there is nothing to
      # compare against but the method name itself, so the expected frame is synthesized
      # with no params.
      expected = (
        example.expected_payload
        if example.expected_payload is not None
        else {selector: example.rpc_method}
      )
      matches = _json_rpc_call_matches(
        message,
        expected,
        selector=selector,
        params=params,
        redacted=example.redacted_names,
      )
      if matches:
        passing.append(example)

    if len(passing) == 1:
      return WsMockMatch(example=passing[0])
    if not passing:
      raise UnexpectedSubscriptionParameters(candidates, message=message)
    raise AmbiguousSubscriptionMatch(passing, message=message)

  def declared_channel_candidates(
    self, message: dict[str, Any]
  ) -> list[WsEndpointExample]:
    """Examples whose declared `envelope.channel` identity matches the incoming message.

    Exposed separately from `match_subscribe` so the WS server loop can check, before ever
    trying `match_rpc`, whether this message has a specific declared-channel routing home.
    An API's generic multiplexed subscribe method (`public/subscribe`) is
    frequently byte-identical, on the wire, to what a caller of one *specific* declared-
    channel stream sends -- the declared-channel candidate is always the correct answer
    when one exists, regardless of whether the generic method also happens to have a
    recorded example that matches.
    """
    return [
      example
      for example in self.examples
      if (channel_key := _channel_key(example.envelope)) is not None
      and _channel_matches(
        read_dotted_path(message, channel_key), _interpolated_channel(example)
      )
    ]

  def match_subscribe(self, message: dict[str, Any]) -> WsMockMatch | None:
    """
    Match an incoming subscribe request against recorded websocket examples.

    Routes first on `envelope.channel` for examples that declare it (`declared_channel_
    candidates`) -- an API whose subscribe dialect is not the mock's own generic `{type,
    channel}` shape (`{id, type, topic, ...}`, for one) otherwise has no `channel`
    key for the fallback below to key off, and every payload-dialect example becomes a
    candidate regardless of which channel it actually names. Declared candidates always win
    over the fallback pool when both are non-empty.

    Within the chosen pool, exactly one structurally matching candidate (`_subscribe_matches`)
    is the only success. A channel-identity-only example matches unconditionally once it is a
    candidate at all -- `channel` is the only field its endpoint declared, so there is nothing
    else recorded to compare the live, per-connection `id`/`privateChannel`/`response`
    noise against -- but it does *not* win merely by being first: a payload-specific sibling
    on the same channel that also matches makes this ambiguous, not "the specific one wins",
    since both examples are equally valid candidates for what the caller actually sent. Zero
    matches raises `UnexpectedSubscriptionParameters`; more than one raises
    `AmbiguousSubscriptionMatch`.
    """
    declared_candidates = self.declared_channel_candidates(message)
    if declared_candidates:
      fallback_candidates: list[WsEndpointExample] = []
    else:
      channel = message.get('channel')
      fallback_candidates = [
        example
        for example in self.examples
        if _channel_key(example.envelope) is None
        and (example.channel == channel or example.expected_payload is not None)
      ]

    candidates = declared_candidates if declared_candidates else fallback_candidates
    if not candidates:
      return None

    passing = [
      example for example in candidates if _subscribe_matches(example, message)
    ]
    if len(passing) == 1:
      return WsMockMatch(example=passing[0])
    if not passing:
      raise UnexpectedSubscriptionParameters(candidates, message=message)
    raise AmbiguousSubscriptionMatch(passing, message=message)


class UnexpectedSubscriptionParameters(Exception):
  def __init__(self, candidates: list[WsEndpointExample], *, message: dict[str, Any]):
    self.candidates = candidates
    self.message = message
    channel = str(message.get('channel', '<unknown>'))
    super().__init__(f'Unexpected subscription parameters for {channel}')


class AmbiguousSubscriptionMatch(Exception):
  def __init__(self, candidates: list[WsEndpointExample], *, message: dict[str, Any]):
    self.candidates = candidates
    self.message = message
    channel = str(message.get('channel', '<unknown>'))
    super().__init__(f'Ambiguous subscription parameters for {channel}')


class UnexpectedRequestParameters(Exception):
  def __init__(self, candidates: list[EndpointExample], *, method: str, path: str):
    self.candidates = candidates
    self.method = method
    self.path = path
    super().__init__(f'Unexpected parameters for {method} {path}')


class AmbiguousRequestMatch(Exception):
  def __init__(self, candidates: list[EndpointExample], *, method: str, path: str):
    self.candidates = candidates
    self.method = method
    self.path = path
    super().__init__(f'Ambiguous parameters for {method} {path}')


def _request_error_candidates(
  candidates: list[EndpointExample],
) -> list[dict[str, Any]]:
  """Build the `candidates` JSON body shared by the HTTP mock's 422/409 error responses.

  Args:
    candidates: The examples an unexpected- or ambiguous-match exception carries.
  """
  return [
    {
      'function': candidate.function,
      'example_id': candidate.example_id,
      'route': candidate.route_display,
      'expected_request': candidate.request_summary(),
    }
    for candidate in candidates
  ]


class MockRequestHandler(BaseHTTPRequestHandler):
  registry: MockRegistry

  def _read_body(self) -> Any:
    """Decode JSON requests eagerly because example matching is JSON-structural.

    A form-urlencoded body (a signed POST/PUT) is decoded into a dict too --
    left as a raw string, it can never structurally `==` a recorded dict, which is what
    `_request_match`'s body comparison needs.
    """
    length = int(self.headers.get('content-length', '0'))
    if length <= 0:
      return None
    raw = self.rfile.read(length)
    if not raw:
      return None
    content_type = self.headers.get('content-type', '')
    if 'application/json' in content_type:
      return json.loads(raw.decode())
    if 'application/x-www-form-urlencoded' in content_type:
      return dict(parse_qsl(raw.decode()))
    return raw.decode()

  def _send_cors(self):
    """Allow any origin, so a browser client can be tested against the mock.

    A generated client that runs in a browser cannot reach a mock that answers without
    `Access-Control-Allow-Origin`: the fetch is blocked before the response is read, and
    a preflight that 501s blocks it before the request is even made. The mock serves
    recordings on loopback to whoever asks, so there is nothing here to protect with a
    narrower policy.
    """
    self.send_header('Access-Control-Allow-Origin', '*')
    self.send_header('Access-Control-Allow-Methods', 'DELETE, GET, OPTIONS, PATCH, POST, PUT')
    self.send_header('Access-Control-Allow-Headers', '*')
    self.send_header('Access-Control-Max-Age', '86400')

  def _write_json(self, status: int, payload: Any):
    body = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header('Content-Type', 'application/json')
    self.send_header('Content-Length', str(len(body)))
    self._send_cors()
    self.end_headers()
    self.wfile.write(body)

  def _handle(self):
    parsed = urlparse(self.path)
    body = self._read_body()
    query_items = sorted(parse_qsl(parsed.query, keep_blank_values=True))

    try:
      match = self.registry.match(self.command, parsed.path, query_items, body)
    except UnexpectedRequestParameters as exc:
      self._write_json(
        EXPECTED_PARAM_STATUS,
        {
          'error': 'unexpected_parameters',
          'method': exc.method,
          'path': exc.path,
          'candidates': _request_error_candidates(exc.candidates),
        },
      )
      return
    except AmbiguousRequestMatch as exc:
      self._write_json(
        AMBIGUOUS_PARAM_STATUS,
        {
          'error': 'ambiguous_parameters',
          'method': exc.method,
          'path': exc.path,
          'candidates': _request_error_candidates(exc.candidates),
        },
      )
      return

    if match is None:
      self._write_json(
        HTTPStatus.NOT_FOUND,
        {
          'error': 'unknown_request',
          'method': self.command,
          'path': parsed.path,
        },
      )
      return

    payload = serve_response(match.response.payload, body, match.envelope)
    self._write_json(match.response.status, payload)

  def do_OPTIONS(self):
    """Answer the preflight a browser sends before a cross-origin call."""
    self.send_response(204)
    self.send_header('Content-Length', '0')
    self._send_cors()
    self.end_headers()

  def do_DELETE(self):
    self._handle()

  def do_GET(self):
    self._handle()

  def do_PATCH(self):
    self._handle()

  def do_POST(self):
    self._handle()

  def do_PUT(self):
    self._handle()

  def log_message(self, format: str, *args):
    return


def build_server(
  root: Path | Project,
  *,
  host: str = '127.0.0.1',
  port: int = 0,
) -> ThreadingHTTPServer:
  """Create an HTTP mock server for one project without starting it.

  Args:
    root: Project, or the project root directory holding its spec.
    host: Bind address.
    port: Bind port, or 0 to pick a free one.
  """
  registry = MockRegistry(root)

  class Handler(MockRequestHandler):
    pass

  Handler.registry = registry
  return ThreadingHTTPServer((host, port), Handler)


def _ws_path() -> str:
  """Return the websocket path exposed by the mock server.

  This is intentionally a single shared default for now; the path is not driven
  from endpoint metadata.
  """
  return DEFAULT_WS_PATH


def start_ws_server(
  root: Path | Project,
  *,
  host: str = '127.0.0.1',
  port: int = 0,
) -> RunningWsServer:
  """Start a websocket mock server in a background thread and return its handle.

  The server replays recorded subscribe replies and subsequent messages, and it
  synthesizes `unsubscribed` replies because those are not stored in the example
  corpus.

  Args:
    root: Project, or the project root directory holding its spec.
    host: Bind address.
    port: Bind port, or 0 to pick a free one.
  """
  registry = WsMockRegistry(root)
  if not registry.has_examples():
    raise ValueError(f'Project has no websocket examples: {root_of(root)}')

  payload_dialect = registry.has_payload_examples()
  # `docs/spec/authoring.md` rule 11: partitioned once per server, not per connection or per
  # message, the same way `payload_dialect` above is -- a push-only example is never reached
  # through `match_subscribe`/`match_rpc`, so there is nothing hot-path about resolving which
  # trigger it declares.
  connect_push_examples = [
    example
    for example in registry.push_only_examples()
    if isinstance(example.push, ConnectPush)
  ]
  after_rpc_push_examples: dict[str, list[WsEndpointExample]] = {}
  for example in registry.push_only_examples():
    if isinstance(example.push, AfterRpcPush):
      after_rpc_push_examples.setdefault(example.push.method, []).append(example)
  started: queue.Queue[
    tuple[asyncio.AbstractEventLoop, WsServer, int, threading.Thread] | Exception
  ] = queue.Queue(maxsize=1)
  path = _ws_path()

  def run():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def handler(connection: websockets.ServerConnection):
      connection_id = str(uuid.uuid4())
      message_id = 0
      active: dict[str, WsEndpointExample] = {}

      # `connect`-triggered push (`docs/spec/authoring.md` rule 11): pushed the instant the
      # connection is accepted, before a single frame has been read from it -- a
      # private user-data stream is this shape (listenKey in the URL, no outgoing frame
      # ever sent). Never touches `active` or the message loop below; a push-only example
      # is never a candidate for `match_subscribe`/`match_rpc` in the first place.
      for push_example in connect_push_examples:
        await _push_example_messages(connection, push_example)

      async for raw_message in connection:
        message_id += 1
        if isinstance(raw_message, bytes):
          raw_message = raw_message.decode()
        try:
          message = json.loads(raw_message)
        except json.JSONDecodeError:
          await connection.send(
            json.dumps(
              _ws_error_message(
                connection_id=connection_id,
                message_id=message_id,
                message='invalid_json',
              )
            )
          )
          continue

        if not isinstance(message, dict):
          await connection.send(
            json.dumps(
              _ws_error_message(
                connection_id=connection_id,
                message_id=message_id,
                message='invalid_message',
              )
            )
          )
          continue

        msg_type = message.get('type')

        # Unsubscribe intent is resolved first, before either registry is consulted at all --
        # neither `match_rpc` nor `match_subscribe` understands "remove an existing
        # subscription" semantics, only this connection's own `active` bookkeeping does.
        #
        # `verb` (ADR 0004, `truewire.spec.endpoint`) is the real,
        # declared signal when this client's stream endpoints have migrated onto it: it
        # reads the frame's own stated intent, so it resolves both the case the old
        # heuristic below got right (an explicit `type == 'unsubscribe'`) and the one it got
        # wrong -- a genuine *subscribe* to a channel already active, on a dialect with no
        # `type` key, no longer gets misread as an unsubscribe merely because `active`
        # happens to already contain that channel. A client not yet migrated (`verb is
        # None` for every loaded example) falls straight through to the pre-declaration
        # heuristic, unchanged:
        #
        # A message that resolves to something already active is treated as an unsubscribe
        # attempt on it regardless of which registry's example it might also coincidentally
        # match: a JSON-RPC dialect's generic `public/unsubscribe`/`private/unsubscribe`
        # method carries the exact same wire shape as `public/subscribe`/`private/subscribe` (the
        # same `channels` param), and the two are distinguishable only by which higher-level
        # call the real caller made, not by frame shape alone -- `active` membership is the
        # only signal available for a dialect whose frames carry no `type` key at all
        # (one not yet migrated onto `verb`). The explicit `type == 'unsubscribe'`
        # dialect signal is authoritative when present, and is checked first so an explicit
        # `type == 'subscribe'` on a generic-dialect frame is never overridden by this (a
        # legitimate re-subscribe to an already-active channel stays a subscribe, exactly as
        # before this existed).
        active_key = _match_active_subscription(active, message)
        verb = _connection_verb(registry.examples, message)
        is_unsubscribe = (
          verb == 'unsubscribe' if verb is not None
          else msg_type == 'unsubscribe' or (msg_type is None and active_key is not None)
        )
        if is_unsubscribe:
          # Prefer `match_rpc`'s own recorded reply when it cleanly resolves this message --
          # the generic `public/unsubscribe`/`private/unsubscribe` RPC method has its
          # own captured example with the real JSON-RPC ack shape, which is more accurate
          # than a synthesized one. `match_subscribe` (subscribe-registration logic) is never
          # consulted here -- it has no unsubscribe concept at all. Either way `active` is
          # popped in this branch, not left to whichever reply path happens to run. A
          # genuinely ambiguous `match_rpc` result (more than one recorded example matches)
          # still surfaces as `ambiguous_parameters` -- the example corpus is at fault the
          # same way it would be for any other message, so it isn't silently downgraded to
          # the no-match fallback below.
          try:
            rpc_match = registry.match_rpc(message)
          except UnexpectedSubscriptionParameters:
            rpc_match = None
          except AmbiguousSubscriptionMatch as exc:
            await connection.send(
              json.dumps(
                _ws_error_message(
                  connection_id=connection_id,
                  message_id=message_id,
                  message='ambiguous_parameters',
                  details={
                    'request': exc.message,
                    'candidates': _ws_error_candidates(exc.candidates),
                  },
                  request=message,
                )
              )
            )
            continue

          example = active.pop(active_key, None) if active_key is not None else None

          if rpc_match is not None:
            reply = (
              serve_response(
                rpc_match.example.reply, message, rpc_match.example.envelope
              )
              if rpc_match.example.reply is not None
              else None
            )
            if reply is not None:
              await connection.send(json.dumps(reply))
            continue

          if example is None:
            await connection.send(
              json.dumps(
                _ws_error_message(
                  connection_id=connection_id,
                  message_id=message_id,
                  message='unknown_subscription',
                  details={'request': message},
                  request=message,
                )
              )
            )
            continue

          # A captured, real unsubscribe ack recorded specifically for this subscription
          # example (`examples/<id>.unsubscribe_reply.json`, `WsEndpointExample.unsubscribe_
          # reply`) is authentic data, not an approximation -- it outranks every fallback
          # below regardless of dialect, declared-channel or generic alike. This is the tier
          # a `ticker` channel needs when its real unsubscribe ack reuses the exact
          # subscribe-ack envelope shape (`{channel: 'subscriptions', timestamp, sequence_
          # num, events}`, no `type` key), which no declared-channel or synthesized fallback
          # below can produce, and declares no `envelope.channel` at all so it could
          # never reach the declared-channel reuse tier either.
          if example.unsubscribe_reply is not None:
            reply = serve_response(example.unsubscribe_reply, message, example.envelope)
            await connection.send(json.dumps(reply))
            continue

          # No recorded unsubscribe example matched, and this example recorded no dedicated
          # `unsubscribe_reply` of its own either. A declared-channel example that *does*
          # record its own subscribe reply belongs
          # to a dialect whose replies are always envelope-shaped (`{jsonrpc, id, result}`),
          # not the generic `{type: 'ack'}` `_ws_unsubscribe_message` synthesizes below for a
          # dialect that never records one at all (`example.reply is None`
          # is what actually excludes them here, not `_channel_key` alone: a declared-channel
          # example with no recorded reply falls straight through to that same generic path).
          # Reusing the original subscribe reply -- re-correlated to this unsubscribe
          # request's own id -- keeps every reply on such a dialect schema-valid without
          # inventing new content; it is not a semantically perfect "channels remaining"
          # answer, but nothing recorded exists to give one for a channel `match_rpc`'s own
          # example didn't happen to capture. This assumes a declared-channel dialect's own
          # subscribe-reply shape reads correctly as an unsubscribe ack too (a
          # `result: [channel names]` does); an API whose subscribe reply looks nothing like
          # its unsubscribe ack -- or that has no `envelope.channel` at all -- needs its own
          # recorded `unsubscribe_reply` instead, which is exactly the tier just above this.
          if _channel_key(example.envelope) is not None and example.reply is not None:
            reply = serve_response(example.reply, message, example.envelope)
            await connection.send(json.dumps(reply))
            continue

          unsubscribed = _ws_unsubscribe_message(example, message)
          if _channel_key(example.envelope) is None:
            unsubscribed.update(
              {
                'connection_id': connection_id,
                'message_id': message_id,
                'type': 'unsubscribed',
              }
            )
          await connection.send(json.dumps(unsubscribed))
          continue

        # A `payload_dialect` registry's subscribe frames have no `type` to dispatch on, so
        # this is the same "could this also be a subscribe attempt" condition the dispatch
        # below re-checks once more explicitly; computed here so a `match_rpc` "recognized
        # method, mismatched params" failure can be deferred to `match_subscribe` instead of
        # raised immediately -- a generic `public/subscribe`/`private/subscribe` RPC
        # method is also a candidate under `match_rpc` (one recorded example channel), so a
        # request for any other channel must still reach `match_subscribe`'s declared-channel
        # routing (`streams.*`) rather than fail on the RPC candidate's mismatch alone.
        # `msg_type == 'unsubscribe'` can no longer reach this point (handled above), so the
        # exclusion below is dead for that case but kept for clarity/safety.
        will_try_subscribe = msg_type == 'subscribe' or (
          payload_dialect and msg_type != 'unsubscribe'
        )
        rpc_unexpected = None

        # A message whose channel identity matches a *declared-channel* example
        # (`streams.*`) is always routed through that candidate, never through `match_rpc`
        # -- a declared-channel candidate is inherently more specific (channel-scoped) than
        # a generic RPC method match (method-name-scoped only). Without this, an API whose
        # generic multiplexed subscribe method (`public/subscribe`) happens to
        # have a recorded example for the exact channel a declared-channel stream also
        # covers would have `match_rpc` claim the message first -- a clean, non-raising
        # match -- and the caller would get an ack but never be registered for pushes.
        # Unsubscribe intent was already resolved and dispatched above, so every message
        # reaching this point is a subscribe attempt (or a plain RPC call) -- no
        # neutralization is needed here.
        declared_candidates = registry.declared_channel_candidates(message)

        rpc_match = None
        if not declared_candidates:
          try:
            rpc_match = registry.match_rpc(message)
          except UnexpectedSubscriptionParameters as exc:
            if not will_try_subscribe:
              await connection.send(
                json.dumps(
                  _ws_error_message(
                    connection_id=connection_id,
                    message_id=message_id,
                    message='unexpected_parameters',
                    details={
                      'request': exc.message,
                      'candidates': _ws_error_candidates(exc.candidates),
                    },
                    request=message,
                  )
                )
              )
              continue
            rpc_unexpected = exc
          except AmbiguousSubscriptionMatch as exc:
            await connection.send(
              json.dumps(
                _ws_error_message(
                  connection_id=connection_id,
                  message_id=message_id,
                  message='ambiguous_parameters',
                  details={
                    'request': exc.message,
                    'candidates': _ws_error_candidates(exc.candidates),
                  },
                  request=message,
                )
              )
            )
            continue

        if rpc_match is not None:
          example = rpc_match.example
          reply = (
            serve_response(example.reply, message, example.envelope)
            if example.reply is not None
            else None
          )
          if reply is not None:
            await connection.send(json.dumps(reply))
          # `after_rpc`-triggered push (`docs/spec/authoring.md` rule 11): once this RPC
          # example's own reply has been served -- or skipped, for a no-reply RPC like
          # an `authenticate` -- any endpoint declaring `push.method` equal to this
          # one's own `rpc_method` starts pushing its declared messages unprompted, with no
          # subscribe frame of its own ever sent. Gated on `example.rpc_method`, the method
          # this specific matched example answers to, not on the incoming message's raw
          # selector value, so an RPC method with several examples only fires the push once
          # the example actually served is the gating one.
          for push_example in after_rpc_push_examples.get(example.rpc_method or '', []):
            await _push_example_messages(connection, push_example)
          continue

        # An API-dialect registry replays the API's own subscribe frames, which have no
        # `type` to dispatch on, so anything that is not an explicit `unsubscribe` goes to
        # the matcher. `unsubscribe` stays out of it in both dialects: routing it here made
        # every unsubscribe raise `UnexpectedSubscriptionParameters` (fixed in cd973363).
        # A declared-channel candidate always reaches the matcher too, regardless of
        # `will_try_subscribe` (a JSON-RPC `subscribe`-shaped `msg_type` may be entirely
        # absent from a JSON-RPC dialect's frames, which have no `type` key at all).
        if declared_candidates or will_try_subscribe:
          try:
            match = registry.match_subscribe(message)
          except UnexpectedSubscriptionParameters as exc:
            # Prefer `rpc_unexpected`'s diagnostic when we have one -- it comes from
            # `match_rpc` actually recognizing the method, which is more informative than
            # `match_subscribe`'s own (possibly broad-fallback-pool) diagnostic. Reachable
            # whenever `match_rpc` ran (no declared candidates), recognized the method, but
            # rejected it on params, and `match_subscribe` then also fails to find a match.
            preferred = rpc_unexpected if rpc_unexpected is not None else exc
            await connection.send(
              json.dumps(
                _ws_error_message(
                  connection_id=connection_id,
                  message_id=message_id,
                  message='unexpected_parameters',
                  details={
                    'request': preferred.message,
                    'candidates': _ws_error_candidates(preferred.candidates),
                  },
                  request=message,
                )
              )
            )
            continue
          except AmbiguousSubscriptionMatch as exc:
            await connection.send(
              json.dumps(
                _ws_error_message(
                  connection_id=connection_id,
                  message_id=message_id,
                  message='ambiguous_parameters',
                  details={
                    'request': exc.message,
                    'candidates': _ws_error_candidates(exc.candidates),
                  },
                  request=message,
                )
              )
            )
            continue

          if match is not None:
            example = match.example
            subscription_key = _ws_example_key(example)
            active[subscription_key] = example
            if example.reply is not None:
              # `serve_response` threads the caller's own request `id` into the replayed
              # ack when `envelope.correlate` declares it -- a JSON-RPC ack is matched by
              # `id` like any other rpc reply, and a socket multiplexes real, incrementing
              # ids across every call, so a recorded ack's own hardcoded id would only ever
              # coincidentally match a caller's without this (see `RpcClient` callers'
              # symmetric `serve_response` use for `match_rpc` just above).
              reply = serve_response(example.reply, message, example.envelope)
              await connection.send(json.dumps(reply))
            elif (ack := _synthesized_ack(example, message)) is not None:
              await connection.send(json.dumps(ack))
            await _push_example_messages(connection, example)
            continue

          # Neither registry found a real match. Prefer `match_rpc`'s own diagnostic when it
          # had one -- a recognized RPC method whose params matched no candidate is more
          # informative than a bare "channel unknown", and is exactly the generic
          # `public/subscribe` case once the one channel its own example captured isn't the
          # one being requested.
          if rpc_unexpected is not None:
            await connection.send(
              json.dumps(
                _ws_error_message(
                  connection_id=connection_id,
                  message_id=message_id,
                  message='unexpected_parameters',
                  details={
                    'request': rpc_unexpected.message,
                    'candidates': _ws_error_candidates(rpc_unexpected.candidates),
                  },
                  request=message,
                )
              )
            )
            continue

          await connection.send(
            json.dumps(
              _ws_error_message(
                connection_id=connection_id,
                message_id=message_id,
                message='unknown_subscription',
                details={'request': message},
                request=message,
              )
            )
          )
          continue

        await connection.send(
          json.dumps(
            _ws_error_message(
              connection_id=connection_id,
              message_id=message_id,
              message='unsupported_message_type',
              details={'request': message},
              request=message,
            )
          )
        )

    async def start():
      server = await websockets.serve(handler, host, port)
      actual_port = server.sockets[0].getsockname()[1]
      started.put((loop, server, actual_port, threading.current_thread()))

    try:
      loop.run_until_complete(start())
      loop.run_forever()
    except Exception as exc:
      started.put(exc)
    finally:
      loop.close()

  thread = threading.Thread(target=run, daemon=True)
  thread.start()
  result = started.get(timeout=5)
  if isinstance(result, Exception):
    raise result
  loop, server, actual_port, _ = result
  return RunningWsServer(
    client=root_of(root).name,
    host=host,
    port=actual_port,
    path=path,
    loop=loop,
    server=server,
    thread=thread,
  )


@contextlib.contextmanager
def running_server(
  root: Path | Project,
  *,
  host: str = '127.0.0.1',
  port: int = 0,
):
  """Context manager that starts and stops the HTTP mock server.

  Args:
    root: Project, or the project root directory holding its spec.
    host: Bind address.
    port: Bind port, or 0 to pick a free one.
  """
  server = build_server(root, host=host, port=port)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    yield server
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@contextlib.contextmanager
def running_ws_server(root: Path | Project, *, host: str = '127.0.0.1', port: int = 0):
  """Context manager that starts and stops the websocket mock server.

  Args:
    root: Project, or the project root directory holding its spec.
    host: Bind address.
    port: Bind port, or 0 to pick a free one.
  """
  server = start_ws_server(root, host=host, port=port)
  try:
    yield server
  finally:
    server.close()


@contextlib.contextmanager
def running_mock_servers(
  root: Path | Project,
  *,
  host: str = '127.0.0.1',
  http_port: int = 0,
  ws_port: int = 0,
):
  """Start HTTP mocks and, when available, websocket mocks for the same project.

  Projects without websocket examples still get a valid HTTP-only context; this
  keeps callers from needing transport-specific setup branches.

  Args:
    root: Project, or the project root directory holding its spec.
    host: Bind address.
    http_port: HTTP bind port, or 0 to pick a free one.
    ws_port: WebSocket bind port, or 0 to pick a free one.
  """
  with running_server(root, host=host, port=http_port) as http_server:
    ws_server = None
    try:
      ws_server = start_ws_server(root, host=host, port=ws_port)
    except ValueError:
      ws_server = None

    try:
      yield RunningMockServers(http_server=http_server, ws_server=ws_server)
    finally:
      if ws_server is not None:
        ws_server.close()
