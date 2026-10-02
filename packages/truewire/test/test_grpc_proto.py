"""The `.proto` reader and the stub build's tree checks (`truewire.grpc`, ADR 0017).

Proved here: the reader parses the grammar Cosmos-shaped trees use and resolves names by
protobuf's scoping rule; the stripped tree drops custom options, imports the tree does not
hold and every declaration naming a type it lacks (emptied `oneof` groups included) while
keeping built-in options; `tree_problems` refuses only what an endpoint reaches; and, when
`buf` and the plugins are installed, `truewire protos` builds and checks both languages.
"""
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.grpc.proto import ProtoError, ProtoTree, json_name, parse_file
from truewire.grpc.stubs import StubError, find_tool, tree_problems
from truewire.project import load_project

FIXTURE = Path(__file__).parent / 'fixtures' / 'grpc_client'


@pytest.fixture(scope='module')
def tree() -> ProtoTree:
  return ProtoTree(FIXTURE / 'spec' / 'proto')


def test_reader_collects_declarations(tree: ProtoTree):
  assert set(tree.files) == {
    'demo/bank/v1/query.proto', 'demo/base/v1/coin.proto', 'demo/base/v1/pagination.proto',
  }
  query = tree.files['demo/bank/v1/query.proto']
  assert query.package == 'demo.bank.v1'
  assert 'gogoproto/gogo.proto' in query.imports
  service = tree.services['demo.bank.v1.Query']
  assert list(service.methods) == ['Balance', 'AllBalances', 'Search', 'Params']
  search = tree.messages['demo.bank.v1.SearchRequest']
  labels = search.field('labels')
  assert labels is not None and labels.map_key == 'string' and labels.label == 'repeated'
  assert 'demo.bank.v1.SearchRequest.Order' in tree.enums
  assert 'demo.bank.v1.SearchResponse.Hit' in tree.messages
  detail = [f.name for f in tree.messages['demo.bank.v1.SearchResponse'].fields if f.oneof == 'detail']
  assert detail == ['note', 'code']
  resolve_denom = tree.messages['demo.bank.v1.QueryAllBalancesRequest'].field('resolve_denom')
  assert resolve_denom is not None and resolve_denom.label == 'optional'


def test_names_resolve_innermost_scope_first(tree: ProtoTree):
  assert tree.resolve('Hit', 'demo.bank.v1.SearchResponse') == ('message', 'demo.bank.v1.SearchResponse.Hit')
  assert tree.resolve('demo.base.v1.Coin', 'demo.bank.v1.SearchResponse.Hit') == ('message', 'demo.base.v1.Coin')
  assert tree.resolve('.demo.base.v1.PageRequest', 'x') == ('message', 'demo.base.v1.PageRequest')
  assert tree.resolve('Order', 'demo.bank.v1.SearchRequest') == ('enum', 'demo.bank.v1.SearchRequest.Order')
  assert tree.resolve('uint64', 'demo') == ('scalar', 'uint64')
  assert tree.resolve('google.protobuf.Timestamp', 'demo.bank.v1') == ('external', 'google.protobuf.Timestamp')
  assert tree.closure('demo.bank.v1.SearchResponse') == {
    'demo.bank.v1.SearchResponse', 'demo.bank.v1.SearchResponse.Hit', 'demo.base.v1.Coin',
  }


def test_unresolved_lists_fields_and_rpcs_naming_missing_types(tree: ProtoTree):
  assert tree.unresolved() == [
    ('demo/bank/v1/query.proto', 'demo.bank.v1.Query/Params', 'Params'),
    ('demo/bank/v1/query.proto', 'demo.bank.v1.Unused.metadata', 'Metadata'),
    ('demo/bank/v1/query.proto', 'demo.bank.v1.Unused.missing', 'MissingDetail'),
    ('demo/bank/v1/query.proto', 'demo.bank.v1.Unused.only', 'MissingDetail'),
  ]


def test_stripped_tree_keeps_the_wire_and_drops_what_cannot_resolve(tree: ProtoTree):
  stripped = tree.stripped()
  query = stripped['demo/bank/v1/query.proto']
  coin = stripped['demo/base/v1/coin.proto']
  for text in (query, coin):
    assert 'gogoproto' not in text and 'cosmos_proto' not in text and 'google.api' not in text
  assert 'import "google/protobuf/timestamp.proto";' in query
  assert 'demo/bank/v1/bank.proto' not in query
  assert 'rpc Params' not in query
  assert '[deprecated = true]' in query
  assert 'option go_package' in query
  assert 'string amount = 2 ;' in coin
  assert 'oneof gone' not in query and 'MissingDetail' not in query and 'Metadata metadata' not in query
  assert 'oneof detail {\n    string note = 1;' in query
  for path, text in stripped.items():
    parsed, _ = parse_file(text, path)
    assert parsed.edits == [], path


def test_reader_reports_where_it_stops():
  with pytest.raises(ProtoError, match=r'broken.proto:3: expected'):
    parse_file('syntax = "proto3";\nmessage A {\n  string = 1;\n}\n', 'broken.proto')


def test_json_name():
  assert json_name('next_key') == 'nextKey'
  assert json_name('count_total') == 'countTotal'
  assert json_name('page') == 'page'


def test_tree_problems_name_only_what_an_endpoint_reaches(tmp_path: Path, tree: ProtoTree):
  assert tree_problems(load_project(FIXTURE), tree) == []
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns('src', '.truewire', 'node_modules'))
  endpoint = root / 'spec' / 'endpoints' / 'bank' / 'search' / 'endpoint.json'
  data = json.loads(endpoint.read_text())
  data['spec']['response'] = 'demo.bank.v1.Unused'
  endpoint.write_text(json.dumps(data))
  balance = root / 'spec' / 'endpoints' / 'bank' / 'balance' / 'endpoint.json'
  data = json.loads(balance.read_text())
  data['spec']['rpc'] = 'Params'
  balance.write_text(json.dumps(data))
  problems = tree_problems(load_project(root), ProtoTree(root / 'spec' / 'proto'))
  assert problems == [
    'bank.balance: demo.bank.v1.Query/Params (demo/bank/v1/query.proto) names Params, which no file under spec/proto/ declares',
    'bank.search: demo.bank.v1.Unused.metadata (demo/bank/v1/query.proto) names Metadata, which no file under spec/proto/ declares',
    'bank.search: demo.bank.v1.Unused.missing (demo/bank/v1/query.proto) names MissingDetail, which no file under spec/proto/ declares',
    'bank.search: demo.bank.v1.Unused.only (demo/bank/v1/query.proto) names MissingDetail, which no file under spec/proto/ declares',
  ]


def _tools_or_skip(language: str):
  project = load_project(FIXTURE)
  try:
    find_tool(project, 'buf')
    find_tool(project, 'protoc-gen-es' if language == 'typescript' else 'protoc-gen-go')
  except StubError as exc:
    pytest.skip(str(exc))


@pytest.mark.parametrize(('language', 'sample'), [
  ('typescript', 'demo/bank/v1/query_pb.ts'),
  ('go', 'demo/bank/v1/query.pb.go'),
])
def test_protos_command_builds_and_checks_stubs(tmp_path: Path, language: str, sample: str):
  _tools_or_skip(language)
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns('src', '.truewire', 'node_modules'))
  runner = CliRunner()
  stale = runner.invoke(app, ['protos', language, '--project', str(root), '--check'])
  assert stale.exit_code == 1 and 'missing:' in stale.output
  built = runner.invoke(app, ['protos', language, '--project', str(root)])
  assert built.exit_code == 0, built.output
  out = root / 'src' / ('grpc_demo' if language == 'typescript' else 'grpcdemo') / 'protos'
  assert (out / sample).is_file()
  if language == 'go':
    assert 'package bankv1' in (out / sample).read_text()
  checked = runner.invoke(app, ['protos', language, '--project', str(root), '--check'])
  assert checked.exit_code == 0, checked.output
  suffix = '_pb.ts' if language == 'typescript' else '.pb.go'
  (out / f'gone{suffix}').write_text('x')
  (out / 'protos.go').write_text('package protos\n')
  drift = runner.invoke(app, ['protos', language, '--project', str(root), '--check'])
  assert drift.exit_code == 1 and f'extra: gone{suffix}' in drift.output and 'protos.go' not in drift.output
  rebuilt = runner.invoke(app, ['protos', language, '--project', str(root)])
  assert rebuilt.exit_code == 0, rebuilt.output
  assert not (out / f'gone{suffix}').exists() and (out / 'protos.go').is_file()
