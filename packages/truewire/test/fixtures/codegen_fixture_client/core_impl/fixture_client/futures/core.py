"""Futures subcore (`codegen/config.toml`'s `[python.cores] futures`), resolved for every
endpoint under `spec/endpoints/futures/` (that directory's own `router.json` declares
`core: "futures"`, overriding the root's `"default"` for anything beneath it -- design
§5, exercised by `Generator._resolve_core`'s Task 14 walk).

Real fleet precedent for this split: mexc hand-builds separate `spot/core/` and
`futures/core/` directories with distinct mixin chains (`SpotMixin`/`AuthSpotMixin` vs
`FuturesMixin`/`AuthFuturesMixin`, neither subclassing the other) -- confirmed in the
design doc's own §5. mexc's own `AuthSpotMixin`/`AuthFuturesMixin` don't independently
hit the identical `reportIncompatibleMethodOverride` trap this fixture manufactures
(neither overrides `request()` with an incompatible `meta` signature -- they add
`authed_request` as a new method instead), so treat this as confirming the weaker, still
load-bearing half of the pattern -- independent per-domain core classes, not one shared
base forced to serve incompatible overridden signatures -- not as mexc having hit this
exact Liskov error before. This
fixture's `FuturesEndpoint` mirrors that with the smallest real difference worth having:
it refuses to make a call whose spec-declared `meta` doesn't include `signed=True`,
where the default core has no opinion on that at all. `positions` declares `signed:
true`; `leverage` declares `signed: false` explicitly (`codegen/config.toml`'s top-level
`[cores.futures]` requires `signed`, so every endpoint under this core states it one way
or the other) -- so this genuinely does fire against real generated code, unlike before
this schema existed: `leverage` is only ever exercised at generation/type-check time
(`test_codegen_generator_e2e.py`'s runtime smoke test never calls it), never at runtime,
so the refusal is dormant, not dead. It's a distinct auth *policy* per subcore, not just
a distinct import path, guarding against a future futures endpoint mistakenly spec'd
`signed: false`. What a caller genuinely controls per call is whether the *client*
itself was built with credentials (`ClientBase.new(public=True)`, say) -- that's
enforced one level down, in the shared transport's own `send`.

`request` accepts `meta` as this module's own hand-written `Meta` now (design §2/§6,
corrected: never code-generated, matching the JSON Schema `codegen/config.toml`'s top-level
`[cores.futures]` declares directly -- `signed` required, `public` optional). Every
generated call under this core emits a plain dict literal (`meta={'signed': True}`),
checked structurally against this class's own `meta: Meta` parameter, no import needed.

`FuturesEndpoint` does *not* subclass `fixture_client.core.RpcEndpoint` -- it delegates
its own request mechanics to that module's `perform_request` (a plain function), instead
of inheriting `request()` and overriding it. `Meta` here genuinely differs from
`RpcEndpoint`'s own (`signed` required here, optional there), and two cores can't both
override one inherited `request()` method with incompatible `meta` parameter types
without breaking Liskov substitutability -- see `fixture_client.core`'s own module
docstring for the full reasoning.
"""

from typing_extensions import Any, NotRequired, TypedDict, TypeVar
from types import UnionType
from dataclasses import dataclass

from fixture_client.core import perform_request
from fixture_client.transport import InMemoryTransport

T = TypeVar('T')


class Meta(TypedDict):
  """`meta`'s shape for this core (`codegen/config.toml`'s `[cores.futures]`) -- hand-written to
  match the declared JSON Schema exactly, never code-generated (design §2/§6). Distinct
  from `fixture_client.core.Meta` (`default`'s own, all-optional) -- each resolved core
  hand-authors its own `Meta`, matching only its own declared schema."""

  signed: bool
  """Whether this call must be signed -- required: every futures endpoint states it."""
  public: NotRequired[bool]
  """Whether this call is public (no credentials required)."""


@dataclass(kw_only=True, frozen=True)
class FuturesEndpoint:
  """Futures subcore: every call must declare `signed=True` -- there is no public futures
  surface, so a mis-spec'd public futures endpoint is refused rather than silently served.
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
    meta: Meta,
  ) -> T:
    """Same as `fixture_client.core.RpcEndpoint.request`, refusing a call whose declared
    `meta` isn't `signed`.

    Raises:
      PermissionError: `meta` did not declare `signed=True`.
    """
    if not meta.get('signed'):
      raise PermissionError(
        f'{method} {path}: futures endpoints have no public surface'
      )
    return await perform_request(
      self.client, request, method=method, path=path, validate=validate,
      request_type=request_type, response_type=response_type, signed=meta['signed'],
    )
