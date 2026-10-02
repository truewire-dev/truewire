"""The Go backend (`truewire generate go`): the plan rendered as Go packages over
`truewire.dev/core`.

Three things are proved here. The renderers decide Go's part of the plan the documented way
(`docs/go.md`: exported names with initialisms, formats to `truewire` types, nil-able forms
for optional and nullable keys, hoisted literals/unions/tuples, pointers on cycles,
`gofmt`-shaped output). The whole fixture package renders, and compiles and vets when a Go
toolchain is available. And the CLI keeps the manifest discipline of every other backend.
"""
import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.go import import_path, render_package, root_struct_name
from truewire.codegen.go.meta import meta_module
from truewire.codegen.go.names import camel_ident, package_ident, pascal_ident, unique
from truewire.codegen.go.printer import Imports, Writer
from truewire.codegen.go.types import Module, Package
from truewire.plan.build import build_plan
from truewire.plan.model import CorePlan, PackagePlan
from truewire.project import load_project

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'codegen_fixture_client'
REPO = Path(__file__).parents[3]
EXAMPLES = REPO / 'examples'
CORE_GO = REPO / 'packages' / 'core-go'


def _go() -> str | None:
  for candidate in (shutil.which('go'), str(Path.home() / '.local' / 'go' / 'bin' / 'go')):
    if candidate and Path(candidate).is_file():
      return candidate
  return None


def _module(types, name='m') -> Module:
  plan = PackagePlan(name='x', root_class='X')
  return Module(plan, 'm/m.go', package=Package('example.com/x'), name=name, local=types, visible=[''])


# -- naming and printing --------------------------------------------------------------


def test_naming_rule():
  """Exported names are PascalCase with Go's initialisms, packages run their words
  together, unexported names avoid keywords."""
  assert pascal_ident('html_url') == 'HTMLURL'
  assert pascal_ident('node_id') == 'NodeID'
  assert pascal_ident('per_page') == 'PerPage'
  assert pascal_ident('2fa') == 'N2fa'
  assert pascal_ident('') == 'Value'
  assert package_ident('list_commits') == 'listcommits'
  assert package_ident('type') == 'typepkg'
  assert camel_ident('type') == 'type_'
  assert camel_ident('list_commits') == 'listCommits'
  assert unique('a', {'a', 'a2'}) == 'a3'


def test_printer_aligns_struct_fields_like_gofmt():
  """A run of consecutive fields is aligned; a doc comment or a blank line breaks the run."""
  w = Writer()
  w.struct('type A struct {', [('ID', 'int64', None), ('NodeIdentifier', '*string', 'Doc.'), ('X', 'bool', None), None, ('Longer', 'any', None)])
  assert w.render() == (
    'type A struct {\n\tID int64\n\t// Doc.\n\tNodeIdentifier *string\n\tX              bool\n\n\tLonger any\n}\n'
  )
  imports = Imports()
  imports.add('truewire.dev/core', 'truewire')
  imports.std('encoding/json')
  imports.std('context')
  imports.add('example.com/x/meta')
  assert imports.render() == [
    'import (', '\t"context"', '\t"encoding/json"', '', '\t"example.com/x/meta"', '\ttruewire "truewire.dev/core"', ')',
  ]


def test_records_render_nilable_forms_and_descriptors():
  """Required keys hold the value, optional or nullable keys a nil-able form, optional and
  nullable keys `truewire.Optional`; every key is named on the wire in the methods."""
  string = {'type': 'scalar', 'base': 'string'}
  null = {'type': 'scalar', 'base': 'null'}
  module = _module({'Issue': {'type': 'record', 'id': 'Issue', 'fields': {
    'id': {'type': {'type': 'scalar', 'base': 'integer'}, 'required': True},
    'body': {'type': {'type': 'union', 'variants': [{'type': string}, {'type': null}]}, 'required': True},
    'labels': {'type': {'type': 'list', 'item': string}, 'required': False},
    'closed_at': {'type': {'type': 'union', 'variants': [{'type': {'type': 'scalar', 'base': 'string', 'format': 'date-time'}}, {'type': null}]}, 'required': False},
    'state': {'type': {'type': 'literal', 'values': ['open', 'closed']}, 'required': True},
    'parent': {'type': {'type': 'ref', 'id': 'Issue'}, 'required': False},
  }}})
  module.define_all(module.local)
  source = module.render()
  assert '\tID       int64\n' in source or '\tID int64\n' in source
  fields = {f.wire: f for f in module.fields['Issue']}
  assert fields['id'].type == 'int64' and fields['id'].descriptor == 'Required'
  assert fields['body'].type == '*string' and fields['body'].descriptor == 'RequiredNullable'
  assert fields['labels'].type == '[]string' and fields['labels'].descriptor == 'OptionalField'
  assert fields['closed_at'].type == 'truewire.Optional[*truewire.TimestampIso]'
  assert fields['state'].type == 'IssueState'
  assert fields['parent'].type == '*Issue'
  assert 'const IssueStateOpen IssueState = "open"' in source
  assert 'truewire.Required("id", &r.ID),' in source
  assert 'truewire.OptionalNullable("closed_at", &r.ClosedAt),' in source


def test_unions_and_tuples_are_hoisted_structs():
  string = {'type': 'scalar', 'base': 'string'}
  module = _module({
    'Level': {'type': 'tuple', 'items': [{'type': 'scalar', 'base': 'string', 'format': 'decimal-string'}, {'type': 'scalar', 'base': 'number'}]},
    'Id': {'type': 'union', 'variants': [{'type': {'type': 'scalar', 'base': 'integer'}}, {'type': string}]},
  })
  module.define_all(module.local)
  source = module.render()
  assert 'type Level struct {\n\tV0 truewire.Decimal\n\tV1 float64\n}' in source
  assert 'truewire.DecodeUnion(data, truewire.VariantOf(&u.Integer), truewire.VariantOf(&u.String))' in source


def test_meta_module_renders_one_struct_per_core_with_a_schema():
  cores = {
    'default': CorePlan(meta={'type': 'object', 'properties': {'signed': {'type': 'boolean'}, 'scope': {'type': 'string'}}, 'required': ['scope']}),
    'plain': CorePlan(),
  }
  source = meta_module(cores)
  assert source is not None and 'type DefaultMeta struct {\n\tSigned *bool\n\tScope  string\n}' in source
  assert meta_module({'plain': CorePlan()}) is None


# -- whole packages -------------------------------------------------------------------


@pytest.fixture(scope='module')
def fixture_rendered():
  return render_package(build_plan(load_project(FIXTURE_ROOT)))


def test_fixture_package_layout(fixture_rendered):
  files = fixture_rendered.files
  assert 'client.go' in files and 'meta/meta.go' in files and 'types/types.go' in files
  assert 'market/orderlist/orderlist.go' in files and 'market/market.go' in files
  assert 'replay/replay.go' in files
  assert all(content.startswith('// Code generated by truewire. DO NOT EDIT.\n') for content in files.values())


def test_fixture_walkers_render(fixture_rendered):
  orderlist = fixture_rendered.files['market/orderlist/orderlist.go']
  assert 'func (e *Endpoint) OrderListPaged(request PagedRequest, opts ...truewire.CallOption) *truewire.PaginatedResponse[' in orderlist
  assert 'truewire.CursorOrDone(&following)' in orderlist
  total = fixture_rendered.files['market/orderpagetotal/orderpagetotal.go']
  assert 'truewire.TotalReached(' in total


def test_github_example_output_is_what_the_backend_renders():
  """The committed `examples/github/src/github/**/*.go` is the backend's current output."""
  root = EXAMPLES / 'github'
  project = load_project(root)
  if project.go is None:
    pytest.skip('examples/github declares no [go] section')
  rendered = render_package(build_plan(project), project)
  assert import_path(build_plan(project), project) == 'truewire.dev/examples/github/src/github'
  assert root_struct_name(build_plan(project), project) == 'GitHub'
  for path, content in rendered.files.items():
    assert (root / 'src' / 'github' / path).read_text() == content, path


def test_rendered_output_is_gofmt_clean_and_vets(fixture_rendered, tmp_path: Path):
  """What the printer writes is what `gofmt` would, and the packages compile."""
  go = _go()
  if go is None:
    pytest.skip('no Go toolchain')
  for path, content in fixture_rendered.files.items():
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
  (tmp_path / 'go.mod').write_text(
    f'module example.com/fixture_client\n\ngo 1.23\n\nrequire truewire.dev/core v0.0.0\n\nreplace truewire.dev/core => {CORE_GO}\n'
  )
  env = {**os.environ, 'GOFLAGS': '-mod=mod'}
  gofmt = Path(go).with_name('gofmt')
  formatted = subprocess.run([str(gofmt), '-l', '.'], cwd=tmp_path, capture_output=True, text=True)
  assert formatted.stdout == '', formatted.stdout
  subprocess.run([go, 'mod', 'tidy'], cwd=tmp_path, capture_output=True, text=True, env=env)
  vetted = subprocess.run([go, 'vet', './...'], cwd=tmp_path, capture_output=True, text=True, env=env)
  assert vetted.returncode == 0, vetted.stderr


# -- the CLI --------------------------------------------------------------------------


def test_generate_go_writes_checks_and_deletes(tmp_path: Path):
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('.truewire', 'core_impl'))
  toml = root / 'truewire.toml'
  toml.write_text(toml.read_text() + '\n[go]\npackage = "client"\nsrc = "go"\nname = "Client"\nmodule = "example.com/client"\nroot = "go"\n')
  runner = CliRunner()
  result = runner.invoke(app, ['generate', 'go', '--project', str(root)])
  assert result.exit_code == 0, result.output
  assert (root / 'go' / 'client' / 'client.go').is_file()
  assert 'example.com/client/client/market' in (root / 'go' / 'client' / 'client.go').read_text()
  assert runner.invoke(app, ['generate', 'go', '--check', '--project', str(root)]).exit_code == 0
  (root / 'go' / 'client' / 'client.go').write_text('drift')
  assert runner.invoke(app, ['generate', 'go', '--check', '--project', str(root)]).exit_code != 0
  assert runner.invoke(app, ['generate', 'go', '--delete', '--project', str(root)]).exit_code == 0
  assert not (root / 'go' / 'client' / 'client.go').exists()


def test_generate_go_needs_a_go_section(tmp_path: Path):
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('.truewire', 'core_impl'))
  result = CliRunner().invoke(app, ['generate', 'go', '--project', str(root)])
  assert result.exit_code != 0
  assert '[go]' in result.output


# -- hand-written methods and `truewire surface --language go` ------------------------


def test_routers_keep_their_core_for_hand_written_methods(fixture_rendered):
  router = fixture_rendered.files['market/market.go']
  assert '\t// The core this router was built from, for hand-written methods beside the generated ones.\n\tcore ' in router
  assert 'return &Market{core: core, ' in router


def test_a_hand_written_surface_is_skipped_and_reconciled():
  """kraken's `retrieve_export` declares a hand-written surface: the backend skips it, and
  the surface check finds the method a file beside the router defines."""
  from truewire.surface import reconcile

  project = load_project(EXAMPLES / 'kraken')
  if project.go is None:
    pytest.skip('examples/kraken declares no [go] section')
  rendered = render_package(build_plan(project), project)
  assert rendered.skipped == ['spot.account.retrieve_export: a hand-written surface (spot.account.retrieve_export:retrieve_export)']
  assert 'spot/account/retrieveexport/retrieveexport.go' not in rendered.files
  result = reconcile(project, language='go')
  assert result.gaps == []
  assert result.handwritten == ['spot.account.retrieve_export']
  assert len(result.generated) == 74


def test_surface_go_reports_a_missing_hand_written_method(tmp_path: Path):
  from truewire.surface import reconcile

  root = tmp_path / 'kraken'
  shutil.copytree(EXAMPLES / 'kraken', root, ignore=shutil.ignore_patterns('node_modules', '.truewire', 'test', 'tests'))
  (root / 'src' / 'kraken' / 'spot' / 'account' / 'retrieveexport.go').unlink()
  result = reconcile(load_project(root), language='go')
  faults = {(gap.function, gap.fault) for gap in result.gaps}
  assert ('spot.account.retrieve_export', 'no_symbol') in faults
  (root / 'src' / 'kraken' / 'spot' / 'marketdata' / 'ticker' / 'ticker.go').unlink()
  faults = {(gap.function, gap.fault) for gap in reconcile(load_project(root), language='go').gaps}
  assert ('spot.market_data.ticker', 'no_module') in faults


# -- typed-dev spec shapes (derived fixtures) -----------------------------------------


def _derived(tmp_path: Path, endpoints: dict[str, dict]) -> Path:
  """The fixture client with `endpoints` (spec path -> endpoint.json) added."""
  import json

  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('.truewire', 'core_impl'))
  for path, spec in endpoints.items():
    target = root / 'spec' / 'endpoints' / path / 'endpoint.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec))
  return root


def _vets(files: dict[str, str], where: Path) -> None:
  """`files` compile and vet as a module over `packages/core-go` (skipped without Go)."""
  go = _go()
  if go is None:
    pytest.skip('no Go toolchain')
  for path, content in files.items():
    target = where / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
  (where / 'go.mod').write_text(
    f'module example.com/fixture_client\n\ngo 1.23\n\nrequire truewire.dev/core v0.0.0\n\nreplace truewire.dev/core => {CORE_GO}\n'
  )
  env = {**os.environ, 'GOFLAGS': '-mod=mod'}
  subprocess.run([go, 'mod', 'tidy'], cwd=where, capture_output=True, text=True, env=env)
  vetted = subprocess.run([go, 'vet', './...'], cwd=where, capture_output=True, text=True, env=env)
  assert vetted.returncode == 0, vetted.stderr


_ROW = {'title': 'CursorOrder', 'type': 'object', 'properties': {'orderId': {'type': 'string', 'description': 'Order id.'}}, 'required': ['orderId']}

NULLABLE_CURSOR = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/order/cursor', 'method': 'GET',
    'request': {'title': 'OrderCursorRequest', 'type': 'object', 'properties': {
      'symbol': {'type': 'string', 'description': 'Trading pair.'},
      'cursor': {'type': 'string', 'description': 'Cursor.'},
    }, 'required': ['symbol']},
    'response': {'title': 'OrderCursorResponse', 'type': 'object', 'properties': {
      'orders': {'type': 'array', 'description': 'Rows.', 'items': _ROW},
      'pageInfo': {'title': 'PageInfo', 'type': 'object', 'description': 'Paging.', 'properties': {
        'endCursor': {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'description': 'Next cursor.'},
      }, 'required': ['endCursor']},
    }, 'required': ['orders', 'pageInfo']},
  },
  'pagination': {'strategy': 'token', 'cursor': {'parameter': 'cursor', 'from': 'pageInfo.endCursor'}, 'done': {'kind': 'absent_cursor', 'rows': 'orders'}},
}

TICKER_STREAM_RAW = {
  'meta': {'public': True},
  'spec': {
    'kind': 'stream', 'channel': '/ticker_raw/{symbol}',
    'parameters': {'title': 'TickerRawParams', 'type': 'object', 'properties': {
      'symbol': {'type': 'string', 'description': 'Trading pair.'},
    }, 'required': ['symbol']},
    'payload': {'title': 'TickerRaw', 'type': 'object', 'properties': {
      'price': {'type': 'string', 'description': 'Last price.'},
    }},
  },
}


def test_a_required_nullable_cursor_is_dereferenced_once(tmp_path: Path):
  """`endCursor: string | null`, required: Go holds it in one pointer, so the walker checks
  and dereferences it once (a second check does not compile)."""
  root = _derived(tmp_path, {'market/order_cursor': NULLABLE_CURSOR})
  rendered = render_package(build_plan(load_project(root)))
  source = rendered.files['market/ordercursor/ordercursor.go']
  assert 'truewire.CursorOrDone(&following)' in source
  assert source.count('== nil {') == 1
  _vets(rendered.files, tmp_path / 'go')


NULL_ONLY_DATA = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/account/upgrade', 'method': 'POST',
    'response': {'title': 'UpgradeResponse', 'type': 'object', 'properties': {
      'data': {'anyOf': [{'type': 'null'}], 'description': 'No content on success.'},
    }, 'required': ['data']},
  },
}


NULL_TYPED_KEY = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/vault', 'method': 'GET',
    'response': {'title': 'Vault', 'type': 'object', 'properties': {
      'name': {'type': 'string', 'description': 'Vault name.'},
      'followerState': {'type': 'null', 'description': 'Always null for a vault queried without a user.'},
    }, 'required': ['name', 'followerState']},
  },
}


def test_a_union_of_only_null_is_null_not_an_empty_union(tmp_path: Path):
  """`anyOf: [{type: null}]` strips to `null` itself: an empty union rendered
  `truewire.DecodeUnion(data, )`, which `gofmt` rewrites."""
  from truewire.plan.types import strip_null

  null = {'type': 'scalar', 'base': 'null'}
  assert strip_null({'type': 'union', 'variants': [{'type': null}]}) == null
  root = _derived(tmp_path, {'account/upgrade': NULL_ONLY_DATA})
  rendered = render_package(build_plan(load_project(root)))
  source = rendered.files['account/upgrade/upgrade.go']
  assert 'DecodeUnion' not in source
  go = _go()
  if go is not None:
    where = tmp_path / 'go'
    for path, content in rendered.files.items():
      (where / path).parent.mkdir(parents=True, exist_ok=True)
      (where / path).write_text(content)
    formatted = subprocess.run([str(Path(go).with_name('gofmt')), '-l', '.'], cwd=where, capture_output=True, text=True)
    assert formatted.stdout == '', formatted.stdout
  _vets(rendered.files, tmp_path / 'go')


def test_a_required_null_typed_key_decodes_null(tmp_path: Path):
  """A required key declared `{"type": "null"}` (hyperliquid's `vaultDetails.followerState`)
  admits null: it is decoded with `RequiredNullable`, not `Required`, which refuses null."""
  root = _derived(tmp_path, {'market/vault': NULL_TYPED_KEY})
  rendered = render_package(build_plan(load_project(root)))
  source = rendered.files['market/vault/vault.go']
  assert 'truewire.RequiredNullable("followerState", &r.FollowerState)' in source
  assert 'truewire.Required("followerState"' not in source
  files = dict(rendered.files)
  files['market/vault/vault_null_test.go'] = """package vault

import "testing"

func TestNullTypedKeyDecodesNull(t *testing.T) {
\tvar v Vault
\tif err := v.UnmarshalJSON([]byte(`{"name":"x","followerState":null}`)); err != nil {
\t\tt.Fatal(err)
\t}
\tif err := v.UnmarshalJSON([]byte(`{"name":"x"}`)); err == nil {
\t\tt.Fatal("followerState is still required")
\t}
}
"""
  _go_tests(files, tmp_path / 'go')


def test_a_router_renames_a_twin_colliding_with_a_sibling_endpoint(tmp_path: Path):
  """`ticker_stream_raw` is `TickerStreamRaw`; `ticker_stream`'s own raw twin is renamed."""
  root = _derived(tmp_path, {'market/ticker_stream_raw': TICKER_STREAM_RAW})
  rendered = render_package(build_plan(load_project(root)))
  router = rendered.files['market/market.go']
  assert 'func (r *Market) TickerStreamRaw(ctx context.Context, symbol string, ' in router
  assert '// TickerStreamRaw2 is TickerStream without validation' in router
  assert 'func (r *Market) TickerStreamRaw2(' in router
  assert 'return r.tickerStream.TickerStreamRaw(ctx, symbol, opts...)' in router
  assert 'func (r *Market) TickerStreamRawRaw(' in router
  _vets(rendered.files, tmp_path / 'go')


def test_a_skipped_walker_leaves_no_import_or_type_behind():
  from truewire.codegen.go.endpoint import _restore, _save

  module = _module({})
  module.writer.line('type Kept struct{}')
  saved = _save(module)
  module.std('strings')
  module.core('Seek')
  module.declare('PagedRequest')
  module.writer.line('type PagedRequest struct{}')
  _restore(module, saved)
  assert 'strings' not in module.imports and 'truewire.dev/core' not in module.imports
  assert 'PagedRequest' not in module.names
  assert module.writer.render() == 'type Kept struct{}\n'


def _go_tests(files: dict[str, str], where: Path) -> None:
  """`files` (generated plus `_test.go` files) pass `go test` over `packages/core-go`."""
  _vets(files, where)
  env = {**os.environ, 'GOFLAGS': '-mod=mod'}
  tested = subprocess.run([_go(), 'test', './...'], cwd=where, capture_output=True, text=True, env=env)  # type: ignore[list-item]
  assert tested.returncode == 0, tested.stdout + tested.stderr


def _offset_endpoint(path: str, done: dict, *, rows: str | None) -> dict:
  row = {'title': 'OffsetOrder', 'type': 'object', 'properties': {'orderId': {'type': 'string', 'description': 'Order id.'}}, 'required': ['orderId']}
  response: dict = {'type': 'array', 'title': 'OffsetOrders', 'description': 'Rows.', 'items': row}
  if rows is not None:
    response = {'title': 'OffsetResponse', 'type': 'object', 'properties': {
      rows: {**response, 'title': None},
      'total': {'type': 'integer', 'description': 'Rows in all.'},
    }, 'required': [rows]}
    del response['properties'][rows]['title']
  return {
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': path, 'method': 'GET',
      'request': {'title': 'OffsetRequest', 'type': 'object', 'properties': {
        'symbol': {'type': 'string', 'description': 'Trading pair.'},
        'offset': {'type': 'integer', 'default': 0, 'description': 'Rows to skip.'},
        'limit': {'type': 'integer', 'format': 'int32', 'default': 2, 'maximum': 2, 'description': 'Page size.'},
      }, 'required': ['symbol']},
      'response': response,
    },
    'pagination': {'strategy': 'offset', 'offset': {'parameter': 'offset'}, 'size': {'parameter': 'limit'}, 'done': done},
  }


SEEK_CAPPED = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/trades', 'method': 'GET',
    'request': {'title': 'TradesRequest', 'type': 'object', 'properties': {
      'fromId': {'type': 'integer', 'description': 'First trade id.'},
      'limit': {'type': 'integer', 'maximum': 1000, 'default': 500, 'description': 'Page size.'},
    }},
    'response': {'title': 'Trades', 'type': 'array', 'description': 'Rows.', 'items': {
      'title': 'Trade', 'type': 'object', 'properties': {'id': {'type': 'integer', 'description': 'Trade id.'}}, 'required': ['id'],
    }},
  },
  'pagination': {'strategy': 'seek', 'cursor': {'field': '[-1].id', 'unique': True}, 'bound': {'start': 'fromId'}, 'anchor': 'start', 'size': {'parameter': 'limit'}},
}

OFFSET_WALK_TEST = '''package orderoffsettotal

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	truewire "truewire.dev/core"
)

type pages struct{ offsets []int }

func (p *pages) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	var request struct {
		Offset int `json:"offset"`
		Limit  int `json:"limit"`
	}
	if err := json.Unmarshal(call.Request, &request); err != nil {
		return nil, err
	}
	p.offsets = append(p.offsets, request.Offset)
	var rows []string
	for i := request.Offset; i < request.Offset+2 && i < 5; i++ {
		rows = append(rows, fmt.Sprintf(`{"orderId":"%d"}`, i))
	}
	return json.RawMessage(fmt.Sprintf(`{"orders":[%s],"total":5}`, strings.Join(rows, ","))), nil
}

func TestOffsetWalkStepsByRowsAndStopsAtTheTotal(t *testing.T) {
	core := &pages{}
	limit := int64(50)
	walk := New(core).OrderOffsetTotalPaged(PagedRequest{Symbol: "X", Limit: &limit})
	rows, err := walk.All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 5 || rows[4].OrderID != "4" {
		t.Fatalf("rows %+v", rows)
	}
	if fmt.Sprint(core.offsets) != "[0 2 4]" {
		t.Fatalf("offsets %v", core.offsets)
	}
	// Resuming from a page's Next state re-requests exactly that page.
	core.offsets = nil
	resumed, err := walk.Resume(2).All(context.Background())
	if err != nil || len(resumed) != 3 || fmt.Sprint(core.offsets) != "[2 4]" {
		t.Fatalf("resumed %+v %v %v", resumed, core.offsets, err)
	}
}
'''


def test_offset_page_total_counts_a_partial_last_page(tmp_path: Path):
  endpoint = _offset_endpoint('/v1/offset/pages', {
    'kind': 'total', 'path': 'total', 'counts': 'pages', 'rows': 'orders',
  }, rows='orders')
  endpoint['spec']['request']['properties']['limit'].update(default=4, maximum=500)
  root = _derived(tmp_path, {'market/order_offset_total': endpoint})
  rendered = render_package(build_plan(load_project(root)))
  assert not rendered.skipped, rendered.skipped
  wire_test = OFFSET_WALK_TEST.replace('request.Offset+2 && i < 5', 'request.Offset+request.Limit && i < 10')
  wire_test = wire_test.replace('"total":5', '"total":3').replace('limit := int64(50)', 'limit := int64(4)')
  wire_test = wire_test.replace('len(rows) != 5 || rows[4].OrderID != "4"', 'len(rows) != 10 || rows[9].OrderID != "9"')
  wire_test = wire_test.replace('[0 2 4]', '[0 4 8]').replace('Resume(2)', 'Resume(5)')
  wire_test = wire_test.replace('len(resumed) != 3', 'len(resumed) != 5 || resumed[4].OrderID != "9"').replace('[2 4]', '[5 9]')
  _go_tests({**rendered.files, 'market/orderoffsettotal/walk_test.go': wire_test}, tmp_path / 'go')


def test_offset_walks_step_by_rows_and_clamp_the_size(tmp_path: Path):
  """`offset` walks (binance, deribit, coinbase, bit2me) are resumable walkers over the row
  offset; a caller's page size is clamped to the schema's `maximum`, so a page full at the
  maximum is not read as the last."""
  root = _derived(tmp_path, {
    'market/order_offset_total': _offset_endpoint('/v1/offset/total', {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'orders'}, rows='orders'),
    'market/order_offset_short': _offset_endpoint('/v1/offset/short', {'kind': 'short_page'}, rows=None),
    'market/order_offset_empty': _offset_endpoint('/v1/offset/empty', {'kind': 'empty', 'rows': 'orders'}, rows='orders'),
  })
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'offset' in note]
  total = rendered.files['market/orderoffsettotal/orderoffsettotal.go']
  assert '*truewire.PaginatedResponse[OffsetOrder, int64]' in total
  assert 'following := state + int64(len(rows))' in total
  assert 'int64(state)+int64(len(rows)) >= int64(total)' in total
  assert 'return truewire.NewPaginatedResponse(int64(0), next)' in total
  short = rendered.files['market/orderoffsetshort/orderoffsetshort.go']
  assert 'truewire.Exhausted(len(rows), size)' in short
  assert 'size = min(int(*request.Limit), 2)' in short
  # `empty` ends on a page with no rows only: a short page may be followed by more rows.
  empty = rendered.files['market/orderoffsetempty/orderoffsetempty.go']
  assert 'if len(rows) == 0 {' in empty
  assert 'Exhausted' not in empty
  _go_tests({**rendered.files, 'market/orderoffsettotal/walk_test.go': OFFSET_WALK_TEST}, tmp_path / 'go')


EMPTY_TOKEN = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/order/empty', 'method': 'GET',
    'request': {'title': 'OrderEmptyRequest', 'type': 'object', 'properties': {
      'symbol': {'type': 'string', 'description': 'Trading pair.'},
      'idLessThan': {'type': 'string', 'description': 'Cursor.'},
    }, 'required': ['symbol']},
    'response': {'title': 'OrderEmptyResponse', 'type': 'object', 'properties': {
      'list': {'type': 'array', 'description': 'Rows.', 'items': {**_ROW, 'title': 'EmptyOrder'}},
      'endId': {'type': 'string', 'description': 'The last row id.'},
    }, 'required': ['list']},
  },
  'pagination': {
    'strategy': 'token', 'cursor': {'parameter': 'idLessThan', 'from': 'endId'},
    'done': {'kind': 'empty', 'rows': 'list'},
  },
}

EMPTY_WALK_TEST = '''package orderempty

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"

	truewire "truewire.dev/core"
)

type cursors struct{ seen []string }

func (c *cursors) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	var request struct {
		IDLessThan string `json:"idLessThan"`
	}
	if err := json.Unmarshal(call.Request, &request); err != nil {
		return nil, err
	}
	c.seen = append(c.seen, request.IDLessThan)
	if request.IDLessThan == "" {
		return json.RawMessage(`{"list":[{"orderId":"1"},{"orderId":"2"}],"endId":"2"}`), nil
	}
	// An empty page still carries a cursor: the walk ends on the empty rows, not on it.
	return json.RawMessage(`{"list":[],"endId":"2"}`), nil
}

func TestEmptyTokenWalkStopsOnTheEmptyPage(t *testing.T) {
	core := &cursors{}
	rows, err := New(core).OrderEmptyPaged(PagedRequest{Symbol: "X"}).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || fmt.Sprint(core.seen) != "[ 2]" {
		t.Fatalf("rows %+v seen %q", rows, core.seen)
	}
}
'''


def test_token_walk_ending_on_an_empty_page_is_a_resumable_walker(tmp_path: Path):
  """bitget's `token` walks end on an empty page: a `PaginatedResponse` over the cursor,
  stopping on the first page with no rows even when it still carries a cursor."""
  root = _derived(tmp_path, {'market/order_empty': EMPTY_TOKEN})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'order_empty' in note]
  source = rendered.files['market/orderempty/orderempty.go']
  assert '*truewire.PaginatedResponse[EmptyOrder, string]' in source
  assert 'if len(rows) == 0 {' in source
  _go_tests({**rendered.files, 'market/orderempty/walk_test.go': EMPTY_WALK_TEST}, tmp_path / 'go')


SEEK_WALK_TEST = '''package trades

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	truewire "truewire.dev/core"
)

// venue serves trades 0..9 with id >= fromId (inclusive), at most min(limit, 1000) of them.
type venue struct{ limits []string }

func (v *venue) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	var request struct {
		FromID *int64 `json:"fromId"`
		Limit  *int64 `json:"limit"`
	}
	if err := json.Unmarshal(call.Request, &request); err != nil {
		return nil, err
	}
	limit := int64(500)
	v.limits = append(v.limits, "unset")
	if request.Limit != nil {
		limit = min(*request.Limit, 1000)
		v.limits[len(v.limits)-1] = fmt.Sprint(*request.Limit)
	}
	var rows []string
	for id := int64(0); id < 10 && int64(len(rows)) < limit; id++ {
		if request.FromID == nil || id >= *request.FromID {
			rows = append(rows, fmt.Sprintf(`{"id":%d}`, id))
		}
	}
	return json.RawMessage("[" + strings.Join(rows, ",") + "]"), nil
}

func TestLimitOneWalksEveryRowOfAnInclusiveBound(t *testing.T) {
	core := &venue{}
	from, limit := int64(0), int64(1)
	rows, err := New(core).TradesPaged(Request{FromID: &from, Limit: &limit}).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 10 || rows[9].ID != 9 {
		t.Fatalf("rows %+v", rows)
	}
	for _, sent := range core.limits {
		if sent != "2" {
			t.Fatalf("sent limits %v", core.limits)
		}
	}
	if limit != 1 {
		t.Fatalf("the caller's own limit was overwritten: %d", limit)
	}
}

func TestTheClampedSizeIsWhatEveryRequestSends(t *testing.T) {
	core := &venue{}
	limit := int64(5000)
	if _, err := New(core).TradesPaged(Request{Limit: &limit}).All(context.Background()); err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(core.limits) != "[1000]" {
		t.Fatalf("sent limits %v", core.limits)
	}
	core = &venue{}
	if _, err := New(core).TradesPaged(Request{}).All(context.Background()); err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(core.limits) != "[unset]" {
		t.Fatalf("sent limits %v", core.limits)
	}
}
'''


SEEK_REQUIRED_WALK_TEST = '''package tradesrequired

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	truewire "truewire.dev/core"
)

// venue serves trades 0..9 with id >= fromId (inclusive), at most min(limit, 1000) of them;
// a limit of 0 serves its default of 500.
type venue struct{ limits []int64 }

func (v *venue) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	var request struct {
		FromID *int64 `json:"fromId"`
		Limit  int64  `json:"limit"`
	}
	if err := json.Unmarshal(call.Request, &request); err != nil {
		return nil, err
	}
	v.limits = append(v.limits, request.Limit)
	limit := min(request.Limit, 1000)
	if limit == 0 {
		limit = 500
	}
	var rows []string
	for id := int64(0); id < 10 && int64(len(rows)) < limit; id++ {
		if request.FromID == nil || id >= *request.FromID {
			rows = append(rows, fmt.Sprintf(`{"id":%d}`, id))
		}
	}
	return json.RawMessage("[" + strings.Join(rows, ",") + "]"), nil
}

// A required size is a plain int64, so a caller who leaves it out passes 0: that is unset,
// sent as it is, and the cap keeps the default rather than walking 2-row pages.
func TestARequiredSizeLeftAtZeroIsSentAsZero(t *testing.T) {
	core := &venue{}
	rows, err := New(core).TradesRequiredPaged(Request{}).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 10 || fmt.Sprint(core.limits) != "[0]" {
		t.Fatalf("rows %d, sent limits %v", len(rows), core.limits)
	}
}

func TestARequiredSizeOf1Sends2(t *testing.T) {
	core := &venue{}
	from := int64(0)
	rows, err := New(core).TradesRequiredPaged(Request{FromID: &from, Limit: 1}).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 10 {
		t.Fatalf("rows %+v", rows)
	}
	for _, sent := range core.limits {
		if sent != 2 {
			t.Fatalf("sent limits %v", core.limits)
		}
	}
}
'''


def test_a_seek_walk_sends_the_size_it_clamps_at_least_2(tmp_path: Path):
  """The caller's size is clamped once, into the request every page sends: at most the
  schema's `maximum`, at least 2 (a page of 1 cannot advance past an inclusive moving
  bound), in a new value rather than through the caller's pointer. It is also the cap; an
  omitted size stays unset and the schema default is the cap."""
  required = copy.deepcopy(SEEK_CAPPED)
  required['spec']['request']['required'] = ['limit']
  nullable = copy.deepcopy(SEEK_CAPPED)
  nullable['spec']['request']['properties']['limit']['type'] = ['integer', 'null']
  root = _derived(tmp_path, {'market/trades': SEEK_CAPPED, 'market/trades_required': required, 'market/trades_nullable': nullable})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'trades' in note], rendered.skipped
  assert (
    '\tif request.Limit != 0 {\n'
    '\t\trequest.Limit = min(max(request.Limit, 2), 1000)\n'
    '\t\twalk.Cap = int(request.Limit)\n'
    '\t}\n'
  ) in rendered.files['market/tradesrequired/tradesrequired.go']
  assert (
    '\tif request.Limit.Set && request.Limit.Value != nil {\n'
    '\t\tsize := min(max(*request.Limit.Value, 2), 1000)\n'
    '\t\trequest.Limit.Value = &size\n'
  ) in rendered.files['market/tradesnullable/tradesnullable.go']
  source = rendered.files['market/trades/trades.go']
  assert 'walk.Cap = 500' in source
  assert (
    '\tif request.Limit != nil {\n'
    '\t\tsize := min(max(*request.Limit, 2), 1000)\n'
    '\t\trequest.Limit = &size\n'
    '\t\twalk.Cap = int(size)\n'
    '\t}\n'
  ) in source
  assert 'The walk requests pages of at least 2 rows and at most 1000: a page must hold one new row beside the one it re-reads.' in source
  _go_tests({
    **rendered.files, 'market/trades/walk_test.go': SEEK_WALK_TEST,
    'market/tradesrequired/walk_test.go': SEEK_REQUIRED_WALK_TEST,
  }, tmp_path / 'go')


SEEK_INTEGER_STRING = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/transfers', 'method': 'GET',
    'request': {'title': 'TransfersRequest', 'type': 'object', 'properties': {
      'fromBlock': {'type': 'string', 'format': 'integer-string', 'description': 'First block.'},
      'toBlock': {'type': 'string', 'format': 'integer-string', 'description': 'Last block.'},
    }},
    'response': {'title': 'Transfers', 'type': 'array', 'description': 'Rows.', 'items': {
      'title': 'Transfer', 'type': 'object', 'properties': {
        'block': {'type': 'string', 'format': 'integer-string', 'description': 'Block.'},
        'value': {'type': 'string', 'format': 'integer-string', 'description': 'Wei.'},
      }, 'required': ['block', 'value'],
    }},
  },
  'pagination': {
    'strategy': 'seek', 'cursor': {'field': '[-1].block', 'unique': False},
    'bound': {'start': 'fromBlock', 'end': 'toBlock'}, 'anchor': 'start', 'span': {'parameter': 'span', 'default': 1000, 'unit': 's'},
  },
}


def test_integer_string_walkers_stay_exact(tmp_path: Path):
  """An `integer-string` holds any size of integer (a wei amount), so a seek bound over one
  compares and steps exactly, and a `total` sent as one ends an offset walk through Int64."""
  offset = _offset_endpoint('/v1/offset/total', {'kind': 'total', 'path': 'total', 'counts': 'items', 'rows': 'orders'}, rows='orders')
  offset['spec']['response']['properties']['total'] = {'type': 'string', 'format': 'integer-string', 'description': 'Rows in all.'}
  root = _derived(tmp_path, {'market/transfers': SEEK_INTEGER_STRING, 'market/order_offset_total': offset})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'transfers' in note or 'offset' in note], rendered.skipped
  seek = rendered.files['market/transfers/transfers.go']
  assert 'walk.Compare = func(a, b truewire.IntegerString) int { return a.Compare(b) }' in seek
  assert 'return pos.Add(span)' in seek
  total = rendered.files['market/orderoffsettotal/orderoffsettotal.go']
  assert 'totalN, fits := total.Int64()' in total
  assert 'int64(state)+int64(len(rows)) >= totalN' in total
  _vets(rendered.files, tmp_path / 'go')


DUAL_TIME = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http', 'ws'], 'path': 'public/get_time', 'method': 'POST',
    'response': {'title': 'ServerTime', 'type': 'object', 'properties': {'time': {'type': 'integer', 'description': 'Millis.'}}, 'required': ['time']},
  },
}

DUAL_CANCEL = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http', 'ws'], 'path': 'private/cancel_all', 'method': 'POST',
    'request': {'title': 'CancelAllRequest', 'type': 'object', 'properties': {'currency': {'type': 'string', 'description': 'Currency.'}}, 'required': ['currency']},
  },
}

DUAL_TEST = '''package gettime

import (
	"context"
	"encoding/json"
	"testing"

	truewire "truewire.dev/core"
)

type both struct{ http, ws []string }

func (b *both) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	b.http = append(b.http, call.Method+" "+call.Path)
	return json.RawMessage(`{"time":1}`), nil
}

func (b *both) Command(ctx context.Context, call truewire.CommandCall) (json.RawMessage, error) {
	b.ws = append(b.ws, call.Path)
	return json.RawMessage(`{"time":2}`), nil
}

func TestTheTransportOptionPicksTheWire(t *testing.T) {
	core := &both{}
	endpoint := New(core)
	overHTTP, err := endpoint.GetTime(context.Background())
	if err != nil || overHTTP.Time != 1 {
		t.Fatalf("http %+v %v", overHTTP, err)
	}
	overWS, err := endpoint.GetTime(context.Background(), truewire.WithTransport(truewire.TransportWS))
	if err != nil || overWS.Time != 2 {
		t.Fatalf("ws %+v %v", overWS, err)
	}
	raw, err := endpoint.GetTimeRaw(context.Background(), truewire.WithTransport(truewire.TransportWS))
	if err != nil || string(raw) != `{"time":2}` {
		t.Fatalf("raw %s %v", raw, err)
	}
	if len(core.http) != 1 || core.http[0] != "POST public/get_time" || len(core.ws) != 2 || core.ws[0] != "public/get_time" {
		t.Fatalf("calls http=%v ws=%v", core.http, core.ws)
	}
}
'''


def test_an_rpc_endpoint_with_both_transports_renders_both_halves(tmp_path: Path):
  """deribit and hyperliquid declare `transports: ["http", "ws"]`: the endpoint holds a
  `truewire.RpcEndpoint` and sends over HTTP unless the caller passes
  `truewire.WithTransport(truewire.TransportWS)`."""
  root = _derived(tmp_path, {'market/get_time': DUAL_TIME, 'market/cancel_all': DUAL_CANCEL})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'transports' in note]
  source = rendered.files['market/gettime/gettime.go']
  assert 'core truewire.RpcEndpoint' in source
  assert 'if truewire.Options(opts...).Transport == truewire.TransportWS {' in source
  assert 'return e.core.Command(ctx, truewire.CommandCall{Path: "public/get_time", ' in source
  assert 'return e.core.Request(ctx, truewire.HttpCall{Method: "POST", Path: "public/get_time", ' in source
  assert 'pass truewire.WithTransport(truewire.TransportWS) to send it over the WebSocket.' in source
  cancel = rendered.files['market/cancelall/cancelall.go']
  assert '\t\t_, err = e.core.Command(ctx, truewire.CommandCall{Path: "private/cancel_all", Request: body, ' in cancel
  # The dual endpoint's default is HTTP, so its recorded examples replay through it.
  assert '"market.get_time"' in rendered.files['replay/replay.go']
  _go_tests({**rendered.files, 'market/gettime/transport_test.go': DUAL_TEST}, tmp_path / 'go')


def test_proto_sources_render_as_the_protos_package(tmp_path: Path):
  """A project with `spec/proto/*.proto` gets `protos/protos.go` holding them verbatim
  (ADR 0016), `gofmt`-clean whatever the file names; one without gets no such package."""
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root)
  assert 'protos/protos.go' not in render_package(build_plan(root), load_project(root)).files
  (root / 'spec' / 'proto' / 'nested').mkdir(parents=True)
  (root / 'spec' / 'proto' / 'a.proto').write_text('syntax = "proto3";\nmessage A { string channel = 1; }\n')
  (root / 'spec' / 'proto' / 'nested' / 'long_file_name.proto').write_text('syntax = "proto3";\n// "é"\tx\nmessage B {}\n')
  source = render_package(build_plan(root), load_project(root)).files['protos/protos.go']
  assert '\tSources["a.proto"] = "syntax = \\"proto3\\";\\nmessage A { string channel = 1; }\\n"' in source
  assert source.index('"a.proto"') < source.index('"nested/long_file_name.proto"')
  go = _go()
  if go is None:
    pytest.skip('no Go toolchain')
  package = tmp_path / 'mod' / 'protos'
  package.mkdir(parents=True)
  (package / 'protos.go').write_text(source)
  (package / 'protos_test.go').write_text(
    'package protos\n\nimport "testing"\n\nfunc TestSources(t *testing.T) {\n'
    '\tif Sources["nested/long_file_name.proto"] != "syntax = \\"proto3\\";\\n// \\"\\u00e9\\"\\tx\\nmessage B {}\\n" {\n'
    '\t\tt.Fatal(Sources)\n\t}\n}\n'
  )
  (tmp_path / 'mod' / 'go.mod').write_text('module example.com/mod\n\ngo 1.23\n')
  gofmt = Path(go).with_name('gofmt')
  assert subprocess.run([str(gofmt), '-l', '.'], cwd=tmp_path / 'mod', capture_output=True, text=True).stdout == ''
  tested = subprocess.run([go, 'test', './...'], cwd=tmp_path / 'mod', capture_output=True, text=True)
  assert tested.returncode == 0, tested.stdout + tested.stderr


def test_a_root_segment_named_protos_is_suffixed():
  from truewire.codegen.go.endpoint import RESERVED_ROOT_DIRS
  assert 'protos' in RESERVED_ROOT_DIRS


def _union_page(title: str, row: dict, **extra: dict) -> dict:
  return {'title': title, 'type': 'object', 'description': 'A page.', 'required': ['list'], 'properties': {
    'list': {'type': 'array', 'description': 'Rows.', 'items': row}, **extra,
  }}


UNION_TOKEN = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/instruments', 'method': 'GET',
    'request': {'title': 'InstrumentsRequest', 'type': 'object', 'properties': {
      'category': {'type': 'string', 'description': 'Category.'},
      'cursor': {'type': 'string', 'description': 'Cursor.'},
    }},
    'response': {'description': 'One shape per category.', 'anyOf': [
      _union_page('SpotPage', {'title': 'SpotRow', 'type': 'object', 'properties': {'symbol': {'type': 'string', 'description': 'Symbol.'}}, 'required': ['symbol']}),
      _union_page(
        'FuturesPage', {'title': 'FuturesRow', 'type': 'object', 'properties': {'contract': {'type': 'string', 'description': 'Contract.'}}, 'required': ['contract']},
        nextPageCursor={'type': 'string', 'description': 'Next cursor.'},
      ),
    ]},
  },
  'pagination': {'strategy': 'token', 'cursor': {'parameter': 'cursor', 'from': 'nextPageCursor'}, 'done': {'kind': 'absent_cursor', 'rows': 'list'}},
}

UNION_WALK_TEST = '''package instruments

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"

	truewire "truewire.dev/core"
)

type categories struct{ seen []string }

func (c *categories) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	var request struct {
		Category string `json:"category"`
		Cursor   string `json:"cursor"`
	}
	if err := json.Unmarshal(call.Request, &request); err != nil {
		return nil, err
	}
	c.seen = append(c.seen, request.Cursor)
	switch {
	case request.Category == "spot":
		return json.RawMessage(`{"list":[{"symbol":"BTCUSDT"}]}`), nil
	case request.Cursor == "":
		return json.RawMessage(`{"list":[{"contract":"A"}],"nextPageCursor":"n"}`), nil
	}
	return json.RawMessage(`{"list":[{"contract":"B"}],"nextPageCursor":""}`), nil
}

func TestUnionWalkFollowsTheSetVariant(t *testing.T) {
	core := &categories{}
	category := "linear"
	rows, err := New(core).InstrumentsPaged(PagedRequest{Category: &category}).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || rows[1].FuturesRow == nil || rows[1].FuturesRow.Contract != "B" || fmt.Sprint(core.seen) != "[ n]" {
		t.Fatalf("rows %+v seen %q", rows, core.seen)
	}
	// A variant with no cursor ends the walk after its one page.
	core.seen = nil
	spot := "spot"
	rows, err = New(core).InstrumentsPaged(PagedRequest{Category: &spot}).All(context.Background())
	if err != nil || len(rows) != 1 || rows[0].SpotRow == nil || rows[0].SpotRow.Symbol != "BTCUSDT" || len(core.seen) != 1 {
		t.Fatalf("rows %+v seen %q %v", rows, core.seen, err)
	}
}
'''


def test_token_walk_over_a_union_payload_joins_every_variants_rows(tmp_path: Path):
  """bybit's `market.instruments` answers one shape per category: the rows are a `PagedRow`
  union of every variant's row type, read off whichever variant is set, and a variant
  without the cursor ends the walk (Python walks it the same way)."""
  root = _derived(tmp_path, {'market/instruments': UNION_TOKEN})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'instruments' in note], rendered.skipped
  source = rendered.files['market/instruments/instruments.go']
  assert '*truewire.PaginatedResponse[PagedRow, string]' in source
  assert 'out = append(out, PagedRow{FuturesRow: &variantRows[i]})' in source
  _go_tests({**rendered.files, 'market/instruments/walk_test.go': UNION_WALK_TEST}, tmp_path / 'go')


STRING_SIZE_PAGE = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/sweeps', 'method': 'GET',
    'request': {'title': 'SweepsRequest', 'type': 'object', 'properties': {
      'cursor': {'type': 'integer', 'description': 'Page number.'},
      'size': {'type': 'string', 'default': '50', 'description': 'Page size, as a string.'},
    }},
    'response': {'title': 'SweepsPage', 'type': 'object', 'properties': {
      'records': {'type': 'array', 'description': 'Rows.', 'items': {**_ROW, 'title': 'Sweep'}},
      'lastPage': {'type': 'string', 'description': 'Last page number.'},
    }, 'required': ['records', 'lastPage']},
  },
  'pagination': {'strategy': 'page', 'index': {'parameter': 'cursor'}, 'size': {'parameter': 'size'}, 'done': {'kind': 'total', 'path': 'lastPage', 'counts': 'pages', 'rows': 'records'}},
}


def test_page_walk_reads_a_string_size_and_a_string_total(tmp_path: Path):
  """bybit's `asset.convert_small_balance.history` sends its page size and last page as
  strings: both are read as numbers, and one that is not a number is unknown."""
  root = _derived(tmp_path, {'market/sweeps': STRING_SIZE_PAGE})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'sweeps' in note], rendered.skipped
  source = rendered.files['market/sweeps/sweeps.go']
  assert 'if n, err := strconv.Atoi(*request.Size); err == nil {' in source
  assert 'totalN, parseErr := strconv.ParseInt(total, 10, 64)' in source
  _vets(rendered.files, tmp_path / 'go')


TUPLE_SEEK = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/kline', 'method': 'GET',
    'request': {'title': 'KlineRequest', 'type': 'object', 'properties': {
      'start': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Millisecond timestamp.'},
      'end': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Millisecond timestamp.'},
      'limit': {'type': 'integer', 'maximum': 1000, 'default': 200, 'description': 'Page size.'},
    }},
    'response': {'title': 'KlineResult', 'type': 'object', 'required': ['list'], 'properties': {
      'list': {'type': 'array', 'description': 'Candles, newest first.', 'items': {
        'title': 'Candle', 'type': 'array', 'description': 'One candle.', 'prefixItems': [
          {'title': 'startTime', 'type': 'string', 'format': 'epoch-millis', 'description': 'Open time.'},
          {'title': 'close', 'type': 'string', 'description': 'Close price.'},
        ],
      }},
    }},
  },
  'pagination': {'strategy': 'seek', 'cursor': {'field': '[-1][0]', 'unique': True}, 'bound': {'start': 'start', 'end': 'end'}, 'anchor': 'end', 'size': {'parameter': 'limit'}, 'rows': 'list'},
}


def test_seek_walk_over_tuple_rows_names_the_rows_item_type(tmp_path: Path):
  """bybit's klines are positional arrays: the row is the hoisted item type of the rows
  field (`KlineResultListItem`), and the cursor `[-1][0]` reads its `V0`."""
  root = _derived(tmp_path, {'market/kline': TUPLE_SEEK})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'kline' in note], rendered.skipped
  source = rendered.files['market/kline/kline.go']
  assert '*truewire.PaginatedResponse[KlineResultListItem, truewire.SeekState[truewire.TimestampMillis, KlineResultListItem]]' in source
  assert '.V0' in source
  _vets(rendered.files, tmp_path / 'go')


def _renamed_composite_plan() -> PackagePlan:
  """Coinbase's shape: the root maps its composite child `app` to `app_client`, and `app`
  (`forward`) holds its own default `client` beside the forwarded `socket`."""
  return PackagePlan.model_validate({
    'name': 'p', 'rootClass': 'P',
    'cores': {'root': {'children': {'app': 'app_client'}}, 'app': {'forward': ['socket'], 'children': {'feed': 'socket'}}, 'plain': {}},
    'schemas': {},
    'routers': [
      {'path': [], 'core': 'root', 'children': [{'name': 'ping', 'kind': 'endpoint', 'class': 'Ping'}, {'name': 'app', 'kind': 'router', 'class': 'App'}]},
      {'path': ['app'], 'core': 'app', 'children': [{'name': 'feed', 'kind': 'router', 'class': 'Feed'}, {'name': 'status', 'kind': 'endpoint', 'class': 'Status'}]},
      {'path': ['app', 'feed'], 'core': 'plain', 'children': [{'name': 'ticks', 'kind': 'endpoint', 'class': 'Ticks'}]},
    ],
    'endpoints': [
      {'path': ['ping'], 'kind': 'rpc', 'transports': ['http'], 'wire': {'path': '/ping', 'method': 'GET'}, 'core': 'root', 'request': {'shape': 'none'}, 'response': {}},
      {'path': ['app', 'status'], 'kind': 'rpc', 'transports': ['http'], 'wire': {'path': '/status', 'method': 'GET'}, 'core': 'app', 'request': {'shape': 'none'}, 'response': {}},
      {'path': ['app', 'feed', 'ticks'], 'kind': 'stream', 'transports': ['ws'], 'wire': {'channel': 'ticks'}, 'core': 'plain', 'request': {'shape': 'none'}, 'response': {}, 'stream': {}},
    ],
  })


def test_a_composite_child_mapped_to_a_field_is_built_from_it_as_its_own_client():
  """`children = { app = "app_client" }` on a composite child: the root takes `appClient`
  and passes it where `app.New` takes its own `client`."""
  rendered = render_package(_renamed_composite_plan())
  root = rendered.files['client.go']
  assert 'func FromCores(appClient truewire.HttpEndpoint, client truewire.HttpEndpoint, socket truewire.StreamEndpoint) *P {' in root
  assert 'App: app.New(appClient, socket)' in root
  assert 'ping: ping.New(client)' in root
  assert 'func New(client truewire.HttpEndpoint, socket truewire.StreamEndpoint) *App {' in rendered.files['app/app.go']


def test_a_router_of_only_hand_written_endpoints_is_rendered_with_its_core(tmp_path: Path):
  """A router whose endpoints are all hand-written is still rendered, holding the contract a
  generated endpoint would, so the methods written beside it have a package and a transport,
  and its parent hands it that core."""
  from test_codegen_typescript import _hand_written_streams_project

  project = load_project(_hand_written_streams_project(tmp_path / 'venue'))
  rendered = render_package(build_plan(project), project)
  assert 'streams.market.trades: a hand-written surface (streams.market.trades:trades)' in rendered.skipped
  market = rendered.files['streams/market/market.go']
  assert 'type Market struct {' in market
  assert 'func New(core truewire.StreamEndpoint) *Market {' in market
  assert 'Market: market.New(core)' in rendered.files['streams/streams.go']
  assert 'streamClient truewire.StreamEndpoint' in rendered.files['client.go']
  _vets(rendered.files, tmp_path / 'go')


NULL_ONLY_FIELD = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/withdrawals', 'method': 'GET',
    'response': {'title': 'Withdrawal', 'type': 'object', 'properties': {
      'id': {'type': 'string', 'description': 'Withdrawal id.'},
      'confirmNo': {'type': 'null', 'description': 'Always null on the wire.'},
    }, 'required': ['id', 'confirmNo']},
  },
}


def test_a_required_null_only_field_accepts_null(tmp_path: Path):
  """A required key whose schema is `null` alone (mexc's `confirmNo`) is nullable: its record
  decodes the null the wire always sends instead of refusing it as a missing value."""
  root = _derived(tmp_path, {'market/withdrawals': NULL_ONLY_FIELD})
  project = load_project(root)
  rendered = render_package(build_plan(project), project)
  source = rendered.files['market/withdrawals/withdrawals.go']
  assert 'ConfirmNo any' in source
  assert 'RequiredNullable("confirmNo", &r.ConfirmNo)' in source
  assert 'Required("confirmNo"' not in source


TUPLE_LIST_SEEK = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/candles', 'method': 'GET',
    'request': {'title': 'CandlesRequest', 'type': 'object', 'properties': {
      'startTime': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Millisecond timestamp.'},
      'endTime': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Millisecond timestamp.'},
    }, 'required': ['startTime', 'endTime']},
    'response': {'title': 'Candles', 'type': 'array', 'description': 'Candles, oldest first.', 'items': {
      'type': 'array', 'description': 'One candle.', 'prefixItems': [
        {'title': 'openTime', 'type': 'integer', 'format': 'epoch-millis', 'description': 'Open time.'},
        {'title': 'close', 'type': 'number', 'description': 'Close price.'},
      ],
    }},
  },
  'pagination': {'strategy': 'seek', 'cursor': {'field': '[-1][0]', 'unique': True}, 'bound': {'start': 'startTime', 'end': 'endTime'}, 'anchor': 'start'},
}


def test_seek_walk_over_a_list_payload_of_tuples_names_the_hoisted_item(tmp_path: Path):
  """bit2me's candles (and binance's, kucoin's and mexc's klines) return the list itself, an
  inline tuple per row: the row is the item type the payload alias hoisted
  (`CandlesResponseItem`), not a twin that would not convert, and the cursor `[-1][0]` reads `V0`."""
  root = _derived(tmp_path, {'market/candles': TUPLE_LIST_SEEK})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'candles' in note], rendered.skipped
  source = rendered.files['market/candles/candles.go']
  assert 'type CandlesResponse = []CandlesResponseItem' in source
  assert '*truewire.PaginatedResponse[CandlesResponseItem, truewire.SeekState[truewire.TimestampMillis, CandlesResponseItem]]' in source
  assert 'return response, nil' in source
  assert '.V0' in source
  assert source.count('struct {\n\tV0 ') == 1
  _vets(rendered.files, tmp_path / 'go')


def test_hoisted_name_prefers_the_same_node_then_an_equal_one():
  tuple_a = {'type': 'tuple', 'items': [{'type': 'scalar', 'base': 'integer'}]}
  module = _module({'Rows': {'type': 'list', 'item': tuple_a}})
  assert module.hoisted_name(tuple_a) is None
  module.define_all(module.local)
  assert module.hoisted_name(tuple_a) == 'RowsItem'
  assert module.hoisted_name({'type': 'tuple', 'items': [{'type': 'scalar', 'base': 'integer'}]}) == 'RowsItem'
  assert module.hoisted_name({'type': 'tuple', 'items': [{'type': 'scalar', 'base': 'number'}]}) is None


TIMESTAMP_TOKEN = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/volatility', 'method': 'GET',
    'request': {'title': 'VolatilityRequest', 'type': 'object', 'properties': {
      'end_timestamp': {'type': 'integer', 'format': 'epoch-millis', 'description': 'Newest time.'},
    }, 'required': ['end_timestamp']},
    'response': {'title': 'Volatility', 'type': 'object', 'required': ['data'], 'properties': {
      'data': {'type': 'array', 'description': 'Rows.', 'items': {'title': 'Point', 'type': 'object', 'properties': {'v': {'type': 'number', 'description': 'Value.'}}, 'required': ['v']}},
      'continuation': {'anyOf': [{'type': 'integer', 'format': 'epoch-millis'}, {'type': 'null'}], 'description': 'Next `end_timestamp`.'},
    }},
  },
  'pagination': {'strategy': 'token', 'cursor': {'parameter': 'end_timestamp', 'from': 'continuation'}, 'done': {'kind': 'absent_cursor', 'rows': 'data'}},
}

NUMBER_TOTAL = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/prices', 'method': 'GET',
    'request': {'title': 'PricesRequest', 'type': 'object', 'properties': {
      'offset': {'type': 'integer', 'description': 'Offset.'},
      'count': {'type': 'integer', 'maximum': 1000, 'default': 10, 'description': 'Page size.'},
    }},
    'response': {'title': 'Prices', 'type': 'object', 'required': ['records_total', 'data'], 'properties': {
      'records_total': {'type': 'number', 'description': 'Rows in all.'},
      'data': {'type': 'array', 'description': 'Rows.', 'items': {'title': 'Price', 'type': 'object', 'properties': {'p': {'type': 'number', 'description': 'Price.'}}, 'required': ['p']}},
    }},
  },
  'pagination': {'strategy': 'offset', 'offset': {'parameter': 'offset'}, 'size': {'parameter': 'count'}, 'done': {'kind': 'total', 'path': 'records_total', 'counts': 'items', 'rows': 'data'}},
}

UNION_KEY_SEEK = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/historical', 'method': 'GET',
    'request': {'title': 'HistoricalRequest', 'type': 'object', 'properties': {
      'fromId': {'type': 'string', 'description': 'First trade id.'},
      'limit': {'type': 'integer', 'description': 'Page size.'},
    }},
    'response': {'title': 'Historical', 'type': 'array', 'description': 'Rows.', 'items': {
      'title': 'HistoricalTrade', 'type': 'object', 'properties': {'id': {'type': ['integer', 'string', 'null'], 'description': 'Trade id.'}}, 'required': ['id'],
    }},
  },
  'pagination': {'strategy': 'seek', 'cursor': {'field': '[-1].id', 'unique': True}, 'bound': {'start': 'fromId'}, 'anchor': 'start', 'size': {'parameter': 'limit'}},
}


def test_token_walk_over_a_timestamp_cursor_ends_on_the_zero_time(tmp_path: Path):
  """deribit's `get_volatility_index_data` sends back an `epoch-millis` `continuation` as the
  next `end_timestamp`: the state is the timestamp itself, and null or zero ends the walk."""
  root = _derived(tmp_path, {'market/volatility': TIMESTAMP_TOKEN})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'volatility' in note], rendered.skipped
  source = rendered.files['market/volatility/volatility.go']
  assert '*truewire.PaginatedResponse[Point, truewire.TimestampMillis]' in source
  assert 'if !found || following.IsZero() {' in source
  assert 'return truewire.NewPaginatedResponse(request.EndTimestamp, next)' in source
  _vets(rendered.files, tmp_path / 'go')


def test_offset_walk_reads_a_number_total(tmp_path: Path):
  """deribit's `records_total` is a JSON number: it still ends an offset walk."""
  root = _derived(tmp_path, {'market/prices': NUMBER_TOTAL})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'prices' in note], rendered.skipped
  source = rendered.files['market/prices/prices.go']
  assert 'total, found := func() (float64, bool) {' in source
  assert '>= int64(total)' in source
  _vets(rendered.files, tmp_path / 'go')


def test_seek_key_of_integer_or_string_reads_the_set_variant(tmp_path: Path):
  """mexc's trade `id` is an integer or a string: the seek key reads whichever is set, as
  the `fromId` string the next page starts from."""
  root = _derived(tmp_path, {'market/historical': UNION_KEY_SEEK})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'historical' in note], rendered.skipped
  source = rendered.files['market/historical/historical.go']
  assert 'strconv.FormatInt(' in source
  assert 'return zero, false' in source
  _vets(rendered.files, tmp_path / 'go')


def _summary_variant(title: str, row: str) -> dict:
  return {'title': title, 'type': 'object', 'required': ['data'], 'properties': {
    'data': {'type': 'array', 'description': 'Rows.', 'items': {'title': row, 'type': 'object', 'properties': {'id': {'type': 'string', 'description': 'Id.'}}, 'required': ['id']}},
  }}


UNION_PAGE = {
  'meta': {'public': True},
  'spec': {
    'kind': 'rpc', 'transports': ['http'], 'path': '/v1/summary', 'method': 'GET',
    'request': {'title': 'SummaryRequest', 'type': 'object', 'properties': {
      'kind': {'type': 'integer', 'enum': [1, 2], 'description': 'Which shape.'},
      'page': {'type': 'integer', 'description': 'Page number.'},
      'size': {'type': 'integer', 'default': 10, 'description': 'Page size.'},
    }, 'required': ['kind']},
    'response': {'anyOf': [_summary_variant('SummaryUsdm', 'UsdmRow'), _summary_variant('SummaryCoinm', 'CoinmRow')]},
  },
  'pagination': {'strategy': 'page', 'index': {'parameter': 'page', 'start': 1}, 'size': {'parameter': 'size'}, 'done': {'kind': 'short_page', 'rows': 'data'}},
}


def test_page_walk_over_a_union_payload_joins_every_variants_rows(tmp_path: Path):
  """binance's broker `sub_account_futures_summary_v2` answers one of two shapes by
  `futuresType`: a page walk joins whichever variant's rows arrived into one `PagedRow`."""
  root = _derived(tmp_path, {'market/summary': UNION_PAGE})
  rendered = render_package(build_plan(load_project(root)))
  assert not [note for note in rendered.skipped if 'summary' in note], rendered.skipped
  source = rendered.files['market/summary/summary.go']
  assert 'type PagedRow struct {' in source
  assert 'truewire.Exhausted(len(rows), size)' in source
  _vets(rendered.files, tmp_path / 'go')


def test_a_seek_size_under_a_maximum_below_2_is_the_maximum():
  """`min(max(size, 2), 1)` is 1: with no room for the floor the doc claims none, and an
  exclusive venue still walks."""
  from types import SimpleNamespace

  from truewire.codegen.go.endpoint import _seek_size, _seek_size_rule

  tiny = SimpleNamespace(size_maximum=1)
  assert _seek_size(tiny, 'request.Limit') == 'min(max(request.Limit, 2), 1)'  # type: ignore[arg-type]
  assert _seek_size_rule(tiny) == 'The walk requests pages of 1 row.'  # type: ignore[arg-type]
