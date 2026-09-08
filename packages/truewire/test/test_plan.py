"""The plan (`truewire.plan`): computed once from a project, read by every backend.

Three things are proved here. The builder names types and decides request shapes, response
nullability and pagination the way the Python backend renders them, on the fixture client
and on both example projects. The Python backend reads those decisions off the plan rather
than re-deriving them (a plan edited by hand changes the generated code). And `truewire
plan` prints the same plan, as a summary or as JSON, with the GitHub example's JSON pinned
as a snapshot.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.python import Generator
from truewire.plan.build import PlanBuilder, build_plan, needs_cast
from truewire.plan.model import PackagePlan
from truewire.project import resolve
from truewire.spec import Endpoint, load_endpoint
from truewire.spec.codegen_toml import load_codegen_toml

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'codegen_fixture_client'
SNAPSHOT = Path(__file__).parent / 'fixtures' / 'plans' / 'github.json'
"""`truewire plan --project examples/github --json > test/fixtures/plans/github.json`
regenerates it after a deliberate change to the plan's shape or to the example."""
EXAMPLES = Path(__file__).parents[3] / 'examples'


@pytest.fixture(scope='module')
def fixture_plan() -> PackagePlan:
  return build_plan(FIXTURE_ROOT)


def _example(name: str) -> Path:
  root = EXAMPLES / name
  if not (root / 'truewire.toml').is_file():
    pytest.skip(f'examples/{name} is not checked out beside the package')
  return root


def _generator() -> Generator:
  generator = Generator()
  generator.project = resolve(FIXTURE_ROOT)
  generator.codegen_config = load_codegen_toml(FIXTURE_ROOT)
  return generator


# -- the builder on the fixture client -----------------------------------------------


def test_fixture_plan_covers_routers_cores_and_scopes(fixture_plan: PackagePlan):
  """Every router node of the function tree, with the core its nearest `router.json`
  declares and its children by kind; every `schemas.json` scope under its own key; every
  core from both `[cores]` and `[python.cores]`."""
  by_path = {tuple(router.path): router for router in fixture_plan.routers}
  assert () in by_path and ('market',) in by_path and ('account', 'deposits') in by_path
  root = by_path[()]
  assert root.core == 'root'
  assert {child.name: child.kind for child in root.children} == {
    'account': 'router', 'futures': 'router', 'market': 'router', 'mixed_dir': 'router',
    'token': 'router',
  }
  assert by_path[('futures',)].core == 'futures'
  assert by_path[('mixed_dir',)].doc is not None
  market = {child.name: child for child in by_path[('market',)].children}
  assert market['order_list'].kind == 'endpoint' and market['order_list'].class_ == 'OrderList'

  assert set(fixture_plan.schemas) == {'', 'futures'}
  assert 'OrderSide' in fixture_plan.schemas['']
  assert 'FuturesSide' in fixture_plan.schemas['futures']

  assert set(fixture_plan.cores) == {'chain', 'default', 'futures', 'root'}
  assert fixture_plan.cores['default'].meta is not None
  chain = fixture_plan.cores['chain'].params
  assert chain is not None and chain['network'].type == 'fixture_client.token.core:Network'
  assert chain['network'].required is True
  assert fixture_plan.root_class == 'FixtureClient'


def test_fixture_plan_names_types_the_way_the_backend_does(fixture_plan: PackagePlan):
  """`market/orderbook`'s response is titled `Orderbook`, the name its endpoint class
  would take: the class yields (`OrderbookEndpoint`) and the type keeps its title, the
  same resolution the backend makes. The request is always `Request`."""
  endpoint = fixture_plan.endpoint('market.orderbook')
  assert endpoint is not None
  assert 'Orderbook' in endpoint.types and endpoint.types['Orderbook']['type'] == 'record'
  assert endpoint.request.type == 'Request' and endpoint.response.payload == 'Orderbook'
  market = {c.name: c for c in next(r for r in fixture_plan.routers if r.path == ['market']).children}
  assert market['orderbook'].class_ == 'OrderbookEndpoint'


def test_fixture_plan_request_shapes(fixture_plan: PackagePlan):
  """A flat object is `fields` with one typed entry per property; a titled `anyOf` is one
  `union`; a bare array is one `array`."""
  orderbook = fixture_plan.endpoint('market.orderbook')
  assert orderbook is not None and orderbook.request.shape == 'fields'
  assert [(f.wire, f.required, f.type) for f in orderbook.request.fields] == [
    ('symbol', True, {'type': 'scalar', 'base': 'string'}),
  ]
  submit = fixture_plan.endpoint('market.order_submit')
  assert submit is not None and submit.request.shape == 'union' and submit.request.type == 'Request'
  assert submit.types['Request']['type'] == 'union'
  batch = fixture_plan.endpoint('market.order_batch')
  assert batch is not None and batch.request.shape == 'fields'
  assert batch.response.payload == 'OrderSide' and batch.response.needs_cast is False

  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_batch'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['spec']['request'] = {
    'title': 'Orders', 'type': 'array', 'description': 'Orders to place.',
    'items': {'type': 'string', 'description': 'One order id.'},
  }
  bare = PlanBuilder(resolve(FIXTURE_ROOT)).endpoint(Endpoint.model_validate(raw), root)
  assert bare is not None and bare.request.shape == 'array' and bare.request.type == 'Request'
  assert bare.types['Request'] == {'type': 'list', 'item': {'type': 'scalar', 'base': 'string'}, 'id': 'Request'}


def test_fixture_plan_pagination_decisions(fixture_plan: PackagePlan):
  """Row and state types come off the type tree, including through a bare `$ref`
  response into a shared scope; nullability off the union; the walker from both."""
  order_list = fixture_plan.endpoint('market.order_list')
  assert order_list is not None and order_list.pagination is not None
  pagination = order_list.pagination
  assert (pagination.strategy, pagination.done['kind'], pagination.rows) == ('token', 'absent_cursor', 'orders')
  assert pagination.row_type == {'type': 'ref', 'id': 'OrderListItem'}
  assert pagination.state_type == {'type': 'scalar', 'base': 'string'}
  assert pagination.walker == 'paginated' and pagination.seedable and not pagination.driver_required

  nullable = fixture_plan.endpoint('market.order_nullable_list')
  assert nullable is not None and nullable.pagination is not None
  assert nullable.response.optional is True and nullable.response.payload == 'Response'
  assert nullable.pagination.row_type == {'type': 'ref', 'id': 'OrderNullableListItem'}
  assert nullable.pagination.walker == 'paginated'

  scalar_rows = fixture_plan.endpoint('market.order_id_list')
  assert scalar_rows is not None and scalar_rows.pagination is not None
  assert scalar_rows.pagination.row_type == {'type': 'scalar', 'base': 'string'}

  ledger = fixture_plan.endpoint('market.order_ledger')
  assert ledger is not None and ledger.pagination is not None
  assert ledger.response.payload == 'OrderLedgerResponse' and 'OrderLedgerResponse' not in ledger.types
  assert ledger.pagination.row_type == {'type': 'ref', 'id': 'OrderLedgerEntry'}
  assert ledger.pagination.walker == 'paginated'

  page_total = fixture_plan.endpoint('market.order_page_total')
  assert page_total is not None and page_total.pagination is not None
  assert page_total.pagination.state_type == {'type': 'scalar', 'base': 'integer'}
  assert page_total.pagination.size == 'size' and page_total.pagination.start == 1
  assert page_total.pagination.walker == 'paginated'


def test_offset_walk_with_nothing_to_step_by_plans_no_walker():
  """An `offset` walk ending on an item-counted `total` with no rows to count and no size
  cannot advance: the plan says `none`, which is what the backend emits (no `_paged`)."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order'
  raw = json.loads((root / 'endpoint.json').read_text())
  raw['pagination'] = {
    'strategy': 'offset', 'offset': {'parameter': 'symbol'},
    'done': {'kind': 'total', 'path': 'count', 'counts': 'items'},
  }
  endpoint = Endpoint.model_validate(raw)
  planned = PlanBuilder(resolve(FIXTURE_ROOT)).endpoint(endpoint, root)
  assert planned is not None and planned.pagination is not None
  assert planned.pagination.walker == 'none'
  assert planned.pagination.state_type == {'type': 'scalar', 'base': 'string'}


def test_fixture_plan_stream_shapes(fixture_plan: PackagePlan):
  """`market/ticker_stream` is a direct-channel stream: its one parameter is the
  channel's placeholder, so no `Parameters` type is planned and the payload keeps its
  title."""
  stream = fixture_plan.endpoint('market.ticker_stream')
  assert stream is not None and stream.kind == 'stream' and stream.stream is not None
  assert stream.wire.channel is not None and stream.wire.placeholders == ['symbol']
  assert stream.stream.direct_channel is True and stream.stream.connect_only is False
  assert stream.stream.channel_params == ['symbol']
  assert stream.request.shape == 'fields' and stream.request.type is None
  assert [f.wire for f in stream.request.fields] == ['symbol']
  assert stream.response.payload == 'Ticker' and 'Ticker' in stream.types


def test_needs_cast_is_decided_on_the_type_tree():
  """A bare `Literal`/`Any` alias needs a cast; a union led by one renders the same way; a
  record, a list, a reference or a nullable record does not."""
  literal = {'type': 'literal', 'values': ['a', 'b']}
  null = {'type': 'scalar', 'base': 'null'}
  assert needs_cast(literal)
  assert needs_cast({'type': 'scalar', 'base': 'any'})
  assert needs_cast({'type': 'union', 'variants': [{'type': literal}, {'type': null}]})
  assert not needs_cast({'type': 'union', 'variants': [{'type': {'type': 'ref', 'id': 'X'}}, {'type': null}]})
  assert not needs_cast({'type': 'record', 'id': 'R', 'fields': {}})
  assert not needs_cast({'type': 'list', 'item': literal})
  assert not needs_cast({'type': 'ref', 'id': 'X'})


def test_plan_json_round_trips(fixture_plan: PackagePlan):
  """`to_json` is the wire form of the same model: camelCase keys, `class` for the
  reserved word, nothing lost on the way back."""
  data = fixture_plan.to_json()
  assert 'rootClass' in data and 'class' in data['routers'][0]['children'][0]
  assert PackagePlan.model_validate(data) == fixture_plan


# -- the backend reads the plan ----------------------------------------------------------


def test_backend_reads_needs_cast_off_the_plan():
  """`rpc_endpoint` wraps `request_type` in `cast(type, ...)` when the plan says so, not
  by re-reading its own rendered definition: an attached plan edited to demand the cast
  changes the emitted call."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'orderbook'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  plain = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderbookEndpoint', method_name='orderbook', endpoint_dir=root,
  )
  assert 'request_type=Request,' in plain and 'cast(' not in plain

  package = build_plan(FIXTURE_ROOT)
  planned = package.endpoint('market.orderbook')
  assert planned is not None
  edited = planned.model_copy(update={'request': planned.request.model_copy(update={'needs_cast': True})})
  generator.plan = package.model_copy(update={
    'endpoints': [edited if e.function == 'market.orderbook' else e for e in package.endpoints],
  })
  cast = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderbookEndpoint', method_name='orderbook', endpoint_dir=root,
  )
  assert 'request_type=cast(type, Request)' in cast
  assert 'from typing_extensions import' in cast and 'cast' in cast.split('class OrderbookEndpoint')[0]


def test_backend_reads_response_optional_off_the_plan():
  """The walker guards its reads on the response only when the plan says the returned
  type is nullable."""
  root = FIXTURE_ROOT / 'spec' / 'endpoints' / 'market' / 'order_list'
  endpoint = load_endpoint(root / 'endpoint.json')
  generator = _generator()
  plain = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderList', method_name='order_list', endpoint_dir=root,
  )
  assert 'if response is not None else None' not in plain

  package = build_plan(FIXTURE_ROOT)
  planned = package.endpoint('market.order_list')
  assert planned is not None
  edited = planned.model_copy(update={'response': planned.response.model_copy(update={'optional': True})})
  generator.plan = package.model_copy(update={
    'endpoints': [edited if e.function == 'market.order_list' else e for e in package.endpoints],
  })
  guarded = generator.rpc_endpoint(
    endpoint, {}, class_name='OrderList', method_name='order_list', endpoint_dir=root,
  )
  assert 'if response is not None else None' in guarded


# -- the examples ------------------------------------------------------------------------


def test_github_plan_matches_snapshot():
  """The GitHub example's plan is pinned as JSON: a change here is a change to what every
  backend sees, and is made on purpose (see `SNAPSHOT`)."""
  plan = build_plan(_example('github'))
  assert plan.to_json() == json.loads(SNAPSHOT.read_text())


def test_kraken_plan_envelopes_and_streams():
  """Kraken's REST responses are enveloped: the selector and the wire type are on the
  plan beside the returned payload. Its streams carry the declared verb, and its WS
  commands are `rpc` over `ws` with the identifier as `wire.path`."""
  plan = build_plan(_example('kraken'))
  ticker = plan.endpoint('spot.market_data.ticker')
  assert ticker is not None
  assert ticker.response.selector == 'result'
  assert ticker.response.wire is not None and ticker.response.wire in ticker.wire_types
  assert ticker.response.payload is not None and ticker.response.payload in ticker.types
  assert ticker.response.wire != ticker.response.payload
  stream = plan.endpoint('streams.market_data.ticker')
  assert stream is not None and stream.stream is not None
  assert stream.stream.verb == {'path': 'method', 'subscribe': 'subscribe', 'unsubscribe': 'unsubscribe'}
  assert stream.request.type == 'Parameters'
  command = plan.endpoint('trading_ws.add_order')
  assert command is not None
  assert command.kind == 'rpc' and command.transports == ['ws']
  assert command.wire.path == 'add_order' and command.wire.method is None
  assert command.core == 'socket'
  history = plan.endpoint('spot.account.trades_history')
  assert history is not None and history.pagination is not None
  assert history.pagination.walker == 'generator' and history.pagination.size_default == 50


@pytest.mark.parametrize('name', ['github', 'kraken'])
def test_generated_code_agrees_with_the_plan(name: str):
  """For every endpoint of an example, the module the Python backend generated renders
  the plan's decisions: the walker shape and its row/state types, and the cast."""
  root = _example(name)
  project = resolve(root)
  plan = build_plan(project)
  generator = Generator()
  checked = 0
  for endpoint in plan.endpoints:
    module = project.package_dir.joinpath(*endpoint.path[:-1], f'{endpoint.path[-1]}.py')
    if not module.is_file():
      continue
    code = module.read_text()
    expects_cast = endpoint.request.needs_cast or endpoint.response.needs_cast
    assert ('cast(type, ' in code) == expects_cast, endpoint.function
    pagination = endpoint.pagination
    if pagination is None:
      assert '_paged(' not in code, endpoint.function
      continue
    checked += 1
    if pagination.walker == 'paginated':
      assert pagination.row_type is not None and pagination.state_type is not None
      row = generator.plan_type_code(pagination.row_type)
      state = generator.plan_type_code(pagination.state_type)
      assert f'PaginatedResponse[{row}, {state}]' in code, endpoint.function
    elif pagination.walker == 'generator':
      assert 'AsyncIterator[' in code and 'PaginatedResponse' not in code, endpoint.function
    else:
      assert '_paged(' not in code, endpoint.function
  assert checked >= 1


# -- the command -------------------------------------------------------------------------


def test_plan_command_prints_a_summary_and_json():
  runner = CliRunner()
  summary = runner.invoke(app, ['plan', '--project', str(FIXTURE_ROOT)])
  assert summary.exit_code == 0, summary.output
  assert 'fixture_client: root class FixtureClient' in summary.output
  assert 'market.order_list  rpc  http' in summary.output
  assert 'paged=token/absent_cursor walker=paginated driver=pageKey' in summary.output
  assert 'market.ticker_stream  stream  ws  channel ' in summary.output
  assert 'direct-channel' in summary.output

  as_json = runner.invoke(app, ['plan', '--project', str(FIXTURE_ROOT), '--json'])
  assert as_json.exit_code == 0, as_json.output
  assert json.loads(as_json.output) == build_plan(FIXTURE_ROOT).to_json()


def test_plan_command_reports_a_missing_project(tmp_path: Path):
  result = CliRunner().invoke(app, ['plan', '--project', str(tmp_path)])
  assert result.exit_code == 1
