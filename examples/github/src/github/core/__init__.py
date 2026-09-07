"""Hand-written core for the github client: transport, auth, envelope and errors.

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
    """Headers for one call: GitHub's media type, API version and a User-Agent, plus the
    token when one was given (public endpoints accept it too, and it raises the rate limit)."""
    headers = {
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'truewire-example-github',
    }
    if self.api_key is not None:
      headers['Authorization'] = f'Bearer {self.api_key}'
    return headers

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
  def new(cls, *, base_url: str = 'https://api.github.com', api_key: str | None = None, validate: bool = True) -> Self:
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
