"""gRPC endpoints on the plan (G9, ADR 0017): `truewire plan` lists them, and every fact a
backend renders a gRPC method and its walker from is resolved against `spec/proto/`."""
import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from truewire.cli import app
from truewire.plan.build import build_plan

FIXTURE = Path(__file__).parent / 'fixtures' / 'grpc_client'


def test_grpc_endpoints_are_planned():
  plan = build_plan(FIXTURE)
  assert [e.function for e in plan.endpoints] == ['bank.all_balances', 'bank.balance', 'bank.search']
  balance = plan.endpoint('bank.balance')
  assert balance is not None and balance.kind == 'grpc' and balance.transports == []
  assert balance.wire.path == '/demo.bank.v1.Query/Balance'
  assert balance.request.shape == 'message' and balance.response.payload is None
  assert balance.meta == {'public': True} and balance.core == 'grpc'
  grpc = balance.grpc
  assert grpc is not None
  assert (grpc.service, grpc.service_file, grpc.rpc, grpc.streaming) == (
    'demo.bank.v1.Query', 'demo/bank/v1/query.proto', 'Balance', 'unary',
  )
  assert grpc.request.model_dump() == {
    'kind': 'message', 'name': 'demo.bank.v1.QueryBalanceRequest', 'file': 'demo/bank/v1/query.proto',
    'package': 'demo.bank.v1',
  }
  assert [f.name for f in grpc.request_fields] == ['address', 'denom']
  assert balance.docs.description == "Query one denomination's balance of an address."
  assert [child.name for child in plan.routers[1].children] == ['all_balances', 'balance', 'search']


def test_request_fields_carry_types_and_presence():
  plan = build_plan(FIXTURE)
  fields = {f.name: f for f in plan.endpoint('bank.all_balances').grpc.request_fields}  # type: ignore[union-attr]
  assert fields['pagination'].type.file == 'demo/base/v1/pagination.proto'
  assert fields['pagination'].presence and not fields['address'].presence
  assert fields['resolve_denom'].presence and fields['resolve_denom'].json_name == 'resolveDenom'
  search = {f.name: f for f in plan.endpoint('bank.search').grpc.request_fields}  # type: ignore[union-attr]
  assert search['query'].presence  # listed in optional_scalars
  assert search['order_by'].type.model_dump() == {
    'kind': 'enum', 'name': 'demo.bank.v1.SearchRequest.Order', 'file': 'demo/bank/v1/query.proto',
    'package': 'demo.bank.v1',
  }
  assert search['labels'].type.package is None and not search['labels'].optional
  assert {f.name: f for f in plan.endpoint('bank.all_balances').grpc.request_fields}['resolve_denom'].optional  # type: ignore[union-attr]
  assert search['labels'].map_key == 'string' and search['labels'].repeated
  response = plan.endpoint('bank.balance')
  assert response is not None


def test_token_walk_resolves_its_paths_hop_by_hop():
  endpoint = build_plan(FIXTURE).endpoint('bank.all_balances')
  assert endpoint is not None and endpoint.pagination is not None and endpoint.grpc is not None
  pagination, paging = endpoint.pagination, endpoint.grpc.paging
  assert (pagination.strategy, pagination.driver, pagination.size, pagination.cursor_from, pagination.rows) == (
    'token', 'pagination.key', 'pagination.limit', 'pagination.next_key', 'balances',
  )
  assert pagination.walker == 'paginated' and pagination.seedable
  assert pagination.state_type == {'type': 'scalar', 'base': 'string', 'format': 'bytes'}
  assert paging is not None
  assert [(hop.name, hop.type.name) for hop in paging.driver] == [
    ('pagination', 'demo.base.v1.PageRequest'), ('key', 'bytes'),
  ]
  assert [hop.name for hop in paging.cursor or []] == ['pagination', 'next_key']
  assert paging.rows is not None and paging.rows[-1].repeated and paging.rows[-1].type.name == 'demo.base.v1.Coin'
  assert paging.total is None


def test_page_walk_with_a_total():
  endpoint = build_plan(FIXTURE).endpoint('bank.search')
  assert endpoint is not None and endpoint.pagination is not None and endpoint.grpc is not None
  assert endpoint.pagination.walker == 'paginated' and endpoint.pagination.start == 1
  assert endpoint.pagination.state_type == {'type': 'scalar', 'base': 'integer'}
  paging = endpoint.grpc.paging
  assert paging is not None and [hop.name for hop in paging.total or []] == ['total']
  assert paging.rows is not None and paging.rows[-1].type.name == 'demo.bank.v1.SearchResponse.Hit'


def test_a_path_that_does_not_resolve_leaves_nothing_to_walk(tmp_path: Path):
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE, root)
  spec = root / 'spec' / 'endpoints' / 'bank' / 'all_balances' / 'endpoint.json'
  data = json.loads(spec.read_text())
  data['pagination']['cursor']['parameter'] = 'pagination.cursor'
  spec.write_text(json.dumps(data))
  endpoint = build_plan(root).endpoint('bank.all_balances')
  assert endpoint is not None and endpoint.pagination is not None and endpoint.grpc is not None
  assert endpoint.pagination.walker == 'none' and endpoint.grpc.paging is None


def test_an_unknown_proto_name_is_planned_as_written(tmp_path: Path):
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE, root)
  spec = root / 'spec' / 'endpoints' / 'bank' / 'balance' / 'endpoint.json'
  data = json.loads(spec.read_text())
  data['spec']['request'] = 'demo.bank.v1.Nope'
  spec.write_text(json.dumps(data))
  endpoint = build_plan(root).endpoint('bank.balance')
  assert endpoint is not None and endpoint.grpc is not None
  assert endpoint.grpc.request.model_dump() == {'kind': 'message', 'name': 'demo.bank.v1.Nope', 'file': None, 'package': None}
  assert endpoint.grpc.request_fields == []


def test_plan_command_prints_grpc_endpoints():
  result = CliRunner().invoke(app, ['plan', '--project', str(FIXTURE)])
  assert result.exit_code == 0, result.output
  assert (
    'bank.all_balances  grpc  unary  /demo.bank.v1.Query/AllBalances  core=grpc  '
    'request=demo.bank.v1.QueryAllBalancesRequest  returns=demo.bank.v1.QueryAllBalancesResponse  '
    'paged=token/absent_cursor walker=paginated driver=pagination.key'
  ) in result.output
  as_json = json.loads(CliRunner().invoke(app, ['plan', '--project', str(FIXTURE), '--json']).output)
  kinds = {endpoint['kind'] for endpoint in as_json['endpoints']}
  assert kinds == {'grpc'}
  assert as_json['endpoints'][0]['grpc']['paging']['driver'][1]['name'] == 'key'
