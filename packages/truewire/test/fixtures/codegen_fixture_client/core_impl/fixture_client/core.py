"""Hand-written core for the fixture client (codegen-mechanization Task 20 smoke test).

Mirrors design §4/§5's real shape: `ClientBase` owns the shared transport and lifecycle
(`.new()`, `__aenter__`/`__aexit__`), and `RpcEndpoint` is the client's default `core`
(`codegen/config.toml`'s `[python.cores] default`) -- resolved for every endpoint whose nearest
ancestor `router.json` declares no `core` of its own, which in this fixture is
everything except `futures/` (see `.futures.core.FuturesEndpoint`).

`RpcEndpoint` implements *both* call verbs (`request`/`subscribe`), not just one --
`market/` mixes HTTP leaves (`orderbook`, `order`, ...) and one WS leaf
(`ticker_stream`) under this same resolved core, so both verbs are genuinely exercised
here, unlike a client whose HTTP/WS split falls along different subtrees (design §5's
own "an HTTP-only subtree's core never needs `.subscribe()`" is the case this fixture
deliberately does *not* take, to exercise the other one).

This is a real, working implementation against a trivial in-memory transport
(`.transport.InMemoryTransport`), not a stub that only satisfies the type checker --
`test_codegen_generator_e2e.py` drives real generated methods through it.

`request`/`subscribe` accept `meta` as `Meta` -- `fixture_client.meta.DefaultMeta`, the
`TypedDict` `truewire generate` renders from `truewire.toml`'s top-level `[cores.default]`
schema (ADR 0011). Every generated call under this core emits a plain dict literal
(`meta={'signed': True}`), never a `Meta(...)` construction and never an import of `Meta`
-- pyright checks the emitted literal against this class structurally, since a
`TypedDict` is checked by shape, not by whether the call site's own module names the
class.

`perform_request` (below) is the shared RPC mechanics every resolved core's own
`request()` delegates to, as a plain function -- deliberately *not* something
`FuturesEndpoint`/`ChainRpc` inherit by subclassing `RpcEndpoint` and overriding
`request()`. Two cores whose own hand-written `Meta` genuinely differ (`default`'s
all-optional vs `futures`'s `signed`-required) can't both override one inherited
`request()` method with incompatible `meta` parameter types without breaking Liskov
substitutability -- pyright correctly flags exactly that
(`reportIncompatibleMethodOverride`) the moment each core's own `Meta` is hand-written to
really differ, confirmed empirically once `futures/core.py`'s `Meta` was given a required
`signed` field. Real fleet precedent for the fix, not just a fixture workaround: mexc
hand-builds `SpotMixin`/`FuturesMixin` as two genuinely independent mixin chains, never
one subclassing the other's request method.
"""

from typing_extensions import Any, Self, TypeVar, cast
from dataclasses import dataclass, field
from types import UnionType
import json

from truewire_core.util import Stream, StreamManager
from truewire_core.validation import validator

from .meta import DefaultMeta as Meta
from .transport import InMemoryTransport, fill_template

T = TypeVar('T')


async def perform_request(
  client: InMemoryTransport,
  request: Any = None,
  *,
  method: str,
  path: str,
  validate: bool | None = None,
  request_type: type[Any] | UnionType | None = None,
  response_type: type[T] | UnionType | None = None,
  signed: bool = False,
) -> T:
  """Shared RPC mechanics every resolved core's own `request()` delegates to: fill
  `path`'s placeholders from `request`, serialize it through `request_type`'s validator
  (ADR 0020/S28), send it over the shared transport, and validate the reply through
  `response_type`'s validator. A plain function, not a method on a shared base class --
  see this module's own docstring for why (`Meta`'s shape genuinely differs per core, so
  inheriting and overriding one `request()` method across cores breaks Liskov
  substitutability once that's true).

  Args:
    client: The shared in-memory transport.
    request: The generated `Request` value (a `TypedDict` instance, a union member, or
      `None` for a parameterless operation).
    method: Wire HTTP method, when this venue transport carries one.
    path: Wire path template, `{name}` placeholders filled from `request`.
    validate: Per-call override of response validation.
    request_type: The generated request type, used to serialize `request`.
    response_type: The generated response type, used to validate the reply.
    signed: Whether this call must be signed -- each core's own `request()` extracts
      this from its own `meta` value, however its own declared schema names it.
  """
  values = dict(request) if isinstance(request, dict) else {}
  filled_path = fill_template(path, values)
  # `truewire_core.validation.validator.__init__` types its own parameter `type[T]`,
  # narrower than the `type[Any] | UnionType` this function itself has to accept -- a
  # discriminated-union `Request` (`order_submit`'s `LimitOrderRequest |
  # MarketOrderRequest`) is a real `UnionType` object at runtime and `pydantic`'s
  # `TypeAdapter` validates it correctly, but pyright can't see that through
  # `validator`'s own narrower signature. `truewire_core` lives in a separate, gitignored
  # local clone (`public/typed`, not this repo) and isn't this task's to change.
  # `response_type` gets the identical widening -- a bare-top-level-`$ref` response onto
  # a shared `Literal[...]`-rendered enum (`market/order_last_side`'s `OrderSide`, added
  # for the alchemy migration's own `rpc_endpoint` fix) is, to pyright, the same
  # `UnionType`-shaped value passed where `type[T]` alone was declared -- `T` simply
  # goes unbound/`Unknown` on that branch rather than erroring, which is an acceptable
  # precision loss for a hand-written `core` value, not a defect in the mechanism.
  body = (
    validator(cast(type, request_type)).dump(request)
    if request_type is not None and request is not None
    else None
  )
  raw = await client.send(method, filled_path, body=body, signed=signed)
  if response_type is None:
    return None  # type: ignore[return-value]
  if validate is False:
    return validator(cast(type, response_type)).python(request_json_fallback(raw))
  return validator(cast(type, response_type)).json(raw)


@dataclass(kw_only=True)
class ClientBase:
  """Root lifecycle: constructs and owns the transport every resolved `core` shares.

  The generated root class (`fixture_client.main:FixtureClient`) subclasses this and
  this alone (design §4) -- never also a resolved `core`, even transitively.
  """

  client: InMemoryTransport = field(default_factory=InMemoryTransport)

  @classmethod
  def new(cls, *, api_key: str | None = None, public: bool = False) -> Self:
    """Build a client wired to a fresh in-memory transport.

    Args:
      api_key: Credential to use for `signed` calls. Required unless `public`.
      public: Allow construction with no credentials, for public-only usage.

    Raises:
      ValueError: No `api_key` was given and `public` was not set.
    """
    if api_key is None and not public:
      raise ValueError(
        'fixture_client requires api_key, or public=True for public-only use'
      )
    return cls(client=InMemoryTransport(api_key=api_key))

  async def __aenter__(self) -> Self:
    """Take ownership of the client; the in-memory transport needs no real connection."""
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    """No real connection to close."""


@dataclass(kw_only=True, frozen=True)
class RpcEndpoint:
  """Default resolved `core`. Every leaf/router class the codegen output composes
  under this core ultimately reaches `self.client` through this field -- the same
  `{Core}(client=self.client)` construction `Generator._router_cached_property` already
  emits for every composite node, root included (design §5c: `router()` renders the
  root the identical way it renders any other position, no separate method).
  """

  client: InMemoryTransport

  async def request(
    self,
    request: Any = None,
    *,
    method: str,
    path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Meta = {},
  ) -> T:
    """Perform one RPC call -- delegates the actual mechanics to `perform_request`
    (module-level, shared across cores; see this module's own docstring for why it's a
    plain function rather than something a subcore inherits by overriding this method).

    Args:
      request: The generated `Request` value (a `TypedDict` instance, a union member,
        or `None` for a parameterless operation).
      method: Wire HTTP method, when this venue transport carries one.
      path: Wire path template, `{name}` placeholders filled from `request`.
      validate: Per-call override of response validation.
      request_type: The generated request type, used to serialize `request`.
      response_type: The generated response type, used to validate the reply.
      meta: A plain dict literal matching this class's own hand-written `Meta` (design
        §2/§6) -- this core's own declared schema only ever carries `public`/`signed`,
        the two keys every fixture endpoint under it declares.
    """
    return await perform_request(
      self.client, request, method=method, path=path, validate=validate,
      request_type=request_type, response_type=response_type,
      signed=bool(meta.get('signed')),
    )

  def subscribe(
    self,
    channel: str,
    parameters: Any = None,
    *,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | None = None,
    meta: Meta = {},
  ) -> StreamManager[T, Any, Any]:
    """Subscribe to one channel, filling its placeholders from `parameters` the same
    way `request` does for `path` (design §8), and validating each pushed message.

    Args:
      channel: Wire channel template, `{name}` placeholders filled from `parameters`.
      parameters: The generated `Parameters` value.
      validate: Per-call override of pushed-message validation.
      request_type: The generated parameters type (unused here -- nothing in this
        fixture's transport needs to serialize a subscribe frame, since the in-memory
        transport keys channels by their already-resolved string).
      response_type: The generated payload type, used to validate each message.
      meta: A plain dict literal matching this class's own hand-written `Meta`, same as
        `request` (design §2/§6).
    """
    values = dict(parameters) if isinstance(parameters, dict) else {}
    resolved_channel = fill_template(channel, values)
    client = self.client

    async def connect() -> Stream[T, Any, Any]:
      async def messages():
        async for raw in client.iter_channel(resolved_channel):
          if response_type is None or validate is False:
            yield request_json_fallback(raw)
          else:
            yield validator(response_type).json(raw)

      async def unsubscribe():
        return None

      return Stream(reply=None, stream=messages(), unsubscribe=unsubscribe)

    return StreamManager(connect=connect)


def request_json_fallback(raw: bytes) -> Any:
  """Decode a raw reply without validating it, for the `validate=False` override."""
  return json.loads(raw)
