"""The core skeletons `truewire init --template <name>` writes, one per common API shape.

Each template is the hand-written half of a project: `src/<pkg>/core/` plus the
`[cores.*]` and `[python.cores.*]` tables of `truewire.toml` that wire it to generated
code. Every template satisfies the protocols in `truewire_core.contract`, passes
`truewire check` and `truewire generate python` on a fresh project, and is pyright-clean
on its own. `docs/cores.md` describes what each one does and what to change.

Templates are `string.Template` text: `$package`, `$base_url`, `$ws_url` and `$class_name`
are the only placeholders, so a template can carry every brace a Python dict literal needs.
"""
from dataclasses import dataclass, field
from string import Template


@dataclass(frozen=True)
class CoreTemplate:
  """One `--template` choice."""
  name: str
  summary: str
  """One line for `--help` and the docs."""
  core: str
  """`src/<pkg>/core/__init__.py`."""
  cores_toml: str
  """The `[cores.<name>]` tables: the meta schema each symbolic core validates against."""
  python_cores_toml: str
  """The `[python.cores.<name>]` tables: which hand-written class each core resolves to."""
  files: dict[str, str] = field(default_factory=dict)
  """Further project-relative files (`src/$package/core/ws.py`, a `router.json`)."""

  def render(self, text: str, *, package: str, base_url: str, ws_url: str, class_name: str) -> str:
    """Fill the placeholders of one template text."""
    return Template(text).substitute(package=package, base_url=base_url, ws_url=ws_url, class_name=class_name)


DEFAULT_CORES_TOML = '''[cores.default]
meta = { type = "object", properties = { public = { type = "boolean" } }, additionalProperties = false }
'''

DEFAULT_PYTHON_CORES_TOML = '''[python.cores.root]
base = "$package.core:ClientBase"

[python.cores.default]
base = "$package.core:Endpoint"
'''


BEARER_CORE = '''"""Hand-written core for the $package client: transport, auth, envelope and errors.

Every generated endpoint class subclasses `Endpoint` and calls `self.request(...)`; this is
the one place that knows how to reach the upstream API. Adapt `Transport` (base URL,
headers, signing, envelope unwrapping, error mapping) to your API; the generated code
never changes when you do.
"""
from dataclasses import dataclass, field
from types import UnionType
from typing_extensions import Any, Self, TypeVar, cast

from truewire_core.exceptions import ApiError
from truewire_core.http import HttpClient
from truewire_core.validation import validator

from ..meta import DefaultMeta as Meta

T = TypeVar('T')


@dataclass(kw_only=True)
class Transport:
  """The shared HTTP transport: base URL plus whatever auth the API needs."""
  base_url: str
  http: HttpClient = field(default_factory=HttpClient)
  api_key: str | None = None
  validate: bool = True

  def headers(self, *, public: bool) -> dict[str, str]:
    """Headers for one call. Add signing here."""
    if public or self.api_key is None:
      return {}
    return {'Authorization': f'Bearer {self.api_key}'}

  async def send(self, method: str, path: str, *, params: dict[str, Any], body: bytes | None, public: bool) -> bytes:
    """Send one request; raise `ApiError` on a non-2xx status."""
    filled = path
    for name, value in list(params.items()):
      if f'{{{name}}}' in filled:
        filled = filled.replace(f'{{{name}}}', str(value))
        params.pop(name)
    response = await self.http.request(
      method, self.base_url.rstrip('/') + '/' + filled.lstrip('/'),
      params=params or None, content=body, headers=self.headers(public=public),
    )
    if response.status_code >= 400:
      raise ApiError(f'{method} {filled}: HTTP {response.status_code}: {response.text[:200]}')
    return response.content


@dataclass(kw_only=True)
class ClientBase:
  """Root client: owns the transport every endpoint shares."""
  client: Transport

  @classmethod
  def new(cls, *, base_url: str = '$base_url', api_key: str | None = None, validate: bool = True) -> Self:
    """Create a client against `base_url`."""
    return cls(client=Transport(base_url=base_url, api_key=api_key, validate=validate))

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    await self.client.http.__aexit__(exc_type, exc_value, traceback)


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  """Base for every generated endpoint class: one shared transport."""
  client: Transport

  async def request(
    self, request: Any = None, *, method: str, path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Meta = {},
  ) -> T:
    """Send one request and validate the reply against `response_type`."""
    params = {k: v for k, v in dict(request or {}).items() if v is not None}
    body = None
    if method.upper() in ('POST', 'PUT', 'PATCH') and request_type is not None and request is not None:
      body = validator(cast(type, request_type)).dump(request)
      params = {}
    raw = await self.client.send(method, path, params=params, body=body, public=bool(meta.get('public')))
    if response_type is None:
      return None  # type: ignore[return-value]
    check = self.client.validate if validate is None else validate
    if check:
      return validator(cast(type, response_type)).json(raw)
    import json
    return json.loads(raw)
'''


HMAC_CORE = '''"""Hand-written core for the $package client: API key plus HMAC-SHA256 request signing.

Every generated endpoint class subclasses `Endpoint` and calls `self.request(...)`; this is
the one place that knows how to reach the upstream API. `Transport` signs every call that
is not `public`: the signature covers the timestamp, the HTTP method, the path (query
string included) and the raw body, in that order, and travels in three headers. Adapt
`signature_message`, `sign` and `Transport.headers` to the API's own recipe; the generated
code never changes when you do.

What the transport injects, and where the spec declares it:

- `X-API-Key`, `X-Timestamp` and `X-Signature` are headers. A recorded example holds the
  request parameters and the response body, never headers, so nothing is declared for them.
- An API that wants the timestamp, a nonce or the signature as a query or body field gets it
  added in `Transport.send`, and every endpoint that carries it lists the field name under
  `redacted` in its `endpoint.json`. A recorded example then never pins a value that changes
  on every call, and `truewire mock` ignores the field when matching a request.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import time
from types import UnionType
from typing_extensions import Any, Callable, NoReturn, Self, TypeVar, cast
from urllib.parse import urlencode

from truewire_core.exceptions import ApiError, AuthError, BadRequest, RateLimited
from truewire_core.http import HttpClient
from truewire_core.validation import validator

from ..meta import DefaultMeta as Meta

T = TypeVar('T')

API_KEY_HEADER = 'X-API-Key'
TIMESTAMP_HEADER = 'X-Timestamp'
SIGNATURE_HEADER = 'X-Signature'


def now_millis() -> str:
  """The default timestamp: milliseconds since the epoch, as a string."""
  return str(int(time.time() * 1000))


def signature_message(timestamp: str, method: str, path: str, body: bytes | None) -> bytes:
  """The bytes a signature covers: `timestamp + METHOD + path + body`.

  `path` carries the query string when there is one, so the signature covers exactly what
  goes on the wire. Change the order, the separators or the method casing here when the
  API's recipe differs; keep it a pure function so it stays testable on its own.
  """
  return f'{timestamp}{method.upper()}{path}'.encode() + (body or b'')


def sign(secret: str, message: bytes) -> str:
  """Hex HMAC-SHA256 of `message` under `secret`. Swap the digest or the encoding here."""
  return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def raise_for_status(status: int, text: str, *, context: str) -> NoReturn:
  """Map a non-2xx status onto `truewire_core.exceptions`.

  Raises:
    AuthError: 401 or 403.
    RateLimited: 429.
    BadRequest: Any other 4xx.
    ApiError: Anything else.
  """
  message = f'{context}: HTTP {status}: {text[:200]}'
  if status in (401, 403):
    raise AuthError(message)
  if status == 429:
    raise RateLimited(message)
  if 400 <= status < 500:
    raise BadRequest(message)
  raise ApiError(message)


@dataclass(kw_only=True)
class Transport:
  """The shared HTTP transport: base URL, credentials and the signing clock."""
  base_url: str
  http: HttpClient = field(default_factory=HttpClient)
  api_key: str | None = None
  api_secret: str | None = None
  validate: bool = True
  timestamp: Callable[[], str] = field(default=now_millis)
  """Clock for the signed timestamp; a test replaces it to sign a known value."""

  def headers(self, method: str, path: str, body: bytes | None, *, public: bool) -> dict[str, str]:
    """Headers for one call: nothing for a public call, the key, timestamp and signature otherwise.

    Raises:
      AuthError: The call is not public and the client was built without credentials.
    """
    if public:
      return {}
    if self.api_key is None or self.api_secret is None:
      raise AuthError(f'{method} {path} is not public: pass api_key and api_secret to new()')
    timestamp = self.timestamp()
    return {
      API_KEY_HEADER: self.api_key,
      TIMESTAMP_HEADER: timestamp,
      SIGNATURE_HEADER: sign(self.api_secret, signature_message(timestamp, method, path, body)),
    }

  async def send(self, method: str, path: str, *, params: dict[str, Any], body: bytes | None, public: bool) -> bytes:
    """Send one request; raise on a non-2xx status.

    The query string is built here rather than by the HTTP client so that the signed path
    and the sent path are the same bytes.
    """
    filled = path
    for name, value in list(params.items()):
      if f'{{{name}}}' in filled:
        filled = filled.replace(f'{{{name}}}', str(value))
        params.pop(name)
    target = '/' + filled.lstrip('/')
    if params:
      target += '?' + urlencode(params, doseq=True)
    headers = self.headers(method, target, body, public=public)
    if body is not None:
      headers['Content-Type'] = 'application/json'
    response = await self.http.request(method, self.base_url.rstrip('/') + target, content=body, headers=headers)
    if response.status_code >= 400:
      raise_for_status(response.status_code, response.text, context=f'{method} {target}')
    return response.content


@dataclass(kw_only=True)
class ClientBase:
  """Root client: owns the transport every endpoint shares."""
  client: Transport

  @classmethod
  def new(
    cls, *, base_url: str = '$base_url', api_key: str | None = None, api_secret: str | None = None,
    validate: bool = True,
  ) -> Self:
    """Create a client against `base_url`. Public endpoints need no credentials."""
    return cls(client=Transport(base_url=base_url, api_key=api_key, api_secret=api_secret, validate=validate))

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    await self.client.http.__aexit__(exc_type, exc_value, traceback)


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  """Base for every generated endpoint class: one shared transport."""
  client: Transport

  async def request(
    self, request: Any = None, *, method: str, path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Meta = {},
  ) -> T:
    """Send one request and validate the reply against `response_type`."""
    params = {k: v for k, v in dict(request or {}).items() if v is not None}
    body = None
    if method.upper() in ('POST', 'PUT', 'PATCH') and request_type is not None and request is not None:
      body = validator(cast(type, request_type)).dump(request)
      params = {}
    raw = await self.client.send(method, path, params=params, body=body, public=bool(meta.get('public')))
    if response_type is None:
      return None  # type: ignore[return-value]
    check = self.client.validate if validate is None else validate
    if check:
      return validator(cast(type, response_type)).json(raw)
    return json.loads(raw)
'''


JSONRPC_CORE = '''"""Hand-written core for the $package client: JSON-RPC 2.0 over HTTP.

Every call is one `POST` to `base_url` carrying `{"jsonrpc": "2.0", "id": ..., "method":
..., "params": ...}`. In the spec, an endpoint's `path` is the JSON-RPC method name
(`getBalance`, no leading slash) and its HTTP `method` is `POST`; `Endpoint.request` takes
the HTTP method because the `HttpEndpoint` contract does, and never reads it.

The reply is unwrapped here: `result` comes back, `error` becomes an exception. Endpoints
declare `envelope: {"payload": "result"}` so the response schema describes the whole frame
while the generated method returns `result` (authoring rule 6). `id` is a counter the
transport injects; it is not a request parameter, so nothing is declared for it.

What to change for a specific API: `Transport.headers` (auth), `build_request` (positional
`params`, a batch), the error-code tables below, and `unwrap` when the API's error shape is
not JSON-RPC's own `{code, message, data}`.
"""
from dataclasses import dataclass, field
import json
from types import UnionType
from typing_extensions import Any, NoReturn, Self, TypeVar, cast

from truewire_core.exceptions import ApiError, AuthError, BadRequest, RateLimited
from truewire_core.http import HttpClient
from truewire_core.validation import validator

from ..meta import DefaultMeta as Meta

T = TypeVar('T')

JSONRPC_VERSION = '2.0'

INVALID_REQUEST_CODES = frozenset({-32700, -32600, -32601, -32602})
"""JSON-RPC's own codes for a request the server could not accept: parse error, invalid
request, method not found, invalid params. All `BadRequest`."""
AUTH_CODES: frozenset[int] = frozenset({-32001})
"""Codes the API uses for a missing or rejected credential; put its own here."""
RATE_LIMIT_CODES: frozenset[int] = frozenset({-32005, 429})
"""Codes the API uses when it throttles; put its own here."""


def build_request(id: int, method: str, params: Any) -> dict[str, Any]:
  """One JSON-RPC 2.0 request frame.

  `params` is the request's named parameters as a dict, or `None` for a call without any.
  An API whose methods take positional parameters wants a list here, built in the order
  the endpoint's `request` schema declares its properties.
  """
  frame: dict[str, Any] = {'jsonrpc': JSONRPC_VERSION, 'id': id, 'method': method}
  if params is not None:
    frame['params'] = params
  return frame


def raise_error(method: str, error: Any) -> NoReturn:
  """Raise the `truewire_core` exception matching a JSON-RPC `error` member.

  Raises:
    AuthError: `code` is in `AUTH_CODES`.
    RateLimited: `code` is in `RATE_LIMIT_CODES`.
    BadRequest: `code` is in `INVALID_REQUEST_CODES`.
    ApiError: Anything else, including an `error` that is not an object.
  """
  code = error.get('code') if isinstance(error, dict) else None
  message = error.get('message', '') if isinstance(error, dict) else str(error)
  detail = f'{method}: {message} (code {code})'
  if isinstance(error, dict) and error.get('data') is not None:
    detail += f': {json.dumps(error["data"])[:200]}'
  if code in AUTH_CODES:
    raise AuthError(detail)
  if code in RATE_LIMIT_CODES:
    raise RateLimited(detail)
  if code in INVALID_REQUEST_CODES:
    raise BadRequest(detail)
  raise ApiError(detail)


def unwrap(frame: Any, *, id: int, method: str) -> Any:
  """Return `result` from one JSON-RPC reply frame.

  Raises:
    ApiError: The frame is not an object, answers a different `id`, or carries neither
      `result` nor `error`.
    AuthError, RateLimited, BadRequest: Per `raise_error`, when the frame carries `error`.
  """
  if not isinstance(frame, dict):
    raise ApiError(f'{method}: not a JSON-RPC reply: {json.dumps(frame)[:200]}')
  if frame.get('error') is not None:
    raise_error(method, frame['error'])
  if frame.get('id') != id:
    raise ApiError(f'{method}: reply id {frame.get("id")!r} does not match request id {id!r}')
  if 'result' not in frame:
    raise ApiError(f'{method}: reply carries neither result nor error')
  return frame['result']


def raise_for_status(status: int, text: str, *, context: str) -> NoReturn:
  """Map a non-2xx HTTP status onto `truewire_core.exceptions`; most JSON-RPC servers answer
  200 even on failure, so this mostly covers proxies and auth gateways.

  Raises:
    AuthError: 401 or 403.
    RateLimited: 429.
    BadRequest: Any other 4xx.
    ApiError: Anything else.
  """
  message = f'{context}: HTTP {status}: {text[:200]}'
  if status in (401, 403):
    raise AuthError(message)
  if status == 429:
    raise RateLimited(message)
  if 400 <= status < 500:
    raise BadRequest(message)
  raise ApiError(message)


@dataclass(kw_only=True)
class Transport:
  """The shared HTTP transport: one URL, a request-id counter, optional bearer auth."""
  base_url: str
  http: HttpClient = field(default_factory=HttpClient)
  api_key: str | None = None
  validate: bool = True
  last_id: int = field(default=0, init=False)

  def headers(self, *, public: bool) -> dict[str, str]:
    """Headers for one call. Change the auth scheme here."""
    headers = {'Content-Type': 'application/json'}
    if not public and self.api_key is not None:
      headers['Authorization'] = f'Bearer {self.api_key}'
    return headers

  async def call(self, method: str, params: Any, *, public: bool) -> Any:
    """Send one JSON-RPC request and return its unwrapped `result`."""
    self.last_id += 1
    id = self.last_id
    body = json.dumps(build_request(id, method, params)).encode()
    response = await self.http.request('POST', self.base_url, content=body, headers=self.headers(public=public))
    if response.status_code >= 400:
      raise_for_status(response.status_code, response.text, context=method)
    try:
      frame = response.json()
    except ValueError as exc:
      raise ApiError(f'{method}: reply is not JSON: {response.text[:200]}') from exc
    return unwrap(frame, id=id, method=method)


@dataclass(kw_only=True)
class ClientBase:
  """Root client: owns the transport every endpoint shares."""
  client: Transport

  @classmethod
  def new(cls, *, base_url: str = '$base_url', api_key: str | None = None, validate: bool = True) -> Self:
    """Create a client against `base_url`, the one URL every method is posted to."""
    return cls(client=Transport(base_url=base_url, api_key=api_key, validate=validate))

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    await self.client.http.__aexit__(exc_type, exc_value, traceback)


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  """Base for every generated endpoint class: one shared transport."""
  client: Transport

  async def request(
    self, request: Any = None, *, method: str, path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Meta = {},
  ) -> T:
    """Call the JSON-RPC method `path` and validate its `result` against `response_type`.

    `method` is the HTTP verb the generated call passes (always `POST` here) and is not read.
    """
    params: Any = None
    if request is not None and request_type is not None:
      params = json.loads(validator(cast(type, request_type)).dump(request))
    elif request is not None:
      params = {k: v for k, v in dict(request).items() if v is not None}
    result = await self.client.call(path, params, public=bool(meta.get('public')))
    if response_type is None:
      return None  # type: ignore[return-value]
    check = self.client.validate if validate is None else validate
    if check:
      return validator(cast(type, response_type)).python(result)
    return cast(T, result)
'''


WS_CORE = '''"""Hand-written core for the $package client: an HTTP transport and a WebSocket streams
client side by side.

`truewire.toml` routes the two: `[python.cores.root]` `children = { streams = "socket" }`
hands the `streams` composite the root's `socket` field, every other child gets `client`
(the HTTP transport). Generated `rpc` endpoints subclass `Endpoint` and call
`self.request(...)`; generated `stream` endpoints (the `spec/endpoints/streams/` group,
whose `router.json` names the `streams` core) subclass `StreamEndpoint` and call
`self.subscribe(...)`. The socket itself is `.ws.Connection`, a `truewire_core.ws.Streams`
subclass that speaks the subscribe dialect `truewire mock` serves by default; adapt it to
the API's frames.

Adapt `Transport` (base URL, headers, signing, envelope, errors) and `ws.Connection`
(frames, acks, channel routing); the generated code never changes when you do.
"""
from dataclasses import dataclass, field
import json
from types import UnionType
from typing_extensions import Any, Self, TypeVar, cast

from truewire_core.exceptions import ApiError
from truewire_core.http import HttpClient
from truewire_core.util import StreamManager
from truewire_core.validation import validator

from ..meta import DefaultMeta as Meta
from .ws import SocketClient

T = TypeVar('T')


@dataclass(kw_only=True)
class Transport:
  """The shared HTTP transport: base URL plus whatever auth the API needs."""
  base_url: str
  http: HttpClient = field(default_factory=HttpClient)
  api_key: str | None = None
  validate: bool = True

  def headers(self, *, public: bool) -> dict[str, str]:
    """Headers for one call. Add signing here."""
    if public or self.api_key is None:
      return {}
    return {'Authorization': f'Bearer {self.api_key}'}

  async def send(self, method: str, path: str, *, params: dict[str, Any], body: bytes | None, public: bool) -> bytes:
    """Send one request; raise `ApiError` on a non-2xx status."""
    filled = path
    for name, value in list(params.items()):
      if f'{{{name}}}' in filled:
        filled = filled.replace(f'{{{name}}}', str(value))
        params.pop(name)
    response = await self.http.request(
      method, self.base_url.rstrip('/') + '/' + filled.lstrip('/'),
      params=params or None, content=body, headers=self.headers(public=public),
    )
    if response.status_code >= 400:
      raise ApiError(f'{method} {filled}: HTTP {response.status_code}: {response.text[:200]}')
    return response.content


@dataclass(kw_only=True)
class ClientBase:
  """Root client: owns the HTTP transport and the socket every endpoint shares."""
  client: Transport
  socket: SocketClient

  @classmethod
  def new(
    cls, *, base_url: str = '$base_url', ws_url: str = '$ws_url',
    api_key: str | None = None, validate: bool = True,
  ) -> Self:
    """Create a client against `base_url` (HTTP) and `ws_url` (the socket, opened on first use)."""
    return cls(
      client=Transport(base_url=base_url, api_key=api_key, validate=validate),
      socket=SocketClient.new(ws_url, validate=validate),
    )

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    await self.socket.__aexit__(exc_type, exc_value, traceback)
    await self.client.http.__aexit__(exc_type, exc_value, traceback)


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  """Base for every generated HTTP endpoint class: one shared transport."""
  client: Transport

  async def request(
    self, request: Any = None, *, method: str, path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Meta = {},
  ) -> T:
    """Send one request and validate the reply against `response_type`."""
    params = {k: v for k, v in dict(request or {}).items() if v is not None}
    body = None
    if method.upper() in ('POST', 'PUT', 'PATCH') and request_type is not None and request is not None:
      body = validator(cast(type, request_type)).dump(request)
      params = {}
    raw = await self.client.send(method, path, params=params, body=body, public=bool(meta.get('public')))
    if response_type is None:
      return None  # type: ignore[return-value]
    check = self.client.validate if validate is None else validate
    if check:
      return validator(cast(type, response_type)).json(raw)
    return json.loads(raw)


@dataclass(kw_only=True, frozen=True)
class StreamEndpoint:
  """Base for every generated `stream` endpoint class: one shared socket."""
  client: SocketClient

  def subscribe(
    self, channel: str, parameters: Any = None, *,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
  ) -> StreamManager[T, Any, Any]:
    """Subscribe to `channel`; each pushed frame validates against `response_type`.

    `{name}` placeholders in `channel` are filled from `parameters`; every other parameter
    goes into the subscribe frame beside `channel`.
    """
    params: dict[str, Any] = {}
    if parameters is not None and request_type is not None:
      params = json.loads(validator(cast(type, request_type)).dump(parameters))
    elif parameters is not None:
      params = {k: v for k, v in dict(parameters).items() if v is not None}
    filled = channel
    for name, value in list(params.items()):
      if f'{{{name}}}' in filled:
        filled = filled.replace(f'{{{name}}}', str(value))
        params.pop(name)
    payload_validator = validator(cast(type, response_type)) if response_type is not None else None
    return self.client.subscribe(filled, params, payload_validator=payload_validator, validate=validate)
'''


WS_MODULE = '''"""The WebSocket side of the $package core: one `truewire_core.ws.Streams` connection.

`Connection` speaks the subscribe dialect `truewire mock` serves when an endpoint records
only `parameters`: the client sends `{"type": "subscribe", "channel": ..., ...params}` and
`{"type": "unsubscribe", ...}`, the server acknowledges with a frame whose `type` is in
`ACK_TYPES`, and every pushed frame carries `channel` (plus `id`, when the subscription was
keyed by one). Acks carry no correlation id, so `SerialReplies` pairs each request with the
next ack by arrival order.

What to change for a specific API: the two frames in `request_subscription` and
`request_unsubscription`, `ACK_TYPES` and the error check, and `subscription_key`, which
decides which local subscription a pushed frame belongs to. An API that correlates acks by
a request id is a `truewire_core.ws.StreamsRpc` instead; `examples/kraken/src/kraken/core`
in the Truewire repository is a complete reference for that shape.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import json
from typing_extensions import Any, Mapping, TypeVar, cast
import websockets

from truewire_core.exceptions import ApiError
from truewire_core.util import StreamManager
from truewire_core.validation import validator
from truewire_core.ws import SerialReplies, Streams
from truewire_core.ws.streams import Subscription

T = TypeVar('T')

Frame = dict[str, Any]
"""One decoded JSON frame, in either direction."""

ACK_TYPES = frozenset({'subscribed', 'unsubscribed', 'ack', 'error'})
"""`type` values of a frame that answers a subscribe or unsubscribe request."""


def subscription_key(channel: str, params: Mapping[str, Any] | None = None) -> str:
  """The local name of one subscription: the channel, plus `:<id>` when it was keyed by one.

  A pushed frame maps to the same key through `Connection.parse_msg`, so this is the one
  place that decides how frames route to subscriptions.
  """
  id = (params or {}).get('id')
  return f'{channel}:{id}' if id is not None else channel


@dataclass
class Connection(SerialReplies[Frame], Streams[Frame, Mapping[str, Any], Frame, Frame]):
  """The socket: subscribe/unsubscribe frames out, acks and channel pushes in."""

  async def send(self, msg: object):
    ws = await self.ws
    await ws.send(json.dumps(msg))

  async def ping(self, ws: websockets.ClientConnection):
    """A protocol-level ping every `ping_interval`; an API with its own heartbeat frame sends it here."""
    await ws.ping()

  async def request_subscription(self, channel: str, params: Mapping[str, Any] | None = None) -> Frame:
    reply = await self.request({'type': 'subscribe', 'channel': channel, **(params or {})})
    if reply.get('type') == 'error':
      raise ApiError(f'subscribe {channel}: {reply}')
    return reply

  async def request_unsubscription(self, channel: str, params: Mapping[str, Any] | None = None) -> Frame:
    return await self.request({'type': 'unsubscribe', 'channel': channel, **(params or {})})

  def parse_msg(self, msg: str | bytes) -> Subscription[Frame] | None:
    frame = json.loads(msg)
    if not isinstance(frame, dict):
      return None
    if frame.get('type') in ACK_TYPES:
      self.replies.put_nowait(frame)
      return None
    if 'channel' in frame:
      return {'channel': subscription_key(frame['channel'], frame), 'notification': frame}
    return None


@dataclass(kw_only=True)
class SocketClient:
  """Owns one `Connection` and the client-level validation default."""
  conn: Connection
  validate: bool = True

  @classmethod
  def new(
    cls, url: str, *, validate: bool = True,
    timeout: timedelta = timedelta(seconds=10), ping_interval: timedelta = timedelta(seconds=30),
  ) -> 'SocketClient':
    """Build a client for `url`; the socket opens on the first subscription."""
    return cls(conn=Connection(url, timeout=timeout, ping_interval=ping_interval), validate=validate)

  async def __aenter__(self):
    await self.conn.__aenter__()
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    await self.conn.__aexit__(exc_type, exc_value, traceback)

  def subscribe(
    self, channel: str, params: Mapping[str, Any] | None = None, *,
    payload_validator: validator[T] | None = None, validate: bool | None = None,
  ) -> StreamManager[T, Any, Any]:
    """Subscribe to `channel`, validating each pushed frame with `payload_validator` unless disabled."""
    manager = self.conn.subscribe(subscription_key(channel, params), params, request_channel=channel)
    check = self.validate if validate is None else validate
    if payload_validator is None or not check:
      return cast(StreamManager[T, Any, Any], manager)
    return manager.map(payload_validator.python)
'''

WS_CORES_TOML = DEFAULT_CORES_TOML + '''
[cores.streams]
# Stream endpoints read nothing per call; the socket is one connection for the whole group.
'''

WS_PYTHON_CORES_TOML = '''[python.cores.root]
base = "$package.core:ClientBase"
# The `streams` child is built on the root's `socket` field; every other child gets `client`.
children = { streams = "socket" }

[python.cores.default]
base = "$package.core:Endpoint"

[python.cores.streams]
base = "$package.core:StreamEndpoint"
'''

WS_STREAMS_ROUTER = '''{
  "description": "$class_name WebSocket channels.",
  "upstream": "$base_url",
  "core": "streams"
}
'''


TEMPLATES: dict[str, CoreTemplate] = {
  'bearer': CoreTemplate(
    name='bearer',
    summary='HTTP with an optional bearer token; no envelope, no signing. The default.',
    core=BEARER_CORE,
    cores_toml=DEFAULT_CORES_TOML,
    python_cores_toml=DEFAULT_PYTHON_CORES_TOML,
  ),
  'hmac': CoreTemplate(
    name='hmac',
    summary='HTTP with an API key and an HMAC-SHA256 signature over timestamp, method, path and body, in headers.',
    core=HMAC_CORE,
    cores_toml=DEFAULT_CORES_TOML,
    python_cores_toml=DEFAULT_PYTHON_CORES_TOML,
  ),
  'jsonrpc': CoreTemplate(
    name='jsonrpc',
    summary='JSON-RPC 2.0 over HTTP POST: one URL, `{jsonrpc, id, method, params}` out, `result` unwrapped, `error` mapped.',
    core=JSONRPC_CORE,
    cores_toml=DEFAULT_CORES_TOML,
    python_cores_toml=DEFAULT_PYTHON_CORES_TOML,
  ),
  'ws': CoreTemplate(
    name='ws',
    summary='The bearer HTTP transport plus a `truewire_core.ws` streams client for a `streams/` endpoint group.',
    core=WS_CORE,
    cores_toml=WS_CORES_TOML,
    python_cores_toml=WS_PYTHON_CORES_TOML,
    files={
      'src/$package/core/ws.py': WS_MODULE,
      'spec/endpoints/streams/router.json': WS_STREAMS_ROUTER,
    },
  ),
}

DEFAULT_TEMPLATE = 'bearer'


def template_help() -> str:
  """The `--template` help text: every name with its one-line summary."""
  lines = [f'`{name}`: {template.summary}' for name, template in TEMPLATES.items()]
  return 'Core skeleton to write. ' + ' '.join(lines)
