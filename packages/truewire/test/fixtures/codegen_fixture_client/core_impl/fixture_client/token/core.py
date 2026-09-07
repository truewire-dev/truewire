"""Chain-scoped subcore (`codegen/config.toml`'s `[python.cores] chain`), resolved for every
endpoint under `spec/endpoints/token/` (that directory's own `router.json` declares
`core: "chain"`, overriding the root's `"root"` for anything beneath it -- design §5).

`ChainRpc` is design §5a's own worked example, ported into the fixture: a subtree
reachable over any of several networks, with no canonical URL to bind at construction --
`network` is threaded into every call's path, and `Generator._core_new_method` (design
§5a) generates a real method (not a zero-arg `@cached_property`) wherever this core is
composed one level under something that doesn't already carry `network` (the client
root), degrading back to a plain `@cached_property` one level further down, where it
does (`Token` itself, since it subclasses `ChainRpc` directly and so already carries
`network` transitively -- `balances`/`nfts` are the two-level-nested proof).

Also the fixture's own regression case for design §2/§6's `meta` mechanism: `codegen.
toml` declares no `[cores.chain]` `meta` schema at all -- the common case, most cores
need none -- so both endpoints under this core declare `meta: {}` and every generated
call omits `meta=` entirely. `request` declares no `meta` parameter of its own at all as
a result -- it never receives one.

`ChainRpc` does *not* subclass `fixture_client.core.RpcEndpoint` -- it delegates its own
request mechanics to that module's `perform_request` (a plain function) directly, the
same reasoning `futures/core.py`'s `FuturesEndpoint` documents in full: a core with no
`meta` parameter at all can't cleanly override a base `request()` that declares one
either (`reportIncompatibleMethodOverride` again, the opposite direction of
`FuturesEndpoint`'s own case -- a parameter genuinely missing in the override, not just
narrowed).
"""

from typing_extensions import Any, Literal, Self, TypeVar
from types import UnionType
from dataclasses import dataclass

from fixture_client.core import perform_request
from fixture_client.transport import InMemoryTransport

T = TypeVar('T')

Network = Literal['ethereum', 'polygon']
"""EVM network `ChainRpc` is scoped to -- folded into every call's own wire path."""


@dataclass(kw_only=True, frozen=True)
class ChainRpc:
  """RPC core scoped to one EVM network -- threads `network` into every call's path."""

  client: InMemoryTransport
  network: Network

  @classmethod
  def new(cls, client: Any, *, network: Network) -> Self:
    """Build a chain-scoped core sharing `client`'s already-built transport.

    Args:
      client: Already-built shared transport, forwarded from whichever composing class
        constructs this core.
      network: EVM network every call through this core is scoped to.
    """
    return cls(client=client, network=network)

  async def request(
    self,
    request: Any = None,
    *,
    method: str,
    path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
  ) -> T:
    """Same as `fixture_client.core.RpcEndpoint.request`, with `self.network` folded
    into the wire path -- `chain` declares no `meta` schema (design §2/§6), so this
    core's own calls are never signed.
    """
    return await perform_request(
      self.client, request, method=method, path=f'/{self.network}{path}',
      validate=validate, request_type=request_type, response_type=response_type,
    )
