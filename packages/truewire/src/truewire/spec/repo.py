import json
from dataclasses import dataclass
from pathlib import Path
from typing_extensions import Any

from truewire.project import Project, spec_dir as project_spec_dir

from .endpoint import Endpoint
from .example import (
  ExampleRequest,
  ExampleResponse,
  WsBinaryFramesExample,
  WsMessagesExample,
  WsParametersExample,
)


@dataclass(frozen=True)
class ExamplePair:
  """Pairing result for one logical example id inside an endpoint `examples/` folder.

  This is intentionally tolerant: one side may be missing so callers can report
  partial coverage instead of silently dropping orphan files.
  """
  example_id: str
  request_path: Path | None
  response_path: Path | None


@dataclass(frozen=True)
class EndpointRecord:
  """Endpoint spec loaded with its canonical file path."""
  path: Path
  endpoint: Endpoint


@dataclass(frozen=True)
class HttpExample:
  """Loaded HTTP example pair for one endpoint."""
  endpoint_path: Path
  endpoint: Endpoint
  example_id: str
  request: ExampleRequest
  response: ExampleResponse


@dataclass(frozen=True)
class WsExample:
  """Loaded WebSocket example bundle for one endpoint."""
  endpoint_path: Path
  endpoint: Endpoint
  example_id: str
  parameters: WsParametersExample
  reply: Any | None
  messages: list[Any]
  message_frames: list[bytes]
  unsubscribe_reply: Any | None = None
  """The upstream API's own captured unsubscribe acknowledgement (`<id>.unsubscribe_reply.json`),
  when recorded. Optional and purely additive: a bare, verbatim frame loaded the same
  trivial way as `reply`, and its presence or absence never affects whether this example
  counts as complete -- see `ws_examples`."""

  @property
  def has_messages(self) -> bool:
    """Return whether this example has decoded or binary messages."""
    return bool(self.messages or self.message_frames)


@dataclass(frozen=True)
class ExampleCoverage:
  """Coverage summary for one endpoint example directory."""
  endpoint_path: Path
  endpoint: Endpoint
  request_files: list[Path]
  response_files: list[Path]
  parameter_files: list[Path]
  reply_files: list[Path]
  message_files: list[Path]
  protobuf_message_files: list[Path]
  has_examples: bool
  is_partial: bool

  @property
  def kind(self) -> str:
    """Return the endpoint transport kind."""
    return self.endpoint.spec.kind


class NoEndpointSpecs(Exception):
  """Raised when a path names no `<spec>/endpoints` tree, so there is nothing to check."""


@dataclass(frozen=True)
class SpecScope:
  """Where a spec command looks, and which project root that place belongs to.

  The two are not the same directory once a command is scoped to one subdivision, and
  keeping them apart is what stops a gate reporting success over nothing: `schemas.json`
  and the `relative_to` base always come from `root`, while the specs actually scanned
  come from `scope`.
  """
  root: Path
  """Project root -- the directory holding `truewire.toml` (or, for a bare spec tree, the
  directory holding `spec/`)."""
  scope: Path
  """Directory actually scanned: the whole `<spec>/endpoints` tree, or one subdivision of it."""
  endpoints_root: Path
  """`<spec>/endpoints`, the base every reported path is relative to."""

  @property
  def is_subdivision(self) -> bool:
    """Return whether this scope is narrower than the project's whole endpoint tree."""
    return self.scope != self.endpoints_root


def resolve_scope(path: Path | Project) -> SpecScope:
  """Resolve a project root *or* one subdivision of its `<spec>/endpoints` tree.

  Spec work is gated per subdivision, so a scope argument has to accept a subdivision
  directory and mean it. It used to accept one and silently scan
  `<subdivision>/spec/endpoints`, which does not exist -- coverage was computed over an
  empty set and reported as success, while a path that did *not* exist failed cleanly.
  Both shapes now resolve, and anything with no `endpoints` tree above or below it raises.

  Args:
    path: A `Project`, a project root, or any directory at or under its `<spec>/endpoints`.

  Raises:
    NoEndpointSpecs: When `path` is not a directory, or names no `endpoints` tree.
  """
  if isinstance(path, Project):
    endpoints = path.endpoints_dir
    if not endpoints.is_dir():
      raise NoEndpointSpecs(
        f'{path.root}: no `{endpoints.relative_to(path.root)}` tree, so nothing would be checked'
      )
    return SpecScope(root=path.root, scope=endpoints, endpoints_root=endpoints)
  target = path.expanduser().resolve()
  if not target.is_dir():
    raise NoEndpointSpecs(f'{target} is not a directory')
  endpoints = project_spec_dir(target) / 'endpoints'
  if endpoints.is_dir():
    return SpecScope(root=target, scope=endpoints, endpoints_root=endpoints)
  for ancestor in (target, *target.parents):
    if ancestor.name == 'endpoints':
      root = ancestor.parent.parent
      if project_spec_dir(root) / 'endpoints' == ancestor:
        return SpecScope(root=root, scope=target, endpoints_root=ancestor)
  raise NoEndpointSpecs(
    f'{target}: no `endpoints` spec tree above or below this path, so nothing would be '
    f'checked. Pass a project root, or a subdivision under its `spec/endpoints`.'
  )


def endpoint_specs(root: Path | Project, *, scope: Path | None = None) -> list[Path]:
  """Find every endpoint spec under the canonical recursive `<spec>/endpoints/**` layout.

  Args:
    root: Project (or project root).
    scope: Subdivision under `<spec>/endpoints` to restrict the search to. Defaults
      to the whole tree.
  """
  endpoints = scope if scope is not None else project_spec_dir(root) / 'endpoints'
  return sorted(endpoints.rglob('endpoint.json'))


def load_endpoint(path: Path) -> Endpoint:
  """Load and validate one `endpoint.json` file."""
  return Endpoint.model_validate(load_json(path))


def endpoint_records(root: Path | Project, *, scope: Path | None = None) -> list[EndpointRecord]:
  """Load every endpoint record under a project root, or under one subdivision of it.

  Args:
    root: Project (or project root).
    scope: Subdivision under `<spec>/endpoints` to restrict the load to.
  """
  return [
    EndpointRecord(path=path, endpoint=load_endpoint(path))
    for path in endpoint_specs(root, scope=scope)
  ]


def load_json(path: Path) -> Any:
  return json.loads(path.read_text())


def load_shared_schemas(root: Path | Project) -> dict[str, Any]:
  """Load every `schemas.json` belonging to a project -- its own root `spec/schemas.json`,
  if present, plus every nested `spec/endpoints/**/schemas.json` scope --
  merged into one flat id -> schema mapping.

  Flattening every scope together (rather than keeping each one's visibility separate the
  way `Generator._resolve_schemas` does for codegen) is correct here: this function only
  feeds a jsonschema `$ref` resolver for example validation, not per-endpoint visibility,
  and the "no shadowing" rule (`check_schemas_no_shadowing`) already means the
  same id can't be declared by two scopes on one ancestor path in a valid spec -- so a flat
  merge and a visibility-scoped one agree wherever the spec actually is valid. A genuine id
  collision still raises here rather than silently letting one scope's definition win,
  mirroring `_resolve_schemas`'s own defensive raise, in case that separate check is ever
  bypassed or stale.

  Before this, a project whose shared schemas lived only in nested scopes -- no root
  `spec/schemas.json` at all -- silently resolved to `{}` here, and every `$ref` into one
  of those scopes then failed downstream as an unresolved jsonschema pointer
  (`PointerToNowhere`) rather than as a clear "schema not found" from this function.

  Callers expect a plain mapping from schema id to schema object. A project with no
  `schemas.json` anywhere normalizes to `{}` so it needs no separate code path.
  """
  schemas: dict[str, Any] = {}
  spec = project_spec_dir(root)
  root_schemas_path = spec / 'schemas.json'
  paths = [root_schemas_path] if root_schemas_path.is_file() else []
  endpoints_root = spec / 'endpoints'
  if endpoints_root.is_dir():
    paths.extend(sorted(endpoints_root.rglob('schemas.json')))
  for path in paths:
    payload = load_json(path)
    if not isinstance(payload, dict):
      continue
    for schema_id, schema in payload.items():
      existing = schemas.get(schema_id)
      if existing is not None and existing != schema:
        raise ValueError(
          f'schema id {schema_id!r} is declared by more than one schemas.json scope '
          f'({path} disagrees with an earlier one) -- shadowing is refused rather '
          'than resolving it by nearer-wins precedence'
        )
      schemas[schema_id] = schema
  return schemas


def request_example_payload(path: Path) -> Any:
  """Return the request payload portion used by runtime matching.

  The repo currently has two request example conventions:
  - `{"parameters": ...}` for parameter-shaped requests
  - `{"payload": ...}` for raw body payloads

  This helper intentionally collapses both to the value the runtime usually
  cares about. Code that needs the original envelope should read the file
  directly instead.
  """
  payload = load_json(path)
  if not isinstance(payload, dict):
    return payload
  if 'parameters' in payload:
    return payload['parameters']
  if 'payload' in payload:
    return payload['payload']
  return payload


def response_example_payload(path: Path) -> dict[str, Any]:
  """Return a normalized HTTP response example.

  The mock server and spec validation both rely on the explicit
  `{"status": ..., "payload": ...}` envelope rather than inferring status from
  filenames or directory structure.
  """
  payload = load_json(path)
  if not isinstance(payload, dict):
    raise ValueError(f'{path}: expected response example object')
  if 'status' not in payload or 'payload' not in payload:
    raise ValueError(f'{path}: expected object with `status` and `payload`')
  return payload


def load_request_example(path: Path) -> ExampleRequest:
  """Load and validate one HTTP request example."""
  return ExampleRequest.model_validate(load_json(path))


def load_response_example(path: Path) -> ExampleResponse:
  """Load and validate one HTTP response example."""
  return ExampleResponse.model_validate(load_json(path))


def load_ws_parameters_example(path: Path) -> WsParametersExample:
  """Load and validate one WebSocket parameters example.

  A file already shaped like the model (any of `description`/`parameters`/`payload` as a
  top-level key) is parsed as one. Checking for `parameters` alone -- the previous rule --
  mis-parsed a `payload`-only, argument-less call (a `disable_heartbeat` command:
  `{"description": ..., "payload": {"method": "public/disable_heartbeat", "params": {}}}`,
  no `parameters` key since there's nothing to decompose): the whole object fell through
  to the bare-dict fallback below and got treated *as* `parameters`, silently discarding
  the real `payload` -- `WsMockRegistry.match_rpc` then had nothing to match the real
  request against, and the connection fell through to subscribe-matching instead. Only a
  genuinely bare shorthand (a file that's just `{"symbol": "BTC-USDT"}`, no wrapper at all)
  still takes the fallback.
  """
  payload = load_json(path)
  if isinstance(payload, dict) and ({'description', 'parameters', 'payload'} & payload.keys()):
    return WsParametersExample.model_validate(payload)
  return WsParametersExample(parameters=payload if isinstance(payload, dict) else {})


def load_ws_reply_payload(path: Path) -> Any:
  """Load one WebSocket reply example payload."""
  return load_json(path)


def load_ws_messages(path: Path) -> list[Any]:
  """Load decoded WebSocket message examples."""
  return WsMessagesExample.from_payload(load_json(path)).messages


def load_ws_binary_frames(path: Path) -> list[bytes]:
  """Load base64-encoded binary WebSocket frames from a sidecar example."""
  return WsBinaryFramesExample.from_payload(load_json(path)).decode()


def example_pairs(endpoint_path: Path, *, request_suffix: str, response_suffix: str) -> list[ExamplePair]:
  """Pair request/response example files by stem within one endpoint.

  The pairing logic is suffix-driven so callers can reuse it for HTTP
  request/response pairs or any other two-file convention with matching ids.
  """
  examples_dir = endpoint_path.parent / 'examples'
  if not examples_dir.is_dir():
    return []

  request_files = {
    path.name.removesuffix(request_suffix): path
    for path in sorted(examples_dir.glob(f'*{request_suffix}'))
  }
  response_files = {
    path.name.removesuffix(response_suffix): path
    for path in sorted(examples_dir.glob(f'*{response_suffix}'))
  }
  example_ids = sorted(request_files.keys() | response_files.keys())
  return [
    ExamplePair(
      example_id=example_id,
      request_path=request_files.get(example_id),
      response_path=response_files.get(example_id),
    )
    for example_id in example_ids
  ]


def _example_files(endpoint_path: Path, pattern: str) -> list[Path]:
  examples_dir = endpoint_path.parent / 'examples'
  if not examples_dir.is_dir():
    return []
  return sorted(examples_dir.glob(pattern))


def _file_map(endpoint_path: Path, suffix: str) -> dict[str, Path]:
  return {
    path.name.removesuffix(suffix): path
    for path in _example_files(endpoint_path, f'*{suffix}')
  }


def _message_file_map(endpoint_path: Path) -> dict[str, Path]:
  out = _file_map(endpoint_path, '.messages.json')
  out.update(_file_map(endpoint_path, '.message.json'))
  return out


def _protobuf_message_file_map(endpoint_path: Path) -> dict[str, Path]:
  return _file_map(endpoint_path, '.messages.protobuf.json')


def http_examples(endpoint_path: Path, endpoint: Endpoint) -> list[HttpExample]:
  """Load complete HTTP examples for one endpoint."""
  if 'http' not in endpoint.transports:
    return []
  request_files = _file_map(endpoint_path, '.request.json')
  response_files = _file_map(endpoint_path, '.response.json')
  examples: list[HttpExample] = []
  for example_id in sorted(request_files.keys() & response_files.keys()):
    examples.append(
      HttpExample(
        endpoint_path=endpoint_path,
        endpoint=endpoint,
        example_id=example_id,
        request=load_request_example(request_files[example_id]),
        response=load_response_example(response_files[example_id]),
      )
    )
  return examples


def synthesize_ws_example_from_http(http_example: HttpExample) -> WsExample:
  """Synthesize a WS-RPC example from a captured HTTP one, for a dual-transport
  `kind: 'rpc'` endpoint (`spec.transports` including both `'http'` and `'ws'`) with no
  native `.parameters.json`/`.reply.json` capture for this example id.

  Legitimate to synthesize from, not fabricate: the upstream API's own JSON-RPC method+params+
  result is identical over either transport for these endpoints -- that is exactly why
  the spec declares one endpoint with both transports rather than two separate ones. The
  HTTP request's `parameters` become the synthetic call's `parameters`,
  wrapped in a synthetic outgoing JSON-RPC frame (`method`/`params`, `endpoint.channel`
  naming the wire method the same way a native WS capture's own `payload` would) for the
  mock's WS matcher (`truewire.mock.WsMockRegistry.match_rpc`) to compare against --
  matching ignores `id` on both sides, so its placeholder value here is never compared.
  The HTTP response's whole recorded frame becomes the synthetic `reply` verbatim, the
  same shape a native `.reply.json` capture already carries (e.g. `supporting/hello`'s:
  the whole `{jsonrpc, id, result, ...}` frame, `id` likewise re-threaded by
  `envelope.correlate` before replay, never compared as recorded).

  Args:
    http_example: The endpoint's own captured HTTP example to synthesize a WS-RPC
      counterpart from.
  """
  endpoint = http_example.endpoint
  # `.request` is preferred over the legacy `.parameters` split, the same rule
  # `truewire.mock`'s own new-shape reader already documents -- a project whose examples
  # all record under `request` would otherwise sync an empty dict into the synthesized
  # WS call every time, silently dropping every recorded field.
  parameters = (
    http_example.request.request
    if http_example.request.request is not None
    else http_example.request.parameters
  )
  payload = {
    'jsonrpc': '2.0', 'id': 0, 'method': endpoint.channel, 'params': parameters,
  }
  return WsExample(
    endpoint_path=http_example.endpoint_path,
    endpoint=endpoint,
    example_id=http_example.example_id,
    parameters=WsParametersExample(
      description=http_example.request.description, parameters=parameters, payload=payload,
    ),
    reply=http_example.response.payload,
    messages=[],
    message_frames=[],
  )


def ws_examples(endpoint_path: Path, endpoint: Endpoint) -> list[WsExample]:
  """Load complete WebSocket examples for one endpoint.

  A dual-transport `kind: 'rpc'` endpoint (`spec.transports` including both `'http'` and
  `'ws'`) gets a synthesized WS example (`synthesize_ws_example_from_http`) for any
  example id captured over HTTP but not natively over WS -- never the reverse (a native
  WS capture always wins), and never synthesized at all for an endpoint whose
  `transports` doesn't also include `'http'` (nothing to synthesize from).
  """
  if 'ws' not in endpoint.transports:
    return []
  parameter_files = _file_map(endpoint_path, '.parameters.json')
  reply_files = _file_map(endpoint_path, '.reply.json')
  unsubscribe_reply_files = _file_map(endpoint_path, '.unsubscribe_reply.json')
  message_files = _message_file_map(endpoint_path)
  protobuf_files = _protobuf_message_file_map(endpoint_path)
  examples: list[WsExample] = []
  example_ids = sorted(parameter_files.keys() & (reply_files.keys() | message_files.keys() | protobuf_files.keys()))
  for example_id in example_ids:
    examples.append(
      WsExample(
        endpoint_path=endpoint_path,
        endpoint=endpoint,
        example_id=example_id,
        parameters=load_ws_parameters_example(parameter_files[example_id]),
        reply=load_ws_reply_payload(reply_files[example_id]) if example_id in reply_files else None,
        unsubscribe_reply=load_ws_reply_payload(unsubscribe_reply_files[example_id])
        if example_id in unsubscribe_reply_files
        else None,
        messages=load_ws_messages(message_files[example_id]) if example_id in message_files else [],
        message_frames=load_ws_binary_frames(protobuf_files[example_id]) if example_id in protobuf_files else [],
      )
    )

  if endpoint.spec.kind == 'rpc' and 'http' in endpoint.transports:
    native_ids = {example.example_id for example in examples}
    for http_example in http_examples(endpoint_path, endpoint):
      if http_example.example_id not in native_ids:
        examples.append(synthesize_ws_example_from_http(http_example))
    examples.sort(key=lambda example: example.example_id)

  return examples


def client_http_examples(root: Path | Project) -> list[HttpExample]:
  """Load all complete HTTP examples for one project."""
  examples: list[HttpExample] = []
  for record in endpoint_records(root):
    examples.extend(http_examples(record.path, record.endpoint))
  return examples


def client_ws_examples(root: Path | Project) -> list[WsExample]:
  """Load all complete WebSocket examples for one project."""
  examples: list[WsExample] = []
  for record in endpoint_records(root):
    examples.extend(ws_examples(record.path, record.endpoint))
  return examples


def example_coverage(endpoint_path: Path, endpoint: Endpoint) -> ExampleCoverage:
  """Compute example coverage for one endpoint using canonical pairing rules."""
  request_files = _example_files(endpoint_path, '*.request.json')
  response_files = _example_files(endpoint_path, '*.response.json')
  parameter_files = _example_files(endpoint_path, '*.parameters.json')
  reply_files = _example_files(endpoint_path, '*.reply.json')
  message_files = _example_files(endpoint_path, '*.messages.json') + _example_files(endpoint_path, '*.message.json')
  protobuf_files = _example_files(endpoint_path, '*.messages.protobuf.json')

  # A dual-transport rpc (`transports: ['http', 'ws']`) is representable, but not yet
  # handled here: this still branches on one transport at a time, so an http-and-ws
  # endpoint only ever has its http pair counted and a complete ws pair alongside it is
  # invisible to `has_examples`/`is_partial`. `ws_examples`/`client_ws_examples` (this
  # same module) have a *replay-side* answer for them (`synthesize_ws_example_from_http`,
  # for a `kind: 'rpc'` endpoint with an HTTP capture but no native WS one), which is a
  # different concern from this function's *coverage-counting* one and doesn't need this
  # branch to change.
  if 'http' in endpoint.transports:
    request_ids = {path.name.removesuffix('.request.json') for path in request_files}
    response_ids = {path.name.removesuffix('.response.json') for path in response_files}
    paired = request_ids & response_ids
    has_any_file = bool(request_files or response_files)
  elif 'ws' in endpoint.transports:
    parameter_ids = {path.name.removesuffix('.parameters.json') for path in parameter_files}
    reply_ids = {path.name.removesuffix('.reply.json') for path in reply_files}
    message_ids: set[str] = set()
    for path in message_files:
      if path.name.endswith('.messages.json'):
        message_ids.add(path.name.removesuffix('.messages.json'))
      elif path.name.endswith('.message.json'):
        message_ids.add(path.name.removesuffix('.message.json'))
    protobuf_ids = {path.name.removesuffix('.messages.protobuf.json') for path in protobuf_files}
    if endpoint.spec.kind == 'stream':
      paired = parameter_ids & (message_ids | protobuf_ids)
    else:
      paired = parameter_ids & reply_ids
    has_any_file = bool(parameter_files or reply_files or message_files or protobuf_files)
  else:
    request_ids = {path.name.removesuffix('.request.json') for path in request_files}
    response_ids = {path.name.removesuffix('.response.json') for path in response_files}
    paired = request_ids & response_ids
    has_any_file = bool(request_files or response_files)

  return ExampleCoverage(
    endpoint_path=endpoint_path,
    endpoint=endpoint,
    request_files=request_files,
    response_files=response_files,
    parameter_files=parameter_files,
    reply_files=reply_files,
    message_files=message_files,
    protobuf_message_files=protobuf_files,
    has_examples=bool(paired),
    is_partial=has_any_file and not paired,
  )


def client_example_coverage(root: Path | Project, *, scope: Path | None = None) -> list[ExampleCoverage]:
  """Compute example coverage for every endpoint under one project root.

  Args:
    root: Project (or project root).
    scope: Subdivision under `<spec>/endpoints` to restrict coverage to.
  """
  return [
    example_coverage(record.path, record.endpoint)
    for record in endpoint_records(root, scope=scope)
  ]
