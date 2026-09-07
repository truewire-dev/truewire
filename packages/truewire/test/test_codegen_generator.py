"""Tests for the universal `Generator` (codegen mechanization, Phase 4) against the
synthetic fixture client at `test/fixtures/codegen_fixture_client/`.

The fixture is a minimal but complete client tree exercising all three router shapes
design §3 names:

- **aggregate** (`market/`): its six children -- `orderbook/`, `order/`,
  `order_cancel/`, `order_status/`, `order_submit/`, `ticker_stream/` -- each hold
  `endpoint.json` directly and nothing routing-shaped beneath them (bare leaves), so
  `market/` composes them by multiple inheritance. `order_cancel/` (added for Task 15)
  is a public, bare-flat-object POST with two plain-string properties -- distinct from
  `order/`'s signed `decimal-string`-formatted POST, closing design §7's own
  ~990-endpoint gap case with no special-casing
  (`truewire.codegen.python.Generator.rpc_endpoint`) beyond `order/`'s. `order_status/`
  (added in a later fix round) carries one required and one genuinely optional
  (`NotRequired`) `request` property, exercising that `rpc_endpoint` only includes an
  optional field in the wire request when the caller actually supplied one, rather than
  writing `None` into it unconditionally. `order_submit/` (added in a second fix round)
  is a titled `anyOf` request over two titled object variants (`LimitOrderRequest`/
  `MarketOrderRequest`), matching `docs/spec/authoring.md` rule 0's deribit `buy`/`sell`
  worked example -- the shape that originally crashed `rpc_endpoint` with `ValueError:
  Found nested record type` before `_rpc_request_params`'s `anyOf` branch was fixed to
  reuse `rpc_endpoint`'s own already-registered `type_generator(...)` resolution
  instead of independently re-deriving the union's type.
- **composite** (`account/`): `account/` itself holds no `endpoint.json`, and its two
  children -- `deposits/`, `withdrawals/` -- are themselves aggregates (each holds two
  bare leaves of its own, `list/` and `history/`), not bare leaves. A directory whose
  children are *bare leaves* (as `account/deposits/` and `account/withdrawals/` would be
  if they held one `endpoint.json` each directly) is itself an aggregate, structurally
  identical to `market/` -- confirmed against real fleet data
  (`clients/kraken/spec/endpoints/spot/`, whose children `market_data/`, `trading/`,
  `account/`, `earn/`, `funding/` are themselves further groupings, never a bare
  single-leaf directory). The first cut of this fixture got this wrong -- `deposits/`
  and `withdrawals/` each held one bare `endpoint.json`, making `account/` just another
  aggregate with zero real composite coverage -- and was corrected on review. A
  Task 14-20 implementer testing "composite" classification should use `account/` for
  that, not `market/`.
- **refused** (`mixed_dir/`): holds `endpoint.json` directly *and* a further subdirectory
  (`nested/`) with its own `endpoint.json` -- a directory that is both a leaf endpoint and
  a router grouping, refused outright rather than composed (`docs/spec/authoring.md` rule
  16, `docs/production_standards.md` S30, design §4). Task 24a originally composed this
  shape onto one class via a synthetic self-child; review found the self-leaf could only
  ever render as `__call__` (S29-forbidden), so Task 24c retired that composition
  mechanism in favor of the refusal. Confirmed live in bit2me's spec tree (8 real
  directories of this exact shape, e.g. `v1/trading/trade` -- endpoint.json directly in
  `trade/`, plus a `last/` subdirectory with its own) -- each needs restructuring, not
  support, per rule 16's own worked fix.

Also exercises a subcore boundary (`futures/router.json` declaring `core: "futures"`,
distinct from the root's `"default"`) and one WS `stream` endpoint
(`market/ticker_stream`).
"""
import ast
import json
import sys
from pathlib import Path

import pytest

from truewire.project import resolve
from truewire.codegen.layout import discover_schemas_files, load_schema_file, load_schemas, schemas_scope
from truewire.codegen.python import Generator
from truewire.spec import Endpoint, endpoint_records, load_endpoint
from truewire.spec.codegen_toml import load_codegen_toml

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'codegen_fixture_client'


def test_fixture_client_loads():
  """The fixture's whole `spec/endpoints/` tree loads cleanly through the real, current
  spec loader (`truewire.spec.endpoint_records` -- there is no `client_endpoints`
  function; that name in the task brief did not match the public surface).

  24 `endpoint.json` files exist under the fixture: `market/` holds 14 (`orderbook`,
  `order`, `order_cancel`, `order_status`, `order_submit` -- all added or extended for
  Task 15, see the module docstring -- `ticker_stream`, plus `order_history`/
  `order_preview`, added for Task 24b's own `$ref`-into-`spec/schemas.json` case, see
  that task's own test below, plus `order_last_side`, added for the alchemy migration's
  own bare-top-level-`$ref`-response fix -- see
  `test_rpc_endpoint_bare_ref_response_is_a_shared_schema_ref` below -- plus
  `order_batch`/`order_list`/`order_id_list`, added for the alchemy migration's own
  fix-round regression tests: `order_batch` (nested-record request property, see
  `test_rpc_endpoint_nested_record_request_property`), `order_list` (pagination wired
  into `rpc_endpoint`, record row type, see
  `test_rpc_endpoint_wires_pagination_into_paged_method`), and `order_id_list`
  (pagination with a non-record row type, see
  `test_rpc_endpoint_paginated_non_record_rows_stays_paginated_response_shaped`)), plus
  `order_dispatch`, added for the etherscan migration's own required-single-value-enum
  literal-default fix (see `test_rpc_endpoint_required_single_value_enum_gets_literal_default`),
  plus `order_ledger`, added for the moralis migration's own bare-`$ref`-response
  `PaginatedResponse` regression fix -- a `token`/`absent_cursor`-paginated endpoint whose
  *entire* response is a bare `$ref` (`OrderLedgerResponse`) into `spec/schemas.json`,
  whose own `orders` rows are themselves `$ref`'d to another schemas.json entry
  (`OrderLedgerEntry`) -- see `test_rpc_endpoint_paginated_bare_ref_response_stays_paginated_response_shaped`,
  plus `order_page_total`, added for the `page`-strategy `total`+`rows` dispatch fix (see
  `test_rpc_endpoint_page_total_with_rows_stays_paginated_response_shaped`), plus
  `order_nullable_list`, added for the kucoin `get_closed_orders` S24 regression fix -- a
  `token`/`absent_cursor`-paginated endpoint whose response is itself `anyOf`-wrapped
  with a `null` branch, no title on the wrapper (see
  `test_rpc_endpoint_paginated_anyof_wrapped_response_stays_paginated_response_shaped`),
  `account/` holds
  4 (`deposits/list`, `deposits/history`,
  `withdrawals/list`, `withdrawals/history` -- two leaves per child, so `account/`
  itself is a genuine composite of two aggregates, not another aggregate of bare
  leaves), `futures/` holds 2 (`positions`, plus `leverage`, added for Task 24b's own
  nested-scope `spec/endpoints/futures/schemas.json` case), `mixed_dir/` holds 2
  (itself, plus `mixed_dir/nested/`), and `token/` holds 2 (`balances/get`,
  `nfts/list` -- added for Task 24a's own §5a two-level-nested regression case, see
  `spec/endpoints/token/router.json`'s own `core: "chain"` declaration and
  `core_impl/fixture_client/token/core.py`'s `ChainRpc`). Verified by actually running
  `endpoint_records()` against the built fixture, not by hand-counting alone.
  """
  records = endpoint_records(FIXTURE_ROOT)
  assert len(records) == 26  # market/ now holds 16 -- see order_nullable_list below


def test_resolve_core_walks_to_nearest_ancestor():
  """`_resolve_core` takes the nearest ancestor `router.json` declaring `core` --
  `futures/` declares its own (`"futures"`), overriding `market/`'s (`"default"`) for
  anything under it. Retiring `_root_class` (design §5c) means the client root's own
  `router.json` now declares a *different* symbolic core (`"root"`, resolving to
  `ClientBase`) than any leaf-bearing subtree uses -- so `market/`/`account/`/
  `mixed_dir/` each declare their own closer `"default"` rather than falling all the way
  through to the root's (which would silently resolve a leaf to `ClientBase`, wrong).
  `account/deposits/list` still proves the walk goes past an *immediate* ancestor with no
  declaration of its own (`deposits/` has none) to the nearest one that does
  (`account/`), not just to a direct parent."""
  generator = Generator()
  config = load_codegen_toml(FIXTURE_ROOT)
  futures_dir = FIXTURE_ROOT / 'spec' / 'endpoints' / 'futures' / 'positions'
  assert (
    generator._resolve_core(futures_dir, FIXTURE_ROOT / 'spec', config).base
    == 'fixture_client.futures.core:FuturesEndpoint'
  )
  market_dir = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'orderbook'
  assert (
    generator._resolve_core(market_dir, FIXTURE_ROOT / 'spec', config).base
    == 'fixture_client.core:RpcEndpoint'
  )
  deposits_dir = FIXTURE_ROOT / 'spec' / 'endpoints' / 'account' / 'deposits' / 'list'
  assert (
    generator._resolve_core(deposits_dir, FIXTURE_ROOT / 'spec', config).base
    == 'fixture_client.core:RpcEndpoint'
  )


def test_resolve_core_walks_to_the_root_position_itself():
  """The client root position (`endpoints_root` itself) resolves through the identical
  `_resolve_core` walk as any leaf -- its own `router.json` declares `"root"`, resolving
  to `ClientBase`, distinct from what a leaf-bearing subtree resolves (design §5c: "the
  root's own base is not a separate mechanism... it is simply whichever base resolves at
  the root position")."""
  generator = Generator()
  config = load_codegen_toml(FIXTURE_ROOT)
  endpoints_root = FIXTURE_ROOT / 'spec' / 'endpoints'
  resolved = generator._resolve_core(endpoints_root, FIXTURE_ROOT / 'spec', config)
  assert resolved.base == 'fixture_client.core:ClientBase'


def test_resolve_core_raises_with_no_declaration(tmp_path: Path):
  """A spec tree whose root `router.json` declares no `core` at all -- unreachable once
  `check_router_core` (Task 11) gates it at spec-test time, but `_resolve_core` itself
  must not silently default."""
  endpoints_root = tmp_path / 'spec' / 'endpoints'
  leaf_dir = endpoints_root / 'leaf'
  leaf_dir.mkdir(parents=True)
  (endpoints_root / 'router.json').write_text(json.dumps({
    'description': 'Root, no core declared.', 'upstream': 'https://example.com/docs',
  }))
  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'src = "pkg/src"\n'
    'name = "X"\n'
    '\n'
    '[python.cores.default]\n'
    'base = "x.core:Endpoint"\n'
  )
  config = load_codegen_toml(tmp_path)
  generator = Generator()
  with pytest.raises(ValueError, match='core'):
    generator._resolve_core(leaf_dir, tmp_path / 'spec', config)


def _schemas_generator() -> Generator:
  """A `Generator` wired the way the CLI wires `core_package` before calling `schemas()`
  (`generator.core_package = f'{output_root.name}.core'`, `cli/codegen.py`)."""
  generator = Generator()
  generator.core_package = 'fixture_client.core'
  return generator


def test_schemas_renders_root_scope(tmp_path: Path):
  """`Generator.schemas()` (Task 24b, design §5b) renders the client root's own
  `spec/schemas.json` -- `schemas_scope` unset (`None`) means root scope -- into a
  `schemas.py` module at the package root, generalizing the identical rendering shape
  every real client's own hand-rolled `schemas()` already uses (kraken's is the reference
  implementation, see this method's own docstring)."""
  generator = _schemas_generator()
  schemas = load_schemas(FIXTURE_ROOT)
  assert 'OrderSide' in schemas  # the fixture's own root-scope shared schema

  result = generator.schemas(schemas)

  assert len(result['files']) == 1
  file = result['files'][0]
  assert file['path'] == 'schemas.py'
  assert "OrderSide = Literal['buy', 'sell']" in file['content']
  assert 'from typing_extensions import Literal' in file['content']
  assert result['references'] == {
    'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.schemas'},
    'OrderLedgerEntry': {'name': 'OrderLedgerEntry', 'package': 'fixture_client.schemas'},
    'OrderLedgerResponse': {'name': 'OrderLedgerResponse', 'package': 'fixture_client.schemas'},
  }


def test_schemas_renders_nested_scope(tmp_path: Path):
  """A nested scope (`schemas_scope = ('futures',)`, `spec/endpoints/futures/
  schemas.json`) renders into its own module/package, distinct from the root's -- design
  §5b: "a subtree with its own concerns... gets a file of its own without touching the
  root one." `schemas_scope` is the per-call context the CLI sets immediately before each
  `schemas()` call (Task 24b step 6), the identical `router_context`-style stash this
  class already uses for `router()`."""
  generator = _schemas_generator()
  generator.schemas_scope = ('futures',)
  schemas = load_schema_file(FIXTURE_ROOT / 'spec' / 'endpoints' / 'futures' / 'schemas.json')
  assert 'FuturesSide' in schemas

  result = generator.schemas(schemas)

  assert len(result['files']) == 1
  file = result['files'][0]
  assert file['path'] == 'futures/schemas.py'
  assert "FuturesSide = Literal['long', 'short']" in file['content']
  assert result['references'] == {
    'FuturesSide': {'name': 'FuturesSide', 'package': 'fixture_client.futures.schemas'},
  }


def test_schemas_empty_input_renders_nothing():
  """No schemas declared at a scope -- the CLI's own existing "nothing to emit" answer,
  now provided by the base implementation itself rather than needing a caller-side guard
  against `NotImplementedError`."""
  generator = _schemas_generator()
  assert generator.schemas({}) == {'files': [], 'references': {}}


def test_schemas_raises_without_core_package():
  """`core_package` (set by the CLI, same point as `client_root`/`codegen_config`) names
  the package a rendered scope's module lives under -- `schemas()` cannot invent one."""
  generator = Generator()
  schemas = load_schemas(FIXTURE_ROOT)
  with pytest.raises(ValueError, match='core_package'):
    generator.schemas(schemas)


def test_discover_schemas_files_finds_root_and_nested():
  """`discover_schemas_files` (Task 24b step 4) finds both the fixture's root
  `spec/schemas.json` and its nested `spec/endpoints/futures/schemas.json`."""
  files = discover_schemas_files(FIXTURE_ROOT)
  assert files == [
    FIXTURE_ROOT / 'spec' / 'schemas.json',
    FIXTURE_ROOT / 'spec' / 'endpoints' / 'futures' / 'schemas.json',
  ]


def test_schemas_scope_helper_reports_root_and_nested_scope():
  """`schemas_scope` (`truewire.codegen.layout`) reports the empty tuple for the client
  root's own `spec/schemas.json` and the real directory segments for a nested scope."""
  assert schemas_scope(FIXTURE_ROOT / 'spec' / 'schemas.json', FIXTURE_ROOT) == ()
  assert schemas_scope(
    FIXTURE_ROOT / 'spec' / 'endpoints' / 'futures' / 'schemas.json', FIXTURE_ROOT
  ) == ('futures',)


def _rendered_references(generator: Generator, path: Path, scope: tuple[str, ...] | None):
  """Render one discovered `schemas.json` file the way the CLI does (Task 24b step 6):
  set `schemas_scope`, call `schemas()`, return only its `references` map."""
  generator.schemas_scope = scope
  return generator.schemas(load_schema_file(path))['references']


def test_resolve_schemas_merges_ancestor_scopes_and_root():
  """`_resolve_schemas` (Task 24b, mirroring `_resolve_core`'s nearest-ancestor walk,
  Task 14) merges every scope visible from a given endpoint directory -- unlike
  `_resolve_core`, it does not stop at the first hit: a leaf under `futures/` sees both
  its own nested scope's ids *and* the client root's (design §5b: "a leaf... automatically
  inherits visibility into every ancestor scope"), while a leaf elsewhere in the tree
  (`market/orderbook`) sees only the root scope, never `futures/`'s -- confirming
  design §5b's own "staying invisible outside it" behavior for a nested scope."""
  generator = _schemas_generator()
  root_path = FIXTURE_ROOT / 'spec' / 'schemas.json'
  futures_path = FIXTURE_ROOT / 'spec' / 'endpoints' / 'futures' / 'schemas.json'
  schemas_references = {
    root_path: _rendered_references(generator, root_path, None),
    futures_path: _rendered_references(generator, futures_path, ('futures',)),
  }
  spec_root = FIXTURE_ROOT / 'spec'

  futures_leaf = spec_root / 'endpoints' / 'futures' / 'leverage'
  resolved = generator._resolve_schemas(futures_leaf, spec_root, schemas_references)
  assert set(resolved) == {'OrderSide', 'OrderLedgerEntry', 'OrderLedgerResponse', 'FuturesSide'}

  market_leaf = spec_root / 'endpoints' / 'market' / 'orderbook'
  resolved = generator._resolve_schemas(market_leaf, spec_root, schemas_references)
  assert set(resolved) == {'OrderSide', 'OrderLedgerEntry', 'OrderLedgerResponse'}
  assert 'FuturesSide' not in resolved


def test_resolve_schemas_raises_on_shadowing_collision():
  """A genuine id collision across two scopes on the same path to root -- unreachable
  once `check_schemas_no_shadowing` gates it at spec-test time, but `_resolve_schemas`
  itself must not silently resolve it by nearer-wins precedence (design §5b), the
  identical backstop-over-a-spec-test-check shape `_resolve_core` already keeps for
  `check_router_core`."""
  generator = _schemas_generator()
  spec_root = FIXTURE_ROOT / 'spec'
  root_path = spec_root / 'schemas.json'
  futures_path = spec_root / 'endpoints' / 'futures' / 'schemas.json'
  schemas_references = {
    root_path: {'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.schemas'}},
    futures_path: {
      'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.futures.schemas'}
    },
  }
  with pytest.raises(ValueError, match='OrderSide'):
    generator._resolve_schemas(
      spec_root / 'endpoints' / 'futures' / 'leverage', spec_root, schemas_references,
    )


def _generator() -> Generator:
  """A `Generator` wired the way the CLI wires one, against the fixture client --
  `client_root`/`codegen_config` set as plain post-construction attributes, per Task 14's
  finding that `Generator` has no constructor of its own for either."""
  generator = Generator()
  generator.project = resolve(FIXTURE_ROOT)
  generator.codegen_config = load_codegen_toml(FIXTURE_ROOT)
  return generator


def test_rpc_endpoint_renders_request_call():
  """A flat, query-role GET (`market/orderbook`) collapses to design §2's single
  `self.request(...)` call: a `Request` TypedDict built from `symbol` (the request's
  only property), the response type resolved from the response schema's own `title`
  (`Orderbook`) -- except it collides with the endpoint class name `rpc_endpoint` is
  handed (`class_name='Orderbook'`, matching the directory-derived name a real caller
  would choose), so it grows to `OrderbookResponse` (`type_generator.forbidden`), the
  same way a genuine schema-title collision already does via `naming.disambiguate`.

  The task brief's own draft test left the response-type assertion soft (`Orderbook_`
  or a looser `response_type=Orderbook` substring check) pending confirmation of the
  real naming convention -- firmed up here to the exact resolved name, `OrderbookResponse`,
  once collision-avoidance is accounted for."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'orderbook'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='Orderbook', method_name='orderbook', endpoint_dir=root,
  )
  assert 'class Orderbook(RpcEndpoint):' in code
  assert 'from fixture_client.core import RpcEndpoint' in code
  assert 'async def orderbook(' in code
  assert 'symbol: str' in code
  assert "method='GET'" in code
  assert "path='/v1/orderbook'" in code
  assert "meta={'public': True}" in code
  assert 'Request(symbol=symbol)' in code
  assert 'request_type=Request' in code
  assert 'response_type=OrderbookResponse' in code
  assert '-> OrderbookResponse:' in code
  assert 'class Request(TypedDict):' in code
  assert 'class OrderbookResponse(TypedDict):' in code


def test_rpc_endpoint_deprecated_endpoint_renders_decorator():
  """`endpoint.deprecated` (a top-level `Endpoint` field, a sibling of `spec` like
  `pagination`/`auth`) is unrelated to the request/response shape `rpc_endpoint` renders
  from -- before this fix, `rpc_endpoint` never read it at all, so a new-shape endpoint
  spec'd `deprecated: true` silently generated an undecorated method. The legacy
  openapi-shaped predecessor read `op.deprecated` (OpenAPI's own field) instead, which
  has no equivalent once an endpoint migrates off `openapi`.

  No real fixture endpoint declares `deprecated: true` (nor did alchemy's own migration,
  the first real client through this code path -- moralis's is the first to have any),
  so this constructs a modified copy of the `market/orderbook` fixture endpoint in
  memory via `model_copy`, rather than editing the shared fixture file other tests also
  depend on.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'orderbook'
  endpoint = load_endpoint(root / 'endpoint.json').model_copy(update={'deprecated': True})
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='Orderbook', method_name='orderbook', endpoint_dir=root,
  )
  assert "@deprecated('Deprecated method')" in code
  assert 'deprecated' in code.splitlines()[0] and 'import' in code.splitlines()[0]


def test_rpc_endpoint_signed_post_with_decimal_string_field():
  """A signed POST (`market/order`) carries `signed=True` in the call, and its
  `decimal-string`-formatted `quantity` field (rule 15) renders as `Decimal` -- the
  same machinery response schemas already use, now confirmed to trigger through the
  new `request` schema path too, with zero endpoint-specific handling in
  `rpc_endpoint` itself."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceOrder', method_name='place_order', endpoint_dir=root,
  )
  assert 'class PlaceOrder(RpcEndpoint):' in code
  assert "meta={'signed': True}" in code
  assert 'quantity: Decimal' in code
  assert 'from decimal import Decimal' in code
  assert "method='POST'" in code
  assert "path='/v1/order'" in code
  assert 'Request(symbol=symbol, quantity=quantity)' in code
  assert 'response_type=OrderAck' in code
  # Only `symbol` (the first declared field) can ever be positional, and only because
  # its own type (`str`) is unique among the request's fields (`_flat_request_kwargs`) --
  # `quantity` (`Decimal`) is always keyword-only.
  assert 'self, symbol: str, *,' in code
  assert 'quantity: Decimal, validate: bool | None = None,' in code


def test_rpc_endpoint_bare_flat_object_post_no_special_casing():
  """The ~990-endpoint gap design §7 closes: a POST endpoint whose `request` is a flat
  object with no `requestBody`/`parameters` distinction at all -- and, unlike
  `market/order`, no `decimal-string` field and no signing -- renders through the exact
  same `rpc_endpoint` code path as the GET and signed-POST cases above, with no
  `parameters`/`requestBody`-shaped branching anywhere in `rpc_endpoint`'s own
  implementation (confirmed by reading it, not just by this test passing).

  `market/order_cancel` also exercises a wire property name (`orderId`) that isn't
  already a valid, unchanged Python identifier: the generated parameter is `order_id`
  (`Generator.identifier`/`safe_identifier` snake-cases it), while the `Request(...)`
  call still keys it by the real wire name `orderId` -- confirming the two names are
  tracked separately rather than assumed identical, unlike `market/orderbook`'s
  `symbol` (already snake_case, so the two happen to coincide there)."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_cancel'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='CancelOrder', method_name='cancel_order', endpoint_dir=root,
  )
  assert 'class CancelOrder(RpcEndpoint):' in code
  assert "meta={'public': True}" in code
  assert "method='POST'" in code
  assert "path='/v1/order/cancel'" in code
  assert 'order_id: str' in code
  assert 'symbol: str' in code
  assert 'Request(orderId=order_id, symbol=symbol)' in code
  assert 'response_type=CancelAck' in code
  # Two fields sharing the same type (`str`) -- neither one is positional, per
  # `_flat_request_kwargs`'s "only the first field, only if its type is unique" rule.
  assert 'self, *,' in code
  assert 'order_id: str, symbol: str, validate: bool | None = None,' in code
  for keyword in ('parameters', 'requestBody', 'openapi'):
    assert keyword not in code


def test_rpc_endpoint_required_single_value_enum_gets_literal_default():
  """A required, single-value `enum` request property (`market/order_dispatch`'s
  `action`, fixed to `'list'`) renders with a literal Python default -- design §2's
  "Transport" subsection names this "the already-existing 'required single-value enum
  gets a literal default' rule (originally for etherscan's `module`/`action` dispatch
  constants)", but no implementation of it existed anywhere in the universal `Generator`
  before this fix: `_rpc_request_params` gave every `request` property either no default
  (required) or `None` (optional), with nothing reading a schema's own `enum`. etherscan's
  V2 API multiplexes nearly every operation through fixed `module`/`action` query
  constants -- exactly this shape, on ~88 endpoints -- so this is a real, blocking gap for
  that migration, not a hypothetical.

  The field stays genuinely required on the *wire*: it's unconditionally included in the
  `Request(...)` call (never `NotRequired`), so a recorded example still has to carry it
  explicitly and the mock server's exact-length `expected_query` match still sees it --
  only the generated *Python* signature gains a default, so a caller never has to type out
  a value with exactly one legal choice.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_dispatch'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderDispatch', method_name='order_dispatch', endpoint_dir=root,
  )
  assert 'class OrderDispatch(RpcEndpoint):' in code
  # Still a real, required field on the wire -- unconditionally passed to `Request(...)`.
  assert "Request(symbol=symbol, action=action)" in code
  # But the generated Python parameter carries a literal default, so a caller can omit it.
  assert "action: Literal['list'] = 'list'" in code
  assert 'class Request(TypedDict):' in code
  assert '  action: Literal[' in code  # required in the TypedDict too, never NotRequired
  ast.parse(code)


def test_rpc_endpoint_optional_request_property():
  """A genuinely optional (`NotRequired`) `request` property (`market/order_status`'s
  `clientId`, alongside the required `orderId`) -- the real-world norm rather than an
  edge case, since nearly every fleet endpoint has at least one optional field.

  Confirms all three pieces this needed, fixed after an initial pass that unconditionally
  passed every field to `Request(...)` regardless of `required`:

  1. The generated `Request` TypedDict marks the optional property `NotRequired[str]`
     (already correct before the fix -- `truewire.generation`'s own `record()` codegen,
     reused unchanged for `Request`, already keys off the schema's `required` list the
     same way it already did for every response type).
  2. The generated method's own parameter is optional too (`client_id: str | None = None`,
     `Function.Param`'s existing `required=False` rendering -- also already correct
     before the fix, since `_rpc_request_params` was already passing the schema's real
     `required` flag through to `Function.Param`; only the `Request(...)` construction
     itself needed fixing).
  3. The optional field is only written into the wire request when the caller actually
     supplied a non-`None` value -- the real fix: `_rpc_request_params` now emits a
     `request: Request = Request(<required fields>)` declaration plus one `if {param} is
     not None: request['{name}'] = {param}` line per optional field, instead of always
     passing every field as a `Request(...)` keyword argument.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_status'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderStatusEndpoint', method_name='order_status', endpoint_dir=root,
  )
  assert 'class OrderStatusEndpoint(RpcEndpoint):' in code
  assert "meta={'signed': True}" in code
  assert "method='GET'" in code
  assert "path='/v1/order/status'" in code

  # (1) `Request` TypedDict: `orderId` required, `clientId` `NotRequired`.
  assert 'class Request(TypedDict):' in code
  assert '  orderId: str' in code
  assert '  clientId: NotRequired[str]' in code

  # (2) generated parameter: `order_id: str` (required, kwonly since both fields share
  # no type collision exemption here -- `order_id` alone would qualify, but is pushed to
  # kwonly regardless since `client_id` is declared right after it in the signature and
  # `_flat_request_kwargs` only ever promotes the very first field); `client_id` optional
  # with a `None` default.
  assert 'order_id: str, client_id: str | None = None, validate: bool | None = None,' in code

  # (3) `Request(...)` construction: only `orderId` (required) is passed unconditionally;
  # `clientId` is set conditionally, never unconditionally with a possibly-`None` value.
  assert 'request: Request = Request(orderId=order_id)' in code
  assert "if client_id is not None:" in code
  assert "request['clientId'] = client_id" in code
  assert 'Request(orderId=order_id, clientId=client_id)' not in code
  assert 'return await self.request(\n      request,' in code


def test_rpc_endpoint_titled_anyof_discriminated_request():
  """A titled `anyOf` request over two titled object variants (`market/order_submit`'s
  `LimitOrderRequest`/`MarketOrderRequest`) -- `docs/spec/authoring.md` rule 0's deribit
  `buy`/`sell` worked example, the one shape S12 explicitly calls out for a discriminated
  request body.

  This is a regression test for a real bug: `_rpc_request_params`'s `anyOf` branch
  originally called `type_generator.render.parser(request_schema, id=None)` directly on
  the raw (un-normalized) schema, bypassing `Normalizer`/`Unnest.records()` -- each
  titled-object variant then reached `Parser.any_of`'s `self.inline(v)` call as a raw
  `Schema` rather than the `Reference` `Unnest.records()` would have substituted, and
  `Parser.inline` refuses to inline a record type at all
  (`ValueError('Found nested record type')`). The fix reuses `rpc_endpoint`'s own
  already-registered `type_generator(schemas, inline=True)` resolution
  (`request_type`) instead of re-deriving the union's type independently -- the same
  mechanism deribit's own real hand-rolled backend already uses successfully.

  Confirms the generated module actually parses as valid Python (`ast.parse`), not just
  that generation itself doesn't raise -- the prior manual verification for this case
  apparently used a non-representative (scalar, not object) `anyOf`, which is how the
  bug above slipped through undetected.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_submit'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderSubmit', method_name='order_submit', endpoint_dir=root,
  )
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'class OrderSubmit(RpcEndpoint):' in code
  assert "meta={'signed': True}" in code
  assert "method='POST'" in code
  assert "path='/v1/order/submit'" in code

  # Both anyOf variants render as real, separately-defined classes.
  assert 'class LimitOrderRequest(TypedDict):' in code
  assert '  symbol: str' in code
  assert '  price: Decimal' in code
  assert 'class MarketOrderRequest(TypedDict):' in code

  # The union is bound to the fixed local name `Request` (design §2/§7's convention,
  # shared with the flat-object case), not independently re-derived or left unbound.
  assert 'Request = LimitOrderRequest | MarketOrderRequest' in code

  # A single union-typed parameter carries the whole request -- no per-variant-field
  # flattening (S12's own anti-pattern).
  assert 'order_request: Request' in code
  assert 'request_type=Request' in code
  assert 'response_type=OrderSubmitAck' in code

  # request_value_expr is just the parameter's own name -- passed straight through,
  # not reconstructed.
  assert 'return await self.request(\n      order_request,' in code


def test_rpc_endpoint_bare_array_request():
  """A bare JSON Schema `array` request -- `{"type": "array", "items": {...}}`, no
  enclosing object at all -- mirroring bitget's real `uta.trade.order.place_batch`
  (`PlaceBatchOrdersRequest`: a titled array of `anyOf`-variant order items).

  Regression test for a real, silent-data-loss bug: `_rpc_request_params` originally had
  no branch for this shape. `request_schema.properties` is `None` on an array schema, so
  `properties = request_schema.properties or {}` silently evaluated to `{}`,
  `field_params` stayed empty, and the method fell straight through to the final `if not
  field_params: return [], [], [], 'Request()', []`, generating a *zero-parameter* method
  whose body always sent `Request()` -- `list[Item]()`, i.e. `[]` -- as the request body,
  discarding whatever the caller actually meant to send. Confirmed live: bitget's 3 real
  batch endpoints (`uta.trade.order.{place,cancel,modify}_batch`) all generated this way,
  and all 3 real replay tests failed with a 422 from the mock server.

  Mirrors bitget's real shape as closely as a fixture reasonably can (an array of `anyOf`
  variant records, both titled), proving the item type resolves through the identical
  `Unnest`/`type_generator` machinery the `anyOf`-request branch already reuses, rather
  than needing its own independent re-derivation.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request'] = {
    'title': 'PlaceBatchOrdersRequest',
    'description': 'Orders to place in one batch.',
    'type': 'array',
    'items': {
      'description': 'One order within the batch.',
      'anyOf': [
        {
          'title': 'BatchLimitOrderItem', 'type': 'object',
          'description': 'A limit order within the batch.',
          'properties': {
            'symbol': {'type': 'string', 'description': 'Trading pair.'},
            'price': {'type': 'string', 'format': 'decimal-string', 'description': 'Limit price.'},
          },
          'required': ['symbol', 'price'],
        },
        {
          'title': 'BatchMarketOrderItem', 'type': 'object',
          'description': 'A market order within the batch.',
          'properties': {
            'symbol': {'type': 'string', 'description': 'Trading pair.'},
          },
          'required': ['symbol'],
        },
      ],
    },
  }
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceBatchOrders', method_name='place_batch_orders',
    endpoint_dir=root,
  )
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  # Both anyOf item variants render as real, separately-defined classes.
  assert 'class BatchLimitOrderItem(TypedDict):' in code
  assert 'class BatchMarketOrderItem(TypedDict):' in code

  # The whole array is bound to the fixed local name `Request`, a plain type alias --
  # there's nothing to wrap it in, so it's never a `TypedDict`.
  assert 'Request = list[BatchLimitOrderItem | BatchMarketOrderItem]' in code
  assert 'class Request' not in code

  # A single, real, list-typed parameter carries the whole request -- not a
  # zero-parameter method that silently discards it.
  assert 'place_batch_orders_request: Request' in code
  assert 'request_type=Request' in code

  # request_value_expr is just the parameter's own name, passed straight through as the
  # request body -- never a `Request()` call (the bug this test guards against: an empty
  # constructor call standing in for the caller's real list).
  assert 'return await self.request(\n      place_batch_orders_request,' in code
  assert 'Request()' not in code


def test_rpc_endpoint_bare_array_request_untitled_scalar_items():
  """The same bare-`array`-request branch as `test_rpc_endpoint_bare_array_request`, at
  its simpler, fallback-naming edge: no `title` of its own (`request_schema.title or
  'items'`) and plain scalar items rather than a nested `anyOf` of records."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request'] = {
    'description': 'Order ids to cancel in one batch.',
    'type': 'array',
    'items': {'type': 'string', 'description': 'An order id.'},
  }
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='CancelBatchOrders', method_name='cancel_batch_orders',
    endpoint_dir=root,
  )
  ast.parse(code)

  assert 'Request = list[str]' in code
  assert 'items: Request' in code
  assert 'request_type=Request' in code
  assert 'return await self.request(\n      items,' in code
  assert 'Request()' not in code


def test_rpc_endpoint_flat_request_property_is_a_shared_schema_ref():
  """A flat (non-`anyOf`) request whose own property is itself a `$ref` into a shared
  schema (`market/order_preview`'s `side`, `$ref: 'OrderSide'`, design §5b/Task 24b) --
  a regression test for a real bug found while building this fixture case: `_rpc_request_
  params`'s flat-property loop called `ensure_nonref(prop)`, which *raises* on a genuine
  `Reference` rather than resolving it, even though the very same property already
  renders correctly inside `Request` itself (a few lines earlier in `rpc_endpoint`, via
  the registered `type_generator(schemas, inline=True)` call, which does know how to
  resolve an external reference). Fixed by resolving a `Reference` property directly out
  of `type_generator.external_references` -- the identical map that rendering already
  used -- instead of trying to route it through `renderer.parser`/`renderer.code`, which
  only ever accepts a `Schema`.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_preview'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  references = {'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.schemas'}}
  code = generator.rpc_endpoint(
    endpoint, references, class_name='OrderPreview', method_name='order_preview',
    endpoint_dir=root,
  )
  ast.parse(code)

  assert 'from fixture_client.schemas import OrderSide' in code
  assert 'class Request(TypedDict):' in code
  assert '  side: OrderSide' in code
  # The generated method's own parameter uses the identical resolved type name, not a
  # re-derived/inlined one.
  assert 'side: OrderSide, validate: bool | None = None,' in code
  assert 'Request(symbol=symbol, side=side)' in code


def test_rpc_endpoint_bare_ref_response_is_a_shared_schema_ref():
  """A `response` schema that is, in its entirety, one bare `{"$ref": "..."}` --
  `market/order_last_side`'s own response, `{"$ref": "OrderSide"}` -- found while
  migrating alchemy (`simulation.execution`/`simulation.asset_changes`, whose response is
  the whole, twice-referenced `ExecutionResult`/`AssetChangesResult` shared schema, not a
  `$ref` nested inside an object or array). Before this fix, `rpc_endpoint` always ran
  `Schema.model_validate(spec.response)` unconditionally; `Schema` (openapi_schema_pydantic)
  has no `ref` field and its own `model_config` is `extra='allow'`, so `$ref` was silently
  accepted as an ignored extra key and validated as an empty, untyped schema (`type` and
  `properties` both `None`) instead of raising -- confirmed directly against `Schema` before
  writing this test. `rpc_endpoint` now special-cases exactly this shape, resolving it the
  same way a `$ref`-typed flat *request* property already is
  (`test_rpc_endpoint_flat_request_property_is_a_shared_schema_ref`, just above): directly
  out of `references`, with no local `schemas['$response']` entry and no attempt to render
  a definition for it.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_last_side'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  references = {'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.schemas'}}
  code = generator.rpc_endpoint(
    endpoint, references, class_name='OrderLastSide', method_name='order_last_side',
    endpoint_dir=root,
  )
  ast.parse(code)

  assert 'from fixture_client.schemas import OrderSide' in code
  assert 'response_type=OrderSide' in code
  assert '-> OrderSide:' in code
  # No local `OrderSide` (re)definition is emitted -- it's a real import, not inlined.
  assert 'class OrderSide' not in code


def test_endpoint_schemas_reports_a_bare_ref_response_as_a_reference():
  """`endpoint_schemas` -- consulted by `type_names`/`class_name` (collision-avoidance
  naming, run by the CLI *before* `rpc_endpoint` itself resolves the real imported name
  for a bare-`$ref` response) -- must see the same bare-`$ref`-response shape
  `rpc_endpoint` special-cases (`test_rpc_endpoint_bare_ref_response_is_a_shared_schema_ref`,
  just above), not just plain, renderable schemas.

  Before this fix, `endpoint_schemas` always ran `Schema.model_validate(spec.response)`
  unconditionally, exactly the bug `rpc_endpoint` itself already had fixed for it --
  `Schema` (openapi_schema_pydantic) has no `ref` field, so a bare `{"$ref": "OrderSide"}`
  silently validated as an empty, untyped schema, and `type_names`'s own `rendered.
  imports` (built from that empty schema) reported nothing for it. `class_name` then had
  no way to see that this leaf's own response type is `OrderSide`, and would happily hand
  out `OrderSide` as the endpoint's own class name if the two happened to collide -- found
  live on moralis's migration: `evm.nft.metadata.collection_metadata` generates a leaf
  class literally named `CollectionMetadata`, which also imports a same-named response
  type `CollectionMetadata` from `typed_moralis.schemas`, and nothing caught it before
  this fix.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_last_side'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  references = {'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.schemas'}}
  names = generator.type_names(endpoint, references)
  assert 'OrderSide' in names

  # class_name must therefore refuse to hand out the colliding name.
  chosen = generator.class_name(endpoint, references, name='OrderSide')
  assert chosen != 'OrderSide'


def test_rpc_endpoint_nested_record_request_property():
  """A flat (non-`anyOf`) request whose own property is an *inline*, single-use nested
  record -- or an array of one -- (`market/order_batch`'s `orders: list[BatchOrderItem]`)
  -- alchemy's real `nft.get_nft_metadata_batch` (`tokens: list[NftMetadataBatchToken]`)
  is the case that surfaced this.

  Regression test for a real bug: `_rpc_request_params`'s flat-property loop originally
  called `renderer.parser(prop, id=None)` directly for every non-`$ref` property, the same
  un-normalized-`Schema` bug `test_rpc_endpoint_titled_anyof_discriminated_request`'s
  `anyOf` branch already had fixed for it -- `Parser.array`'s own `self.inline(...)` call
  refuses any record type outright (`ValueError: Found nested record type`), titled or
  not. Fixed by resolving from `types.identifiers` first (`$request/{name}` or
  `$request/{name}/item`, `Unnest`'s own array-element convention), falling through to the
  direct parse unchanged for every scalar/array-of-scalar property.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_batch'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  references = {'OrderSide': {'name': 'OrderSide', 'package': 'fixture_client.schemas'}}
  code = generator.rpc_endpoint(
    endpoint, references, class_name='OrderBatch', method_name='order_batch',
    endpoint_dir=root,
  )
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'class BatchOrderItem(TypedDict):' in code
  assert 'class Request(TypedDict):' in code
  assert '  orders: list[BatchOrderItem]' in code
  assert 'orders: list[BatchOrderItem]' in code  # the generated parameter itself
  assert 'Request(orders=orders)' in code


def test_rpc_endpoint_wires_pagination_into_paged_method():
  """A declared `pagination` block (`market/order_list`, `token`/`absent_cursor` with a
  declared `done.rows` naming a titled-record row schema) must actually generate a
  `<method>_paged` companion -- design §7 is silent on pagination (it's untouched by the
  request/response collapse), but `rpc_endpoint` still has to call `paged_method`/
  `paged_response_method` itself.

  Regression test for a real bug: `rpc_endpoint` never read `endpoint.pagination` at all,
  so every endpoint declaring `pagination` silently generated no `_paged` companion
  whatsoever (S18/S24) -- confirmed by reverting the fix and generating this exact
  endpoint below. Fixed by wiring in the same shared `paged_method`/`paged_response_method`
  primitives every already-migrated (legacy `openapi`-shaped) backend already calls.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_list'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderList', method_name='order_list', endpoint_dir=root,
  )
  ast.parse(code)

  assert 'def order_list_paged(' in code
  assert 'PaginatedResponse[OrderListItem, str]' in code
  assert 'from truewire_core import PaginatedResponse' in code


def test_rpc_endpoint_paginated_non_record_rows_stays_paginated_response_shaped():
  """A declared `pagination` block whose row schema is *not* a titled record --
  `market/order_id_list`'s `orderIds: list[str]`, a plain scalar array, mirroring
  alchemy's real `nft.get_owners_for_nft` (`owners: list[str]`) -- must still render the
  richer `PaginatedResponse`-shaped `_paged` method, not silently downgrade to a plain
  async generator.

  Regression test for a real bug: `Unnest` only assigns an identifier to a titled-record
  row schema, so `rpc_endpoint`'s original `rows_type` lookup (straight off
  `types.identifiers`) resolved to `None` for a scalar row type and silently fell back to
  `paged_method`'s plain-async-generator shape -- an S24 regression from what the
  pre-migration hand-written core already published for this exact endpoint shape.
  `paged_response_rows_type` fixes this by falling back to regex-extracting the field's
  own rendered type straight off the parent response's own definition
  (stripping `NotRequired[...]`/`| None`/`list[...]`) whenever `Unnest` gives it nothing.
  Confirmed by reverting the fix and generating this exact endpoint below (it degrades to
  `AsyncIterator[OrderIdListResponse]`, no `PaginatedResponse` at all).
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_id_list'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderIdList', method_name='order_id_list', endpoint_dir=root,
  )
  ast.parse(code)

  assert 'def order_id_list_paged(' in code
  assert 'PaginatedResponse[str, str]' in code
  assert 'AsyncIterator' not in code


def test_rpc_endpoint_paginated_bare_ref_response_stays_paginated_response_shaped():
  """A declared `pagination` block on an endpoint whose *entire* response is a bare
  `$ref` (`market/order_ledger`'s `{"$ref": "OrderLedgerResponse"}`) must still render
  the richer `PaginatedResponse`-shaped `_paged` method -- `OrderLedgerResponse` is
  itself declared in `spec/schemas.json`, and its own `orders` rows are themselves
  `$ref`'d to another schemas.json entry (`OrderLedgerEntry`), the exact double-`$ref`
  shape moralis's real `streams.evm.all_streams` (and 6 more `streams.*` endpoints) hit.

  Regression test for a real bug: a bare-`$ref` response is never added to `schemas`
  at all (`rpc_endpoint`'s own `response_ref` special case -- see
  `test_rpc_endpoint_bare_ref_response_is_a_shared_schema_ref`, above), so `types`
  carries no `$response/...` entries whatsoever and both of `paged_response_rows_type`'s
  original resolution paths (`Unnest`'s own identifier, and a regex over the parent
  record's own rendered field) always failed -- silently downgrading every such endpoint
  to a plain async generator (confirmed live on moralis: 7 real endpoints). Fixed by
  giving `paged_response_rows_type` a third path, reading the referenced schema directly
  out of `self.shared_schemas` (`spec/schemas.json`'s raw, parsed schemas) and resolving
  its own row-items `$ref` against `references` -- exactly mirrored here.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_ledger'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  generator.shared_schemas = load_schemas(FIXTURE_ROOT)
  references = {
    'OrderLedgerResponse': {'name': 'OrderLedgerResponse', 'package': 'fixture_client.schemas'},
    'OrderLedgerEntry': {'name': 'OrderLedgerEntry', 'package': 'fixture_client.schemas'},
  }
  code = generator.rpc_endpoint(
    endpoint, references, class_name='OrderLedger', method_name='order_ledger',
    endpoint_dir=root,
  )
  ast.parse(code)

  assert 'def order_ledger_paged(' in code
  assert 'PaginatedResponse[OrderLedgerEntry, str]' in code
  assert 'from fixture_client.schemas import OrderLedgerEntry' in code


def test_rpc_endpoint_page_total_with_rows_stays_paginated_response_shaped():
  """A declared `pagination` block using `page` strategy, terminated by a `total` that
  also declares `done.rows` (`market/order_page_total`, mirroring binance's real
  `spot.http.mining.*`/`bfusd`/`rwusd` `history.rate_history`-shaped endpoints --
  `{rows: [...], total: N}`, rule 8's own worked `done.rows` example) must render the
  richer `PaginatedResponse`-shaped `_paged` method via `paged_response_page_total`, not
  the plain async-generator `paged_method`.

  Regression test for a real bug: `rpc_endpoint`'s own `rows_type` resolution only ever
  set `rows_type` for `token`/`absent_cursor` and plain `seek` strategies -- `page`
  strategy fell straight through to `paged_method`'s plain-generator fallback regardless
  of whether `paged_response_page_total` (S24's own dispatch target for `page` strategy,
  already implemented and already reachable from `paged_response_method`) could have
  rendered it. `paged_response_page_total` itself was never actually reachable from
  `rpc_endpoint` for any client before this fix -- found while investigating a user-
  reported validation error traced to binance's bfusd `rate_history`'s `total` field,
  whose generated `_paged` method turned out to be a plain `AsyncIterator` despite
  qualifying for the richer shape. binance alone has 108 real endpoints of this exact
  shape once this fix lands (confirmed by a full-fleet spec scan), none of them
  previously reachable through `paged_response_page_total` at all.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_page_total'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderPageTotal', method_name='order_page_total',
    endpoint_dir=root,
  )
  ast.parse(code)

  assert 'def order_page_total_paged(' in code
  assert 'PaginatedResponse[OrderPageTotalItem, int]' in code
  assert 'from truewire_core import PaginatedResponse' in code
  assert 'AsyncIterator' not in code


def test_rpc_endpoint_paginated_anyof_wrapped_response_stays_paginated_response_shaped():
  """A declared `pagination` block (`market/order_nullable_list`, `token`/
  `absent_cursor` with a declared `done.rows`) whose response is itself `anyOf`-wrapped
  with a `null` branch (`docs/spec/authoring.md` rule 5's nullable-record shape,
  `{"anyOf": [OrderNullableListPage, {"type": "null"}]}`, no title on the wrapper
  itself) must still render the richer `PaginatedResponse`-shaped `_paged` method, not
  silently downgrade to a plain async generator -- kucoin's real `spot.orders_hf.
  get_closed_orders`/`margin.orders_hf.get_closed_orders` are this exact shape.

  Regression test for a real bug: `Unnest` keys a titled record nested inside an `anyOf`
  wrapper under `$response/anyOf/{i}/...`, never under `$response/...` directly (only the
  wrapper's own union alias, e.g. `Response = OrderNullableListPage | None`, lives at
  `$response` itself) -- `paged_response_rows_type`'s original two resolutions only ever
  looked directly under `$response`, so both failed silently and `rows_type` stayed
  `None`, falling straight through to `paged_method`'s plain-generator fallback despite
  the endpoint qualifying for S24's richer shape. Confirmed by reverting the fix and
  generating this exact endpoint below (it degrades to `AsyncIterator[Response]`, no
  `PaginatedResponse` at all). Fixed by retrying the same two resolutions against every
  `$response/anyOf/{i}` branch `Unnest` actually extracted its own record for.

  Also covers the follow-on null-safety fix: `response` (the single-request method's own
  return value) is genuinely nullable here (`Response = OrderNullableListPage | None`),
  so the generated `_paged` method's own row/cursor reads must guard it (`response.get(...)
  if response is not None else None`), not assume non-`None` "by construction" the way
  every already-migrated, non-nullable-response caller's generated code correctly does.
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_nullable_list'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderNullableList', method_name='order_nullable_list',
    endpoint_dir=root,
  )
  ast.parse(code)

  assert 'def order_nullable_list_paged(' in code
  assert 'PaginatedResponse[OrderNullableListItem, str]' in code
  assert 'from truewire_core import PaginatedResponse' in code
  assert 'AsyncIterator' not in code
  assert 'Response = OrderNullableListPage | None' in code
  assert "response.get('orders') if response is not None else None" in code
  assert "response.get('pageKey') if response is not None else None" in code


def test_rpc_endpoint_offset_total_with_no_size_generates_no_paged_variant():
  """A real, structural pagination-shape limitation `paged_step` refuses to render: an
  `offset` walk terminated by an item-counted `total`, with no declared `size` parameter
  at all (so no page-size expression, and no venue-documented default, exists to advance
  the offset by). kraken's real `spot.account.trades_history` is exactly this shape.

  `rpc_endpoint` must degrade gracefully -- generate the plain single-request method with
  no `_paged` companion at all -- rather than propagating `paged_step`'s `ValueError` and
  refusing to generate the endpoint's module entirely. Regression test for the review
  finding that the original fix wrapped the *entire* `paged_method` call in `try/except
  ValueError` (over-broad: would also swallow a real, unrelated bug from anywhere else
  in that method's body) -- narrowed to wrap only the specific `paged_step` call that can
  actually raise, with `paged_method` itself returning `None` gracefully instead.

  Confirmed the raise is real and reachable: without the fix (`paged_step`'s bare
  `ValueError`, uncaught), generating this exact fixture raises `ValueError` instead of
  returning a clean module -- reproduced by temporarily reverting the narrowed
  `try/except` before writing this test."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['pagination'] = {
    'strategy': 'offset',
    'offset': {'parameter': 'symbol'},  # any declared request property works as the driver
    'done': {'kind': 'total', 'path': 'count', 'counts': 'items'},
    # No `size` declared at all -- the exact shape `paged_step` has nothing to compute a
    # step from (no page-size parameter, and therefore no default to fall back to either).
  }
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceOrder', method_name='place_order', endpoint_dir=root,
  )
  ast.parse(code)  # must generate valid Python, not raise ValueError

  assert 'async def place_order(' in code
  assert 'place_order_paged' not in code
  assert 'PaginatedResponse' not in code
  assert 'AsyncIterator' not in code


def test_rpc_endpoint_rejects_non_dict_meta():
  """`endpoint.meta` must be a real dict to generate -- `Endpoint._require_meta_for_new_shape`
  only checks `meta is not None`, so `"meta": false` (accepted at spec-load time) must
  not silently coerce to `{}` inside `rpc_endpoint` and generate a method indistinguishable
  from a genuinely public endpoint. `market/orderbook`'s own endpoint, reused here with
  `meta` overridden to `False`, would otherwise be a real, silent regression."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'orderbook'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['meta'] = False
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  with pytest.raises(ValueError, match='meta'):
    generator.rpc_endpoint(
      endpoint, {}, class_name='Orderbook', method_name='orderbook', endpoint_dir=root,
    )


def test_rpc_endpoint_renders_plain_dict_literal_when_core_declares_a_schema():
  """`futures` (`codegen/config.toml`'s top-level `[cores.futures]`, added alongside the design
  §2/§6 `meta` mechanism) declares a real `meta` schema (`signed` required, `public`
  optional) -- `futures/positions` (`meta: {"signed": true}`) renders a plain dict
  literal (`meta={'signed': True}`) as the call argument, never a generated `Meta` type:
  `Meta` is hand-written directly in the resolved core's own module
  (`fixture_client.futures.core.Meta`), matching the declared schema, and pyright checks
  the emitted dict literal against that hand-written `meta: Meta`-typed parameter
  structurally -- confirmed by `test_codegen_generator_e2e.py`'s real pyright run, not
  just by this generation-only test. No `Meta` class, and no `Meta` import, appears
  anywhere in this generated module."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'futures' / 'positions'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='Positions', method_name='positions', endpoint_dir=root,
  )
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'class Meta' not in code
  assert 'import Meta' not in code
  assert "meta={'signed': True}" in code
  assert 'class Positions(FuturesEndpoint):' in code


def test_rpc_endpoint_omits_meta_argument_when_core_declares_no_schema():
  """`chain` (`token/`'s resolved core) declares no top-level `[cores.chain]` entry at
  all -- the common case, most cores need none (design §6) -- so `token/balances/get`'s
  `meta: {}` renders no `Meta` type and no `meta=` argument in the generated call,
  never the old bare-kwargs splat and never a literal `meta={}`."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'token' / 'balances' / 'get'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='Balances', method_name='balances', endpoint_dir=root,
  )
  ast.parse(code)

  assert 'class Meta' not in code
  assert 'meta=' not in code
  assert 'meta={}' not in code
  assert 'class Balances(ChainRpc):' in code


def test_rpc_endpoint_raises_on_non_empty_meta_with_no_declared_schema():
  """A non-empty `meta` on a core with no declared schema has nowhere to be validated
  (design §6) -- generation fails loudly instead of silently dropping the declared
  values or falling back to the retired bare-kwargs splat. Constructs a modified copy of
  `token/balances/get` (whose real `chain` core declares no schema) rather than editing
  the shared fixture file other tests also depend on."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'token' / 'balances' / 'get'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['meta'] = {'public': True}
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  with pytest.raises(ValueError, match='no `meta` schema'):
    generator.rpc_endpoint(
      endpoint, {}, class_name='Balances', method_name='balances', endpoint_dir=root,
    )


def test_stream_endpoint_renders_subscribe_call():
  """`market/ticker_stream` (`channel: '/ticker/{symbol}'`, `parameters: {symbol}`,
  `payload: Ticker`) is a direct-channel endpoint (`_channel_direct_params`): its one
  declared `parameters` field is exactly `channel`'s own one placeholder, so
  `stream_endpoint` builds the channel string straight from the generated method's own
  `symbol` local variable (`channel_expr`'s default f-string rendering) instead of
  wrapping it in a `Parameters` TypedDict -- no `Parameters` class is generated at all
  (2026-09 codegen mechanization channel-params revision). The payload type still
  resolves from its own schema title (`Ticker`), unchanged -- unlike the old
  `parameters`, `payload` was never forced to a fixed name either way, exactly mirroring
  how `rpc_endpoint` treats `response` vs. the fixed-named `request`.

  Also confirms the generated call carries no `await` and the generated method is not
  `async` -- `self.subscribe(...)` hands back a stream/manager object constructed
  synchronously, not a single reply to await directly (design §8; matches the legacy
  per-client `stream_endpoint` backends' own `asyn=False` shape, e.g. bybit's).
  """
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'ticker_stream'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  code = generator.stream_endpoint(
    endpoint, {}, class_name='TickerStream', method_name='ticker_stream', endpoint_dir=root,
  )
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'class TickerStream(RpcEndpoint):' in code
  assert 'from fixture_client.core import RpcEndpoint' in code
  assert 'def ticker_stream(' in code
  assert 'async def ticker_stream(' not in code  # not async -- see docstring above
  assert 'self.subscribe(' in code
  assert 'return self.subscribe(' in code
  assert 'return await self.subscribe(' not in code  # no `await` -- see docstring above
  assert "f'/ticker/{symbol}'" in code
  assert "meta={'public': True}" in code
  assert 'symbol: str' in code
  assert 'response_type=Ticker' in code
  # No `Parameters` TypedDict/value/type at all -- the direct-channel shape this test
  # now exercises drops all three.
  assert 'Parameters(symbol=symbol)' not in code
  assert 'request_type=Parameters' not in code
  assert 'class Parameters(TypedDict):' not in code
  assert 'class Ticker(TypedDict):' in code
  # The generated method's own return annotation is the stream/manager object
  # `self.subscribe(...)` actually returns, never the bare payload type -- an exact,
  # non-substring check, since `'Ticker' in code` (the old, insufficient check) would
  # still pass against either shape: `Ticker` is a substring of `StreamManager[Ticker,
  # Any, Any]` too, so `'-> Ticker' in code` alone cannot tell the two apart. Task 20's
  # own end-to-end smoke test found this exact regression invisible to every assertion
  # above -- they all still pass today whichever shape `return_type` actually renders.
  assert ') -> StreamManager[Ticker, Any, Any]:' in code
  assert ') -> Ticker:' not in code
  assert 'from truewire_core.util import StreamManager' in code


def test_stream_endpoint_optional_parameters_property():
  """A genuinely optional (`NotRequired`) `parameters` property renders the same
  conditional-assignment shape `_rpc_request_params` already established for `request`
  (`test_rpc_endpoint_optional_request_property`), now confirmed to trigger unchanged
  through `_stream_parameters_params`'s own independent copy of that logic."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'ticker_stream'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['parameters']['properties']['depth'] = {
    'type': 'integer', 'description': 'Order book depth.',
  }
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.stream_endpoint(
    endpoint, {}, class_name='TickerStream', method_name='ticker_stream', endpoint_dir=root,
  )
  ast.parse(code)

  assert '  symbol: str' in code
  assert '  depth: NotRequired[int]' in code
  assert 'parameters: Parameters = Parameters(symbol=symbol)' in code
  assert 'if depth is not None:' in code
  assert "parameters['depth'] = depth" in code
  assert 'Parameters(symbol=symbol, depth=depth)' not in code


def test_stream_endpoint_rejects_non_dict_meta():
  """Mirrors `test_rpc_endpoint_rejects_non_dict_meta`: `stream_endpoint` must reject a
  non-dict `meta` (e.g. `"meta": false`) rather than silently generating a method
  indistinguishable from a genuinely public subscription."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'ticker_stream'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['meta'] = False
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  with pytest.raises(ValueError, match='meta'):
    generator.stream_endpoint(
      endpoint, {}, class_name='TickerStream', method_name='ticker_stream', endpoint_dir=root,
    )


def test_router_aggregate_multiply_inherits_alphabetically():
  """A directory holding only leaves (`market/`) multiply-inherits their generated
  classes directly -- no explicit core base, since each leaf already subclasses one in
  its own module and `self.client` reaches the composed router transitively.

  Base order is alphabetical by dict key (`'order' < 'orderbook'`), not insertion order
  -- `children` is built with `orderbook` inserted first, `order` second, to prove the
  rendered order is genuinely sorted rather than happening to match insertion order."""
  generator = _generator()
  generator.router_context = ('', ('market',))
  children = {
    'orderbook': {
      'import_path': '.orderbook', 'class_name': 'Orderbook', 'attr_name': 'orderbook',
      'kind': 'endpoint', 'transport': 'http',
    },
    'order': {
      'import_path': '.order', 'class_name': 'Order', 'attr_name': 'order',
      'kind': 'endpoint', 'transport': 'http',
    },
  }
  code = generator.router('market', children)
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'class Market(Order, Orderbook):' in code  # alphabetical: Order < Orderbook
  assert 'from .order import Order' in code
  assert 'from .orderbook import Orderbook' in code
  assert '@cached_property' not in code  # pure aggregate, nothing to compose lazily
  # market/router.json's own real docstring (added so market/ has its own closer `core`
  # declaration now that the client root's own resolves to `ClientBase`, not `RpcEndpoint`
  # -- see test_resolve_core_walks_to_nearest_ancestor's docstring)
  assert 'Market data and order endpoints.' in code


def test_router_composite_uses_cached_property():
  """A directory holding only subdirectories (`account/`) composes each as a
  `@cached_property` returning the child constructed with `client=self.client` -- and,
  having no leaf of its own to inherit `self.client` from, subclasses the resolved core
  itself (`_resolve_core`, confirmed by `test_resolve_core_walks_to_nearest_ancestor` to
  resolve `account/`'s own `router.json` declaration to `fixture_client.core:RpcEndpoint`)."""
  generator = _generator()
  generator.router_context = ('', ('account',))
  children = {
    'deposits': {
      'import_path': '.deposits', 'class_name': 'Deposits', 'attr_name': 'deposits',
      'kind': 'router', 'transport': 'http', 'mixin': 'AuthRouter', 'doc': None,
    },
    'withdrawals': {
      'import_path': '.withdrawals', 'class_name': 'Withdrawals', 'attr_name': 'withdrawals',
      'kind': 'router', 'transport': 'http', 'mixin': 'AuthRouter', 'doc': None,
    },
  }
  code = generator.router('account', children)
  ast.parse(code)

  assert 'class Account(RpcEndpoint):' in code
  assert 'from fixture_client.core import RpcEndpoint' in code
  assert 'from functools import cached_property' in code
  assert '@cached_property' in code
  assert 'def deposits(self) -> Deposits:' in code
  assert 'def withdrawals(self) -> Withdrawals:' in code
  assert 'return Deposits(client=self.client)' in code
  assert 'return Withdrawals(client=self.client)' in code


def test_router_rejects_aggregate_mixing_different_cores():
  """Two sibling leaves an aggregate multiply-inherits, resolving to two different cores,
  are refused rather than silently composed -- the moralis `token_search`/`pump_fun_*`
  regression: C3 linearization, not declaration order, decides which sibling's own core
  wins a shared method like `_base_url()` on the composed class, so every mechanical
  check (spec test, pytest, standards) can pass while a real call silently hits the
  wrong host.

  `mismatch_probe/router.json` declares `core: "default"`; its own `b/router.json`
  overrides to `core: "futures"` -- a hand-built leaf `a` (no real directory of its own)
  resolves to `default` via `_resolve_core`'s ancestor walk, `b` resolves to `futures`
  directly, and the two differ."""
  generator = _generator()
  generator.router_context = ('', ('mismatch_probe',))
  children = {
    'a': {
      'import_path': '.a', 'class_name': 'A', 'attr_name': 'a',
      'kind': 'endpoint', 'transport': 'http',
    },
    'b': {
      'import_path': '.b', 'class_name': 'B', 'attr_name': 'b',
      'kind': 'endpoint', 'transport': 'http',
    },
  }
  with pytest.raises(ValueError, match='different cores'):
    generator.router('mismatch_probe', children)


def test_router_allows_aggregate_with_matching_cores():
  """A same-core aggregate still generates cleanly -- no false positive from
  `_check_aggregate_core_consistency`. `market/`'s two leaves (`order`/`orderbook`) both
  resolve to `market/router.json`'s own declared `default` core; this is the identical
  fixture `test_router_aggregate_multiply_inherits_alphabetically` already exercises,
  asserted again here to name the exact property under test."""
  generator = _generator()
  generator.router_context = ('', ('market',))
  children = {
    'order': {
      'import_path': '.order', 'class_name': 'Order', 'attr_name': 'order',
      'kind': 'endpoint', 'transport': 'http',
    },
    'orderbook': {
      'import_path': '.orderbook', 'class_name': 'Orderbook', 'attr_name': 'orderbook',
      'kind': 'endpoint', 'transport': 'http',
    },
  }
  code = generator.router('market', children)
  assert 'class Market(Order, Orderbook):' in code


def test_router_rejects_directory_that_is_both_a_leaf_and_a_router():
  """A directory holding both a leaf of its own and a further subdirectory (`mixed_dir/`,
  bit2me's real shape) is refused, not composed (`docs/spec/authoring.md` rule 16,
  `docs/production_standards.md` S30, design §4). Task 24a originally composed this shape
  onto one class (the leaf inherited directly as a base, the `nested/` subdirectory as a
  `@cached_property`) via a synthetic self-child -- but review found the leaf could only
  ever render as `__call__` (S29-forbidden), since a self-referential node's own parent can
  never qualify as an `aggregate_nodes` parent. Task 24c retires that composition mechanism
  in favor of this refusal."""
  generator = _generator()
  generator.router_context = ('', ('mixed_dir',))
  children = {
    'mixed_dir': {
      'import_path': '.mixed_dir', 'class_name': 'MixedDirEndpoint', 'attr_name': 'mixed_dir',
      'kind': 'endpoint', 'transport': 'http',
    },
    'nested': {
      'import_path': '.nested', 'class_name': 'Nested', 'attr_name': 'nested',
      'kind': 'router', 'transport': 'http', 'mixin': 'AuthRouter', 'doc': None,
    },
  }
  with pytest.raises(ValueError, match='mixed_dir'):
    generator.router('mixed_dir', children)


def test_router_rejects_sibling_identifier_collision():
  """Two sibling leaves whose directory names sanitize to the same generated identifier
  (`orderStatus`/`order_status` both collapse through `Generator.identifier` to
  `order_status`) is a loud error, not a silent, base-order-dependent MRO surprise
  (design §3's own stated consequence of alphabetical base ordering) -- confirmed here
  since no other check in this repo catches it (`typed-dev spec test` has no rule for
  sibling-directory-name collisions within one grouping)."""
  generator = _generator()
  generator.router_context = ('', ('market',))
  children = {
    'orderStatus': {
      'import_path': '.order_status', 'class_name': 'OrderStatusA', 'attr_name': 'orderStatus',
      'kind': 'endpoint', 'transport': 'http',
    },
    'order_status': {
      'import_path': '.order_status_legacy', 'class_name': 'OrderStatusB',
      'attr_name': 'order_status', 'kind': 'endpoint', 'transport': 'http',
    },
  }
  with pytest.raises(ValueError, match='order_status'):
    generator.router('market', children)


def test_router_rejects_leaf_subdirectory_identifier_collision():
  """A leaf's own generated identifier colliding with a subdirectory's `cached_property`
  attribute name is the same class of silent bug as two colliding leaves (above), just
  one dict-body-definition-shadowing instead of one MRO-shadowing -- covered by the same
  check, not a leaves-only special case."""
  generator = _generator()
  generator.router_context = ('', ('mixed_dir',))
  children = {
    'orders': {
      'import_path': '.orders', 'class_name': 'OrdersEndpoint', 'attr_name': 'orders',
      'kind': 'endpoint', 'transport': 'http',
    },
    'orders_group': {
      'import_path': '.orders_group', 'class_name': 'OrdersGroup', 'attr_name': 'orders',
      'kind': 'router', 'transport': 'http', 'mixin': 'AuthRouter', 'doc': None,
    },
  }
  with pytest.raises(ValueError, match='orders'):
    generator.router('mixed_dir', children)


def _root_children() -> dict:
  """The fixture's four top-level directories, in the shape the CLI builds them (design
  §5c: the root is rendered by `router()` itself now, no separate `_root_class` method)."""
  return {
    name: {
      'import_path': f'.{name}', 'class_name': class_name, 'attr_name': name,
      'kind': 'router', 'transport': 'mixed', 'doc': None,
    }
    for name, class_name in [
      ('market', 'Market'), ('account', 'Account'),
      ('futures', 'Futures'), ('mixed_dir', 'MixedDir'),
    ]
  }


def test_router_at_root_subclasses_client_base_only():
  """`router()`, called for the client root position itself (`router_context = ('', ())`,
  matching `endpoints_root` exactly), composes the fixture's four top-level directories
  through the identical directory-driven composite rule any other node uses (each is a
  `@cached_property`, since none is a bare leaf), subclassing whatever its own
  `spec/endpoints/router.json` declares (`"root"` -> `fixture_client.core:ClientBase`) --
  and takes its class name from `codegen/config.toml`'s `config.python.name`
  (`"FixtureClient"`), not the derived `router_class_name('')`. This replaces the retired
  `_root_class` (Task 19/design §4) -- design §5c retires the root/ordinary-node
  distinction it existed for."""
  generator = _generator()
  generator.router_context = ('', ())
  code = generator.router('', _root_children())
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'class FixtureClient(ClientBase):' in code
  assert 'from fixture_client.core import ClientBase' in code
  class_body = code.split('class FixtureClient(ClientBase):', 1)[1]
  assert 'RpcEndpoint' not in class_body  # never also a leaf-shaped core
  assert 'FuturesEndpoint' not in class_body
  assert '@cached_property' in code

  for attr, child_class in [
    ('market', 'Market'), ('account', 'Account'),
    ('futures', 'Futures'), ('mixed_dir', 'MixedDir'),
  ]:
    assert f'def {attr}(self) -> {child_class}:' in code
    assert f'return {child_class}(client=self.client)' in code
    assert f'from .{attr} import {child_class}' in code


def test_router_rejects_root_level_bare_leaf():
  """A leaf sitting directly under the client root can't be composed onto the generated
  root class the way an ordinary aggregate/mixed router would inherit one:
  `router()`'s own aggregate/mixed branch never resolves a base at all when leaves are
  present, so a root-level leaf would silently multiply-inherit *only* that leaf's own
  resolved core, losing the root's own declared `ClientBase` entirely rather than
  smuggling it in as a second base. Refused loudly, not silently supported -- a client
  whose spec puts a leaf directly under the root is spec debt to restructure under a
  real grouping directory."""
  generator = _generator()
  generator.router_context = ('', ())
  children = {
    **_root_children(),
    'ping': {
      'import_path': '.ping', 'class_name': 'Ping', 'attr_name': 'ping',
      'kind': 'endpoint', 'transport': 'http',
    },
  }
  with pytest.raises(ValueError, match='ping'):
    generator.router('', children)


def test_router_at_root_rejects_sibling_identifier_collision():
  """Two top-level directories whose names normalize to the same generated identifier
  (`subAccount`/`sub_account` both collapse through `Generator.identifier` to
  `sub_account`, the same pair `test_router_rejects_sibling_identifier_collision` uses)
  must be a loud error at the root too -- `router()` composes the root's top-level
  children through the identical directory-driven rule it uses at any other position, so
  it needs the identical guard, not a silently-shadowed `@cached_property`."""
  generator = _generator()
  generator.router_context = ('', ())
  children = {
    'subAccount': {
      'import_path': '.sub_account', 'class_name': 'SubAccountA', 'attr_name': 'subAccount',
      'kind': 'router', 'transport': 'http', 'doc': None,
    },
    'sub_account': {
      'import_path': '.sub_account_legacy', 'class_name': 'SubAccountB',
      'attr_name': 'sub_account', 'kind': 'router', 'transport': 'http', 'doc': None,
    },
  }
  with pytest.raises(ValueError, match='sub_account'):
    generator.router('', children)


def test_router_at_root_composes_heterogeneous_children(tmp_path: Path):
  """Design §5c's own `children` mapping, declared on the *root's own* resolved core --
  a fixture analog of kraken's real, shipped `main.py` (`Kraken`: `spot: Spot`,
  `streams: Streams`, `trading_ws: TradingWs`, one level *above* the whole endpoint
  tree). §5c's own worked example (`SpotBase` composing `rest`/`streams`) is one level
  *down* from a client's root; this proves the identical mechanism holds *at* the root
  too, through `router()`'s own uniform resolution -- not a special case bolted on
  beside it.

  Built as its own small, self-contained synthetic client (not an extension of
  `codegen_fixture_client`) specifically so it doesn't disturb that fixture's own root,
  which is deliberately kept in its simpler, non-heterogeneous shape -- proving the
  common, no-`children` default path stays byte-identical is just as important a
  regression bar as proving the new mechanism works, and the two are easiest to keep
  honest as separate fixtures.
  """
  package_root = tmp_path / 'pkg' / 'src' / 'hetero_fixture'
  package_root.mkdir(parents=True)
  (package_root / '__init__.py').write_text('')
  (package_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True)\n'
    'class HeteroBase:\n'
    '  """Root base composing three heterogeneous, already-built children."""\n'
    '  spot_client: object\n'
    '  streams_client: object\n'
    '  trading_ws_client: object\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  """Ordinary resolved core -- no `.new()`, the degenerate default case."""\n'
    '  client: object\n'
  )

  endpoints_root = tmp_path / 'spec' / 'endpoints'
  (endpoints_root).mkdir(parents=True)
  (endpoints_root / 'router.json').write_text(json.dumps({
    'description': 'Kraken-shaped fixture root.', 'upstream': 'https://example.com/docs',
    'core': 'root',
  }))
  for name in ('spot', 'streams', 'trading_ws'):
    directory = endpoints_root / name
    directory.mkdir()
    (directory / 'router.json').write_text(json.dumps({
      'description': f'{name} endpoints.', 'upstream': 'https://example.com/docs',
      'core': 'default',
    }))

  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'src = "pkg/src"\n'
    'name = "Kraken"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "hetero_fixture.core:HeteroBase"\n'
    'children = { spot = "spot_client", streams = "streams_client", '
    'trading_ws = "trading_ws_client" }\n'
    '\n'
    '[python.cores.default]\n'
    'base = "hetero_fixture.core:RpcEndpoint"\n'
  )

  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.codegen_config = load_codegen_toml(tmp_path)
  generator.router_context = ('', ())
  children = {
    name: {
      'import_path': f'.{name}', 'class_name': class_name, 'attr_name': name,
      'kind': 'router', 'transport': 'mixed', 'doc': None,
      'spec_dir': endpoints_root / name,
    }
    for name, class_name in [
      ('spot', 'Spot'), ('streams', 'Streams'), ('trading_ws', 'TradingWs'),
    ]
  }
  code = generator.router('', children)
  ast.parse(code)

  assert 'class Kraken(HeteroBase):' in code
  assert 'from hetero_fixture.core import HeteroBase' in code
  assert 'return Spot(client=self.spot_client)' in code
  assert 'return Streams(client=self.streams_client)' in code
  assert 'return TradingWs(client=self.trading_ws_client)' in code


def test_router_composes_heterogeneous_children_below_root(tmp_path: Path):
  """Design §5c's own worked example, ported into a real fixture: a base *one level
  under* a client's root composing two distinctly-based children -- mexc's real,
  confirmed shape (`Spot` composing `SpotRest`/`SpotStreams`, HTTP vs WS, distinct
  cores). Validated *before* `test_router_at_root_composes_heterogeneous_children`
  (the simpler placement, per the task brief) -- `spot/`'s own resolved base
  (`SpotBase`) is reached via the identical nearest-ancestor `router.json` walk as any
  other position, proving the mechanism itself before also proving it holds at the root.
  """
  package_root = tmp_path / 'pkg' / 'src' / 'spot_fixture'
  package_root.mkdir(parents=True)
  (package_root / '__init__.py').write_text('')
  (package_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class SpotBase:\n'
    '  """Composes two distinctly-based, already-built children (design §5c)."""\n'
    '  rest_client: object\n'
    '  streams_client: object\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class SpotRestBase:\n'
    '  """Ordinary resolved core -- no `.new()`, the degenerate default case."""\n'
    '  client: object\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class SpotStreamsBase:\n'
    '  """Ordinary resolved core -- no `.new()`, the degenerate default case."""\n'
    '  client: object\n'
  )

  endpoints_root = tmp_path / 'spec' / 'endpoints'
  spot_dir = endpoints_root / 'spot'
  spot_dir.mkdir(parents=True)
  (endpoints_root / 'router.json').write_text(json.dumps({
    'description': 'Spot fixture root.', 'upstream': 'https://example.com/docs',
    'core': 'root',
  }))
  (spot_dir / 'router.json').write_text(json.dumps({
    'description': 'Spot endpoints.', 'upstream': 'https://example.com/docs/spot',
    'core': 'spot',
  }))
  for name in ('rest', 'streams'):
    directory = spot_dir / name
    directory.mkdir()
    (directory / 'router.json').write_text(json.dumps({
      'description': f'Spot {name} endpoints.', 'upstream': 'https://example.com/docs',
      'core': f'spot_{name}',
    }))

  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'src = "pkg/src"\n'
    'name = "SpotFixture"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "spot_fixture.core:SpotBase"\n'
    '\n'
    '[python.cores.spot]\n'
    'base = "spot_fixture.core:SpotBase"\n'
    'children = { rest = "rest_client", streams = "streams_client" }\n'
    '\n'
    '[python.cores.spot_rest]\n'
    'base = "spot_fixture.core:SpotRestBase"\n'
    '\n'
    '[python.cores.spot_streams]\n'
    'base = "spot_fixture.core:SpotStreamsBase"\n'
  )

  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.codegen_config = load_codegen_toml(tmp_path)
  generator.router_context = ('', ('spot',))
  children = {
    name: {
      'import_path': f'.{name}', 'class_name': class_name, 'attr_name': name,
      'kind': 'router', 'transport': 'mixed', 'doc': None,
      'spec_dir': spot_dir / name,
    }
    for name, class_name in [('rest', 'SpotRest'), ('streams', 'SpotStreams')]
  }
  code = generator.router('spot', children)
  ast.parse(code)

  assert 'class Spot(SpotBase):' in code
  assert 'from spot_fixture.core import SpotBase' in code
  assert 'return SpotRest(client=self.rest_client)' in code
  assert 'return SpotStreams(client=self.streams_client)' in code


def _parameterized_subtree_fixture(tmp_path: Path) -> tuple[dict, Path]:
  """Shared setup for the two tests below: a design §5a parameterized subtree
  (`token/`, forwarding `network`) with a `balances/breakdown/` grouping beneath it --
  `codegen/config.toml` and the raw `.new()`-carrying core `ChainRpc` both live here since neither
  test needs its own copy."""
  package_root = tmp_path / 'pkg' / 'src' / 'mixed_fixture'
  package_root.mkdir(parents=True)
  (package_root / '__init__.py').write_text('')
  (package_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    'from typing_extensions import Literal, Self\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: object\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class ChainRpc(RpcEndpoint):\n'
    '  network: Literal["ethereum", "polygon"]\n'
    '\n'
    '  @classmethod\n'
    '  def new(cls, client, *, network: Literal["ethereum", "polygon"]) -> Self:\n'
    '    return cls(client=client, network=network)\n'
  )

  endpoints_root = tmp_path / 'spec' / 'endpoints'
  token_dir = endpoints_root / 'token'
  balances_dir = token_dir / 'balances'
  breakdown_dir = balances_dir / 'breakdown'
  breakdown_dir.mkdir(parents=True)
  (endpoints_root / 'router.json').write_text(json.dumps({
    'description': 'Root.', 'upstream': 'https://example.com/docs', 'core': 'root',
  }))
  (token_dir / 'router.json').write_text(json.dumps({
    'description': 'Token endpoints.', 'upstream': 'https://example.com/docs', 'core': 'chain',
  }))
  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'src = "pkg/src"\n'
    'name = "MixedFixture"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "mixed_fixture.core:RpcEndpoint"\n'
    '\n'
    '[python.cores.chain]\n'
    'base = "mixed_fixture.core:ChainRpc"\n'
  )

  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.codegen_config = load_codegen_toml(tmp_path)
  generator.router_context = ('', ('token', 'balances'))
  breakdown_child = {
    'import_path': '.breakdown', 'class_name': 'Breakdown', 'attr_name': 'breakdown',
    'kind': 'router', 'transport': 'mixed', 'doc': None,
    'spec_dir': breakdown_dir,
  }
  return {'generator': generator, 'breakdown': breakdown_child}, breakdown_dir


def test_directory_that_is_both_a_leaf_and_a_router_is_rejected_under_parameterized_subtree(
  tmp_path: Path,
):
  """A directory that is itself also a leaf (design §3/§4: a leaf of its own *and* a
  further subdirectory) is refused regardless of where in the tree it sits -- including
  directly under a design §5a parameterized subtree (`token/` here, forwarding `network`).

  This used to be `test_mixed_node_under_parameterized_subtree_forwards_inherited_field`
  (Task 24a review finding 2): before Task 24c, this exact shape was composed onto one
  class, and `own_core`/`parent_fields` resolution needed a fix so `breakdown()` correctly
  forwarded `self.network` instead of re-exposing it as a new caller parameter. Task 24c
  retires the whole composition path in favor of an outright refusal (rule 16/S30), so this
  shape's own coverage flips to asserting the raise instead -- the underlying `own_core`/
  `parent_fields` forwarding mechanism itself stays covered by the sibling (non-self-
  referential) test right below, since that mechanism is still reachable and still real for
  an ordinary leaf-and-router sibling composite.
  """
  fixture, _ = _parameterized_subtree_fixture(tmp_path)
  children = {
    'balances': {  # this node's own self-leaf -- the shape rule 16/S30 refuses
      'import_path': '.balances', 'class_name': 'BalancesEndpoint', 'attr_name': 'balances',
      'kind': 'endpoint', 'transport': 'http',
    },
    'breakdown': fixture['breakdown'],
  }
  with pytest.raises(ValueError, match='balances'):
    fixture['generator'].router('balances', children)


def test_leaf_and_router_siblings_forward_inherited_field_under_parameterized_subtree(
  tmp_path: Path,
):
  """The non-self-referential twin of the test above: a genuine sibling leaf (`get/`, a
  real subdirectory distinct from `balances/` itself) alongside a sibling router
  (`breakdown/`) under a design §5a parameterized subtree must forward the field it
  already carries transitively through its own leaf base, not re-expose it as a new caller
  parameter that can disagree with `self`'s own value -- the same `own_core`/
  `parent_fields` forwarding bug Task 24a's review finding 2 fixed, still real and still
  reachable for this ordinary sibling-mix shape after Task 24c refuses only the
  self-referential one.
  """
  fixture, breakdown_dir = _parameterized_subtree_fixture(tmp_path)
  get_dir = breakdown_dir.parent / 'get'
  get_dir.mkdir()
  children = {
    'get': {
      'import_path': '.get', 'class_name': 'Get', 'attr_name': 'get',
      'kind': 'endpoint', 'transport': 'http',
    },
    'breakdown': fixture['breakdown'],
  }
  code = fixture['generator'].router('balances', children)
  ast.parse(code)

  # The bug: without the fix, this reads `def breakdown(self, *, network) -> Breakdown:`
  # -- `network` wrongly re-exposed instead of forwarded from `self.network`.
  assert 'def breakdown(self) -> Breakdown:' in code
  assert '@cached_property' in code
  assert 'return Breakdown.new(self.client, network=self.network)' in code
  assert 'network=network' not in code  # would only appear if re-exposed as a new param


def test_new_param_type_renders_a_named_type_alias_type_not_an_inlined_literal(
  tmp_path: Path,
):
  """`_new_param_type`'s `TypeAliasType` branch (the fix for alchemy's real `main.py`
  bug: five generated factory methods -- `nft`/`token`/`transfers`/`utility`/
  `simulation` -- each re-inlining the identical 9-value `network: Literal[...]`,
  because a *plain* `Network = Literal[...]` assignment is erased by `get_type_hints`,
  which hands back the raw expanded `Literal[...]` with no way to recover the alias name
  at all). A resolved core's `.new()` parameter typed through
  `TypeAliasType('Network', Literal[...])` (`typing_extensions`, constructed directly --
  not the 3.12-only `type Network = ...` statement, since this fleet's shared template
  pins `requires-python = '>=3.10'`) is the one spelling that survives that erasure: the
  hint itself comes back as the `TypeAliasType` instance, not its expansion, so it
  renders as the bare alias name `Network`, imported once -- never as a re-expanded
  inline `Literal[...]`.

  Mirrors `_parameterized_subtree_fixture` above (also design §5a's own worked example,
  ChainRpc/`network`), but renders the client-root factory method directly -- alchemy's
  real shape (`Alchemy.nft(self, *, network: Network | None = None) -> Nft`) is a
  *root-level* child, not a nested one, and the plain-vs-`TypeAliasType` distinction only
  shows up in what `main.py` actually renders for that parameter's type expression.
  """
  package_root = tmp_path / 'pkg' / 'src' / 'alias_fixture'
  package_root.mkdir(parents=True)
  (package_root / '__init__.py').write_text('')
  (package_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    'from typing_extensions import Literal, Self, TypeAliasType\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: object\n'
    '\n'
    '\n'
    "Network = TypeAliasType('Network', Literal['ethereum', 'polygon'])\n"
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class ChainRpc:\n'
    '  client: object\n'
    '  network: Network | None\n'
    '\n'
    '  @classmethod\n'
    '  def new(cls, client, *, network: Network | None = None) -> Self:\n'
    '    return cls(client=client, network=network)\n'
  )

  endpoints_root = tmp_path / 'spec' / 'endpoints'
  token_dir = endpoints_root / 'token'
  token_dir.mkdir(parents=True)
  (endpoints_root / 'router.json').write_text(json.dumps({
    'description': 'Root.', 'upstream': 'https://example.com/docs', 'core': 'root',
  }))
  (token_dir / 'router.json').write_text(json.dumps({
    'description': 'Token endpoints.', 'upstream': 'https://example.com/docs', 'core': 'chain',
  }))
  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'src = "pkg/src"\n'
    'name = "AliasFixture"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "alias_fixture.core:RpcEndpoint"\n'
    '\n'
    '[python.cores.chain]\n'
    'base = "alias_fixture.core:ChainRpc"\n'
  )

  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.codegen_config = load_codegen_toml(tmp_path)
  generator.router_context = ('', ())
  token_child = {
    'import_path': '.token', 'class_name': 'Token', 'attr_name': 'token',
    'kind': 'router', 'transport': 'mixed', 'doc': None,
    'spec_dir': token_dir,
  }
  code = generator.router('', {'token': token_child})
  ast.parse(code)

  assert 'def token(self, *, network: Network | None = None) -> Token:' in code
  assert 'from alias_fixture.core import Network' in code
  # the plain-assignment fallback (pre-fix behavior) would have re-inlined this instead
  assert 'Literal[' not in code


def test_import_core_class_raises_a_diagnosable_bootstrap_error(tmp_path: Path):
  """Review finding 3: codegen imports the client package it is currently generating
  (design §5a's own `.new()` introspection genuinely needs the resolved core's live
  signature) -- so a package whose `__init__.py` depends on a file codegen hasn't
  written yet this run (`from .main import X`, the hand-curated export surface's own
  dependency on the generated root module, is the real, common shape) fails to import.
  Before this fix, that surfaced as a bare `ModuleNotFoundError` naming nothing about the
  real cause. Now it's wrapped into a `ValueError` naming the failing module and
  explaining the bootstrap-ordering cause and the fix (seed a placeholder)."""
  package_root = tmp_path / 'pkg' / 'src' / 'bootstrap_fixture'
  package_root.mkdir(parents=True)
  # `__init__.py` depends on a `main.py` that does not exist -- exactly the real shape a
  # hand-curated export surface has (design §4: `from .main import <RootClass>`).
  (package_root / '__init__.py').write_text('from .main import RootClient\n')
  (package_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: object\n'
  )

  generator = Generator()
  generator.project = resolve(tmp_path)
  with pytest.raises(ValueError) as excinfo:
    generator._import_core_class('bootstrap_fixture.core:RpcEndpoint')

  message = str(excinfo.value)
  assert 'bootstrap_fixture.core' in message
  assert 'first' in message.lower()  # names the real, diagnosable cause
  assert 'placeholder' in message.lower()  # names the actionable fix


def test_import_core_class_does_not_leave_pkg_src_on_sys_path(tmp_path: Path):
  """Review finding 3: `_import_core_class` can run many times across one client's
  generation (once per router node needing design §5a's own introspection), so it must
  not leave its own `pkg/src` insertion on `sys.path` for the rest of the process --
  a latent cross-client shadowing hazard for any caller generating more than one client
  in one process (the real CLI, run repeatedly in-process by a test, or a future
  multi-client command). Verified on both the success and the failure path."""
  package_root = tmp_path / 'pkg' / 'src' / 'cleanup_fixture'
  package_root.mkdir(parents=True)
  (package_root / '__init__.py').write_text('')
  (package_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: object\n'
  )
  pkg_src = str(tmp_path / 'pkg' / 'src')
  assert pkg_src not in sys.path
  (tmp_path / 'truewire.toml').write_text(
    '[python]\nsrc = "pkg/src"\npackage = "cleanup_fixture"\n'
    '[python.cores.default]\nbase = "cleanup_fixture.core:RpcEndpoint"\n'
  )

  generator = Generator()
  generator.project = resolve(tmp_path)
  cls = generator._import_core_class('cleanup_fixture.core:RpcEndpoint')
  assert cls.__name__ == 'RpcEndpoint'
  assert pkg_src not in sys.path  # success path: cleaned up

  with pytest.raises(ValueError):
    generator._import_core_class('cleanup_fixture.core:NoSuchClass')
  assert pkg_src not in sys.path  # failure path: still cleaned up


def test_rpc_endpoint_flat_validate_property_renamed_to_avoid_collision():
  """A flat `request` property literally named `validate` must not collide with the
  generated method's own reserved `validate: bool | None = None` kwarg -- kraken's real,
  motivating case (`spot.trading.edit_order`/`add_order_batch`, both transports: each
  declares a genuine wire field named `validate`, optional, flat at the top level of the
  request body). Before `_avoid_reserved_collision` existed, this produced a `Function`
  with two same-named `Param`s, rendering `def f(self, *, validate: bool = ...,
  validate: bool | None = None)` -- a real `SyntaxError` at import time, not just an
  awkward signature.

  Reuses `market/order`'s fixture (a signed POST, flat `request`), with an extra optional
  `validate` boolean property mixed in, mirroring the exact shape `edit_order` declares."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request']['properties']['validate'] = {
    'type': 'boolean',
    'description': 'If true, only validate the order; never submit it.',
  }
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceOrder', method_name='place_order', endpoint_dir=root,
  )
  tree = ast.parse(code)  # must be valid Python -- the duplicate-kwarg bug is a SyntaxError

  # Exactly one `validate` kwarg (the reserved response-validation override) and one
  # `validate_` kwarg (the renamed wire field) on the generated method -- never two
  # `validate`s.
  [method] = [
    node for node in ast.walk(tree)
    if isinstance(node, ast.AsyncFunctionDef) and node.name == 'place_order'
  ]
  kwonly_names = [arg.arg for arg in method.args.kwonlyargs]
  assert kwonly_names.count('validate') == 1
  assert 'validate_' in kwonly_names

  # The wire key inside the built `Request(...)` stays the real spec name (`validate`),
  # only the local Python identifier bound to it is renamed -- `Request.validate` (the
  # TypedDict field) is untouched, matching what the venue actually calls it on the wire.
  assert "request['validate'] = validate_" in code or 'validate=validate_' in code
  assert 'class Request(TypedDict):' in code
  assert 'validate: NotRequired[bool]' in code or 'validate:' in code


def test_skip_endpoint_default_respects_handwritten_surface():
  """The base `Generator.skip_endpoint` (no per-client subclass exists for a fully
  migrated client -- `truewire.codegen.layout.load_generator`'s `codegen/config.toml`-only
  fallback constructs a bare `Generator()`) must still let a `surface: {"kind":
  "handwritten"}` endpoint opt out of regeneration, the same way every legacy per-client
  backend's own hand-rolled `skip_endpoint` override already does. Without this default,
  `cli/codegen.py` would silently overwrite a hand-written module (kraken's
  `spot.account.retrieve_export`, whose raw-binary response the mechanized `rpc_endpoint`
  shape cannot express as a typed return) on every regen.

  Confirmed to not change behavior for any of the three real, currently-unmigrated
  clients with a real `handwritten`-surface endpoint (bitget/kucoin/mexc) -- each already
  defines its own `skip_endpoint` override in its own `codegen/python.py` subclass, which
  Python's MRO always prefers over this base default; this test only exercises the bare,
  subclass-free path a migrated client actually uses."""
  generator = Generator()
  handwritten = Endpoint.model_validate({
    'meta': {},
    'surface': {
      'kind': 'handwritten', 'symbol': 'pkg.leaf:Leaf', 'reason': 'raw binary response',
    },
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
      'response': {'title': 'Raw', 'type': 'object', 'properties': {}},
    },
  })
  assert generator.skip_endpoint(handwritten) is True

  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  ordinary = load_endpoint(root / 'endpoint.json')
  assert generator.skip_endpoint(ordinary) is False


def test_rpc_endpoint_flat_property_anyof_of_array_of_record():
  """A flat `request` property that is itself `anyOf` with a variant that is an array
  of a titled record -- nested one level past what `_rpc_request_params`'s own two
  direct-lookup shapes (the property *is* a record, or *is* an array of one) cover.
  kraken's real, motivating case: `spot.account.trade_volume`'s `pair` (`anyOf:
  [string, array of TradeVolumePairClass]`). Before `_flat_property_type` existed, this
  crashed generation entirely (`ValueError: Found nested record type`), since
  `renderer.parser` was called directly on the raw, un-normalized property schema."""
  # No explicit `null` variant -- exactly kraken's real shape (optional via `required`
  # omission alone). A flat property whose `anyOf` *itself* includes a `null` variant is
  # a separate, unconfirmed-against-any-real-spec case (`Function.Param`'s own
  # required=False rendering may double up the `| None` `_flat_property_type` already
  # adds for the nullable variant) -- not exercised here, out of scope for this fix.
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request']['properties']['pair'] = {
    'description': 'Asset pair(s).',
    'anyOf': [
      {'type': 'string'},
      {
        'type': 'array',
        'items': {
          'title': 'TradeVolumePairClass',
          'type': 'object',
          'required': ['asset', 'aclass'],
          'properties': {
            'asset': {'type': 'string'},
            'aclass': {'type': 'string', 'enum': ['currency', 'forex']},
          },
        },
      },
    ],
  }
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceOrder', method_name='place_order', endpoint_dir=root,
  )
  ast.parse(code)  # must generate valid Python, not crash

  assert 'class TradeVolumePairClass(TypedDict):' in code
  assert 'pair: str | list[TradeVolumePairClass] | None = None' in code


def test_rpc_endpoint_flat_property_array_of_anyof_of_record():
  """A flat `request` property that is a plain array whose *items* are themselves a
  titled-`anyOf` union of records -- the opposite nesting order from the test above
  (`array` wrapping `anyOf`, rather than `anyOf` wrapping `array`), registered under a
  different `Unnest` id shape (`{base_id}/item/anyOf/{index}`, `item` outermost).
  kraken's real, motivating case: `spot.trading.add_order_batch`/`trading_ws.batch_add`'s
  `orders` (a list of order-type variant records)."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request']['properties']['orders'] = {
    'type': 'array',
    'items': {
      'anyOf': [
        {
          'title': 'MarketOrderEntry', 'type': 'object',
          'properties': {'kind': {'type': 'string', 'enum': ['market']}},
        },
        {
          'title': 'LimitOrderEntry', 'type': 'object',
          'properties': {'price': {'type': 'string'}},
        },
      ],
    },
  }
  raw['spec']['request']['required'] = ['symbol', 'quantity', 'orders']
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceOrder', method_name='place_order', endpoint_dir=root,
  )
  ast.parse(code)

  assert 'class MarketOrderEntry(TypedDict):' in code
  assert 'class LimitOrderEntry(TypedDict):' in code
  assert 'orders: list[MarketOrderEntry | LimitOrderEntry]' in code


def test_rpc_endpoint_required_field_with_keyword_wire_name():
  """A required flat `request` property whose wire name is a Python keyword (`from`) --
  kraken's real, motivating case: `spot.account.account_transfer`/
  `spot.funding.wallet_transfer`, both declaring a genuine, required `from` field.

  `Request(from=from_)` is a flat `SyntaxError` regardless of how the *local* Python
  identifier gets renamed (`self.identifier`'s own keyword-suffixing only fixes the
  parameter name, not the keyword-argument name at the call site) -- and the next
  obvious fix, `Request(**{'from': from_}, other=other)`, is valid syntax but a real
  pyright false positive: mixing `**mapping` unpacking with ordinary keyword arguments
  in one `TypedDict(...)` call makes pyright check the whole unpacked mapping's
  inferred value type against every *other* field's own declared type too, even ones
  the mapping never touches -- confirmed by hand against a real Literal-typed sibling
  field, which pyright falsely flagged only once the `**{'from': ...}` entry was mixed
  in. `_typed_dict_constructor` sidesteps both by rendering a plain dict literal,
  checked against an explicit `: Request` annotation, whenever any required field's
  wire name isn't a valid keyword-argument name."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request']['properties']['from'] = {'type': 'string', 'description': 'Source.'}
  raw['spec']['request']['required'] = ['symbol', 'quantity', 'from']
  endpoint = Endpoint.model_validate(raw)
  generator = _generator()
  code = generator.rpc_endpoint(
    endpoint, {}, class_name='PlaceOrder', method_name='place_order', endpoint_dir=root,
  )
  tree = ast.parse(code)  # must be valid Python, not a SyntaxError

  # The generated parameter is renamed (`from_`), never the literal keyword `from`.
  [method] = [
    node for node in ast.walk(tree)
    if isinstance(node, ast.AsyncFunctionDef) and node.name == 'place_order'
  ]
  all_arg_names = [a.arg for a in method.args.args + method.args.kwonlyargs]
  assert 'from_' in all_arg_names
  assert 'from' not in all_arg_names

  # Built via a dict literal with an explicit annotation, not a constructor call that
  # would need `from=` as a literal keyword argument.
  assert "request: Request = {'symbol': symbol, 'quantity': quantity, 'from': from_}" in code
  # No `Request(...)` constructor *call* anywhere (only the class declaration itself,
  # `class Request(RequestKeywords):`, legitimately contains the substring `Request(`).
  assert 'Request(symbol' not in code and 'Request(quantity' not in code


def test_paged_size_default_reads_the_new_request_shape():
  """`paged_size_default` only ever read `endpoint.openapi.parameters` -- `None` for
  every migrated (design §7) endpoint, since `endpoint.openapi` is `None` once `request`/
  `response` replace it. kraken's real `spot.account.trades_history` is the motivating
  case: an `offset` walk terminated by an item-counted `total` genuinely needs this
  default to compute its own step (`paged_step` raises `ValueError` without one) -- the
  spec correctly declares `limit`'s own `default: 50`, but the migrated-client path
  never looked at `request.properties.limit.default` at all, only the legacy shape.

  Confirms both the new-shape path (this test) and that the legacy path is untouched
  (a plain `Endpoint.openapi`-shaped fixture keeps working, covered by every other
  already-passing test that exercises pagination against an unmigrated client)."""
  endpoint = Endpoint.model_validate({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
      'request': {
        'title': 'Req', 'type': 'object',
        'properties': {
          'ofs': {'type': 'integer'},
          'limit': {'type': 'integer', 'default': 50},
        },
      },
      'response': {'title': 'Resp', 'type': 'object', 'properties': {}},
    },
    'pagination': {
      'strategy': 'offset',
      'offset': {'parameter': 'ofs'},
      'size': {'parameter': 'limit'},
      'done': {'kind': 'total', 'path': 'count', 'counts': 'items'},
    },
  })
  generator = _generator()
  assert generator.paged_size_default(endpoint) == 50
