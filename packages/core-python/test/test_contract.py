"""`truewire_core.contract`: the protocols a hand-written core satisfies.

The generator never imports a core; these protocols are the published shape it emits
against. The tests pin that a core written the way `truewire init`'s template writes one
satisfies them at runtime, and that a class missing a verb does not.
"""
from dataclasses import dataclass
from types import UnionType
from typing_extensions import Any, Self, TypeVar

from truewire_core.contract import (
  ClientRoot, CommandEndpoint, Composite, HttpEndpoint, StreamEndpoint,
)
from truewire_core.util import StreamManager

T = TypeVar('T')


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  client: object

  async def request(
    self, request: Any = None, *, method: str, path: str, validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None, meta: dict[str, Any] = {},
  ) -> T:
    raise NotImplementedError

  def subscribe(
    self, channel: str, parameters: Any = None, *, validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None, meta: dict[str, Any] = {},
  ) -> StreamManager[T, Any, Any]:
    raise NotImplementedError


@dataclass(kw_only=True)
class Root:
  client: object

  @classmethod
  def new(cls, *, api_key: str | None = None) -> Self:
    return cls(client=api_key)

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    return None


@dataclass(kw_only=True, frozen=True)
class Streams:
  private_client: object
  market_client: object

  @classmethod
  def new(cls, client: object, *, market_client: object) -> Self:
    return cls(private_client=client, market_client=market_client)


def test_template_shaped_core_satisfies_the_verbs():
  endpoint = Endpoint(client=None)
  assert isinstance(endpoint, HttpEndpoint)
  assert isinstance(endpoint, CommandEndpoint)
  assert isinstance(endpoint, StreamEndpoint)


def test_root_and_composite_bases():
  assert isinstance(Root(client=None), ClientRoot)
  assert isinstance(Streams.new(None, market_client=None), Composite)
  assert not isinstance(Endpoint(client=None), ClientRoot)


def test_a_class_without_the_verb_does_not_satisfy():
  assert not isinstance(Root(client=None), HttpEndpoint)
  assert not isinstance(Root(client=None), StreamEndpoint)
