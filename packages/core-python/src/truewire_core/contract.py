"""The contract between a hand-written core and the code `truewire generate` emits.

A generated endpoint class subclasses the base its `[python.cores.<name>]` entry names and
calls exactly one of three verbs on `self`; a generated composite constructs its children
from the fields declared in `truewire.toml`. The generator reads nothing else from a core:
it never imports the target package. These `Protocol`s spell out what each base has to
provide, so a core can be checked against them (`isinstance` at runtime, structurally by a
type checker) without the generator ever seeing it.

The `meta` keyword is present on a verb only when the core's `[cores.<name>]` entry
declares a `meta` schema; its type is the `TypedDict` the generator writes to
`<package>/meta.py` from that schema. A core with no schema takes no `meta` at all. The
protocols below type it as any mapping for that reason.

Verbs:

- `request(request, *, method, path, ...)`: one HTTP call (`HttpEndpoint`).
- `request(request, *, path, ...)`: one WebSocket command/reply call, `path` being the
  wire method name (`CommandEndpoint`).
- `subscribe(channel, parameters, *, ...)`: one channel subscription, returning a
  `StreamManager` the caller awaits or iterates (`StreamEndpoint`).

Bases:

- `ClientRoot`: what the root class the generated `main.py` subclasses provides: a
  `new(...)` classmethod that builds every transport, and the async context manager that
  opens and closes them.
- `Composite`: a base a composite router subclasses when its children need more than the
  parent's single `client` field: `new(client, *, ...)` receives the forwarded field first
  and every `forward`/`params` keyword declared for that core after it.
"""
from types import UnionType
from typing_extensions import Any, Mapping, Protocol, Self, TypeVar, runtime_checkable

from .util.streams import StreamManager

T = TypeVar('T')


@runtime_checkable
class HttpEndpoint(Protocol):
  """Base for a generated `rpc` endpoint reached over HTTP."""

  async def request(
    self,
    request: Any = None,
    *,
    method: str,
    path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Mapping[str, Any] = {},
  ) -> T:
    """Send one request and return the value the method's return type describes.

    Args:
      request: The generated `Request` value (a `TypedDict`, a union member or `None`).
      method: The wire HTTP method.
      path: The wire path template; `{name}` placeholders are filled from `request`.
      validate: Per-call override of response validation; the client default when `None`.
      request_type: The generated request type, for `validator(...).dump(request)`.
      response_type: The generated response type, for `validator(...).json(raw)`.
      meta: The endpoint's declared `meta`, when this core declares a schema for it.
    """
    ...


@runtime_checkable
class CommandEndpoint(Protocol):
  """Base for a generated `rpc` endpoint reached over a WebSocket connection."""

  async def request(
    self,
    request: Any = None,
    *,
    path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Mapping[str, Any] = {},
  ) -> T:
    """Send one command and return its reply; `path` is the wire method name."""
    ...


@runtime_checkable
class StreamEndpoint(Protocol):
  """Base for a generated `stream` endpoint."""

  def subscribe(
    self,
    channel: str,
    parameters: Any = None,
    *,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Mapping[str, Any] = {},
  ) -> StreamManager[T, Any, Any]:
    """Subscribe to `channel` with `parameters`; each pushed payload validates as `T`."""
    ...


@runtime_checkable
class ClientRoot(Protocol):
  """Base of the generated root class: builds and owns every transport."""

  @classmethod
  def new(cls, **kwargs: Any) -> Self:
    """Build a client from credentials and options; every argument is keyword-only."""
    ...

  async def __aenter__(self) -> Self: ...

  async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any: ...


@runtime_checkable
class Composite(Protocol):
  """Base of a composite router whose `[python.cores.<name>]` entry declares `forward`
  or `params`: the parent constructs it through `new`, not the dataclass constructor."""

  @classmethod
  def new(cls, client: Any, /, **kwargs: Any) -> Self:
    """Build from the parent's forwarded field (`client`) plus every declared keyword."""
    ...


__all__ = ['HttpEndpoint', 'CommandEndpoint', 'StreamEndpoint', 'ClientRoot', 'Composite']
