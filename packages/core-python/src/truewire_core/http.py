from typing_extensions import Any, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import asyncio
import math
import os
import time
import httpx

from truewire_core.exceptions import NetworkError


@dataclass(frozen=True)
class Exchange:
  """One request and the response it got, exactly as they crossed the wire."""
  request: httpx.Request
  response: httpx.Response


_recording: ContextVar[list[Exchange] | None] = ContextVar('truewire_http_recording', default=None)


@contextmanager
def recording() -> Iterator[list[Exchange]]:
  """Record every exchange any `HttpClient` makes inside the block, in order.

  What a `truewire capture` needs and nothing more: the wire-level request and response,
  before the client core unwraps an envelope or maps an error, so a recorded example
  describes what the API actually sent. Scoped to the current task via a context variable,
  so concurrent callers outside the block record nothing.

  Everything the core sent is in here, in order, not just the call's own request: a token
  mint, a refresh, a retry after a 401. Pick the exchange you mean out of the list by its
  request -- method and path, or the operation name in the frame -- never by position.
  Reading `exchanges[-1]` is what once recorded an OAuth token's 200 body as an endpoint's
  example (`truewire capture`, fixed).

  Examples:
    ```python
    with recording() as exchanges:
      pet = await client.pets.get_pet(pet_id=42)
    mine = [x for x in exchanges if x.request.url.path.endswith('/pets/42')]
    status, body = mine[-1].response.status_code, mine[-1].response.json()
    ```
  """
  exchanges: list[Exchange] = []
  token = _recording.set(exchanges)
  try:
    yield exchanges
  finally:
    _recording.reset(token)

RETRY_ATTEMPTS = 3
"""Attempts a request gets with `retry=True`, the first included."""

RETRY_STATUSES = frozenset({429, 503})
"""Statuses retried with `retry=True`: the server said it did not handle the request."""

RETRY_AFTER_CAP = 30.0
"""Longest `Retry-After`, in seconds, a retry waits; a longer one returns the reply as is."""

RETRY_BACKOFF = 0.5
"""Seconds before the first retry when the server names no `Retry-After`; doubled after."""


def retry_after(response: httpx.Response) -> float | None:
  """The reply's `Retry-After` in seconds from now, or `None` when absent or unreadable.

  Either form HTTP allows: delay-seconds (`120`) or an HTTP date, which counts from now and
  is never negative.
  """
  value = response.headers.get('retry-after')
  if value is None:
    return None
  value = value.strip()
  if value.isascii() and value.isdigit():
    return float(value)
  try:
    when = parsedate_to_datetime(value)
  except (TypeError, ValueError, IndexError):
    return None
  if when.tzinfo is None:
    when = when.replace(tzinfo=timezone.utc)
  return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _default_limits(proxy: str | None = None) -> httpx.Limits:
  """No kept-alive connections behind a proxy, explicit or from the environment."""
  if proxy or os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY'):
    return httpx.Limits(max_keepalive_connections=0)
  return httpx.Limits()

@dataclass
class HttpClient:
  """Managed HTTP client, wrapping `httpx.AsyncClient`.

  ### Proxy
  `proxy` is an HTTP(S) proxy URL (`http://host:port`, credentials in the userinfo) every
  request goes through. Left `None` or `''`, httpx reads the environment
  (`HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`/`NO_PROXY`), and with none set, on macOS and
  Windows, the system proxy settings but not their bypass list (`Socket.proxy` honours
  it); an explicit one ignores both, `NO_PROXY` included. Packages clause P18: the environment is a fallback, since a sandbox or a
  library caller cannot always set it. A `socks5://` URL needs `httpx[socks]`.

  ### Rate and retry
  `rate` is the requests per second the client paces itself to (`[policy].rate`, workspace
  clause W15): request starts are spaced `1 / rate` seconds apart, shared by every caller
  of this client, so ten requests at `rate=5` span 1.8 s. `None` sends each at once.

  `retry=True` (`[policy].retry`) sends a request again, up to `RETRY_ATTEMPTS` in all,
  when it did not reach the server (a failure to connect, before anything was sent) or the
  reply is 429 or 503. A retry waits the reply's `Retry-After` (seconds or an HTTP date),
  else `RETRY_BACKOFF` doubling; a `Retry-After` over `RETRY_AFTER_CAP` returns the reply
  instead. Nothing else is retried: not another status, and not a connection lost after
  the request was sent, which the server may have acted on. Each attempt is paced and
  recorded like any request. `retry=False`, the default, sends each request once.

  ### Concurrency Contract
  1. Connection: single owner via `async with`, also supports lazy no-owner use
  2. Requests: many concurrent callers OK
  """
  proxy: str | None = field(default=None, kw_only=True)
  rate: float | None = field(default=None, kw_only=True)
  """Requests per second to pace to; `None` for no pacing."""
  retry: bool = field(default=False, kw_only=True)
  """Whether to retry a request that did not reach the server or got a 429 or 503."""
  limits: httpx.Limits | None = None
  """Connection limits; `None` picks `_default_limits`, which keeps nothing alive behind a proxy."""
  lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
  _client: httpx.AsyncClient | None = None
  _pace_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
  _next_start: float = field(default=0.0, init=False, repr=False)

  def __post_init__(self):
    if self.rate is not None and (isinstance(self.rate, bool) or not math.isfinite(self.rate) or self.rate <= 0):
      raise ValueError(f'rate must be a positive number of requests per second; got {self.rate!r}')

  async def _pace(self):
    """Wait for this request's start slot: `1 / rate` after the previous one's."""
    if self.rate is None:
      return
    # Commit a slot only when it is used. Cancellation releases the FIFO lock,
    # whether the caller is queued for it or sleeping at the head of the queue.
    async with self._pace_lock:
      while (wait := self._next_start - time.monotonic()) > 0:
        await asyncio.sleep(wait)
      self._next_start = time.monotonic() + 1 / self.rate

  @property
  async def client(self) -> httpx.AsyncClient:
    async with self.lock:
      if self._client is None:
        proxy = self.proxy or None
        limits = self.limits if self.limits is not None else _default_limits(proxy)
        self._client = await httpx.AsyncClient(limits=limits, proxy=proxy).__aenter__()
      return self._client

  async def __aenter__(self):
    """Take ownership without connecting; the underlying client opens lazily on first use."""
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    async with self.lock:
      if self._client is not None:
        await self._client.__aexit__(exc_type, exc_value, traceback)
        self._client = None

  async def request(
    self, method: str, url: str,
    *,
    content: httpx._types.RequestContent | None = None,
    data: httpx._types.RequestData | None = None,
    files: httpx._types.RequestFiles | None = None,
    json: Any | None = None,
    params: Mapping[str, Any] | None = None,
    headers: Mapping | None = None,
    cookies: httpx._types.CookieTypes | None = None,
    auth: httpx._types.AuthTypes | httpx._client.UseClientDefault | None = httpx.USE_CLIENT_DEFAULT,
    follow_redirects: bool | httpx._client.UseClientDefault = httpx.USE_CLIENT_DEFAULT,
    timeout: httpx._types.TimeoutTypes | httpx._client.UseClientDefault = httpx.USE_CLIENT_DEFAULT,
    extensions: httpx._types.RequestExtensions | None = None,
  ):
    # A body that is an iterator or a file cannot be sent twice.
    retry = self.retry and files is None and (content is None or isinstance(content, bytes | str))
    for attempt in range(1, RETRY_ATTEMPTS + 1):
      last = not retry or attempt == RETRY_ATTEMPTS
      backoff = RETRY_BACKOFF * 2 ** (attempt - 1)
      # Opened before the pace, which then spaces the sends themselves.
      client = await self.client
      await self._pace()
      try:
        response = await client.request(
          method, url, params=params, cookies=cookies, json=json,
          content=content, data=data, files=files, auth=auth, follow_redirects=follow_redirects,
          timeout=timeout, extensions=extensions,
          headers=headers,
        )
      except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # With redirects enabled, the origin may already have acted before a later
        # hop failed to connect. HTTPX does not expose that history on the exception.
        redirects = client.follow_redirects if isinstance(follow_redirects, httpx._client.UseClientDefault) else follow_redirects
        if last or redirects:
          raise NetworkError(f'Error sending request to {method} {url}', *e.args) from e
        await asyncio.sleep(backoff)
        continue
      except httpx.HTTPError as e:
        req = f'{method} {url}'
        raise NetworkError(f'Error sending request to {req}', *e.args) from e
      exchanges = _recording.get()
      if exchanges is not None:
        exchanges.append(Exchange(request=response.request, response=response))
      if last or response.history or response.status_code not in RETRY_STATUSES:
        return response
      delay = retry_after(response)
      if delay is not None and delay > RETRY_AFTER_CAP:
        return response
      await asyncio.sleep(backoff if delay is None else delay)
    raise AssertionError('unreachable: the last attempt returns or raises')
