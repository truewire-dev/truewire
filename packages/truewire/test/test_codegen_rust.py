"""The Rust backend (`truewire generate rust`): the plan rendered as the modules of a
crate over `truewire-core`: `serde` structs, enums, endpoint structs and delegating routers.

Three things are proved here. The renderers decide Rust's part of the plan the documented
way (`docs/rust.md`: scalar formats to newtypes, `Option`/`double_option`, hoisted enums,
boxed cycles, the naming rule, `rustfmt`-shaped output). The whole package renders every
walker shape the fixture client declares and reports what it skips. And the CLI keeps the
same manifest discipline as the other backends (`--check` reports drift, `--delete`
removes only what it owns).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.rust import render_package, root_struct_name
from truewire.codegen.rust.endpoint import read_path, tokenize
from truewire.codegen.rust.meta import meta_module, meta_type, meta_value
from truewire.codegen.rust.names import literal, pascal_ident, snake_ident, string, unique
from truewire.codegen.rust.printer import BANNER, Imports, Writer
from truewire.codegen.rust.types import Module, scope_file
from truewire.plan.build import build_plan
from truewire.plan.model import CorePlan, PackagePlan
from truewire.project import load_project

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'codegen_fixture_client'
EXAMPLES = Path(__file__).parents[3] / 'examples'


def _example(name: str) -> Path:
  root = EXAMPLES / name
  if not (root / 'truewire.toml').is_file():
    pytest.skip(f'examples/{name} is not checked out beside the package')
  return root


def _with_rust(source: Path, target: Path) -> Path:
  """A copy of a project with a `[rust]` section added."""
  shutil.copytree(source, target, ignore=shutil.ignore_patterns('node_modules', '.truewire', 'src', 'target'))
  toml = target / 'truewire.toml'
  toml.write_text(toml.read_text() + '\n[rust]\npackage = "client"\nsrc = "rust"\nname = "Client"\n')
  return target


# -- naming and printing --------------------------------------------------------------


def test_naming_rule():
  """Fields and modules are snake_case, types and variants PascalCase, keywords suffixed."""
  assert snake_ident('htmlUrl') == 'html_url'
  assert snake_ident('HTMLParser') == 'html_parser'
  assert snake_ident('X-Rate') == 'x_rate'
  assert snake_ident('per_page') == 'per_page'
  assert snake_ident('2fa') == '_2fa'
  assert snake_ident('type') == 'type_'
  assert snake_ident('') == 'field'
  assert pascal_ident('not_planned') == 'NotPlanned'
  assert pascal_ident('FIRST_TIMER') == 'FirstTimer'
  assert pascal_ident('1h') == 'N1h'
  assert unique('a', {'a', 'a2'}) == 'a3'
  assert string('it\'s "x"') == '"it\'s \\"x\\""'
  assert literal(1.0) == '1.0' and literal(True) == 'true' and literal('s') == '"s"'


def test_printer_follows_rustfmt():
  """The printer reproduces `rustfmt`'s decisions: import order and packing, struct
  literal and attribute widths, signature breaking."""
  imports = Imports()
  imports.add('truewire_core', 'decode')
  imports.add('truewire_core', 'CallOptions')
  imports.add('truewire_core', 'DATE')
  imports.add('truewire_core::paging', 'exhausted')
  imports.add('std::sync', 'Arc')
  imports.add('crate::types', 'Label')
  imports.add('crate::types::futures', 'Side')
  assert imports.render() == [
    'use std::sync::Arc;',
    '',
    'use truewire_core::paging::exhausted;',
    'use truewire_core::{decode, CallOptions, DATE};',
    '',
    'use crate::types::futures::Side;',
    'use crate::types::Label;',
  ]
  wide = Imports()
  for name in ('decode', 'dump', 'serde_json', 'CallOptions', 'HttpCall', 'HttpEndpoint', 'PaginatedResponse', 'Result', 'TimestampIso'):
    wide.add('truewire_core', name)
  assert '\n'.join(wide.render()) == (
    'use truewire_core::{\n'
    '    decode, dump, serde_json, CallOptions, HttpCall, HttpEndpoint, PaginatedResponse, Result,\n'
    '    TimestampIso,\n'
    '};'
  )
  w = Writer()
  w.struct_literal('', 'Self', ['core'])
  w.struct_literal('let meta = ', 'DefaultMeta', ['public: Some(true)'], ';')
  w.struct_literal('let meta = ', 'DefaultMeta', ['public: Some(true)', 'signed: None'], ';')
  w.attribute('serde', ['default', 'skip_serializing_if = "Option::is_none"'])
  w.attribute('serde', ['default', 'skip_serializing_if = "Option::is_none"', 'with = "truewire_core::validation::double_option"'])
  w.signature('pub async fn get', ['&self', 'request: Request', 'options: CallOptions'], ' -> Result<Repository> {')
  w.signature('pub async fn get_raw', ['&self', 'request: SomeLongRequestTypeName', 'options: CallOptions'], ' -> Result<serde_json::Value> {')
  w.chain('let raw = ', 'self', ['.get_raw(request, options)', '.await?'], ';')
  w.chain('', 'self', ['.order_nullable_list', '.order_nullable_list_paged(request, options)'])
  w.chain('let a = ', 'response', ['.as_ref()', '.and_then(|value| value.page_key.as_ref())', '.cloned()'], ';')
  assert w.render() == (
    'Self { core }\n'
    'let meta = DefaultMeta { public: Some(true) };\n'
    'let meta = DefaultMeta {\n    public: Some(true),\n    signed: None,\n};\n'
    '#[serde(default, skip_serializing_if = "Option::is_none")]\n'
    '#[serde(\n    default,\n    skip_serializing_if = "Option::is_none",\n'
    '    with = "truewire_core::validation::double_option"\n)]\n'
    'pub async fn get(&self, request: Request, options: CallOptions) -> Result<Repository> {\n'
    'pub async fn get_raw(\n    &self,\n    request: SomeLongRequestTypeName,\n    options: CallOptions,\n'
    ') -> Result<serde_json::Value> {\n'
    'let raw = self.get_raw(request, options).await?;\n'
    'self.order_nullable_list\n    .order_nullable_list_paged(request, options)\n'
    'let a = response\n    .as_ref()\n    .and_then(|value| value.page_key.as_ref())\n    .cloned();\n'
  )


# -- types --------------------------------------------------------------------------------


def _module(types: dict, schemas: dict | None = None) -> Module:
  plan = PackagePlan(name='p', root_class='P', schemas=schemas or {})
  return Module(plan, 'x/y.rs', local=types, visible=[''])


def test_scalar_formats_render_to_core_newtypes():
  m = _module({})
  cases = {
    ('string', None): 'String', ('integer', None): 'i64', ('number', None): 'f64',
    ('boolean', None): 'bool', ('null', None): '()', ('any', None): 'serde_json::Value',
    ('string', 'decimal-string'): 'DecimalString', ('string', 'integer-string'): 'IntegerString',
    ('string', 'boolean-string'): 'BooleanString', ('integer', 'epoch-millis'): 'TimestampMillis',
    ('integer', 'epoch-seconds'): 'TimestampSeconds', ('string', 'date-time'): 'TimestampIso',
    ('string', 'date'): 'DateIso', ('string', 'uuid'): 'String',
  }
  for (base, fmt), expected in cases.items():
    t = {'type': 'scalar', 'base': base, **({'format': fmt} if fmt else {})}
    assert m.type_expr(t) == expected, (base, fmt)
  assert m.type_expr({'type': 'list', 'item': {'type': 'scalar', 'base': 'string'}}) == 'Vec<String>'
  assert m.type_expr({'type': 'tuple', 'items': [{'type': 'scalar', 'base': 'string'}]}) == '(String,)'
  assert m.type_expr({'type': 'dict', 'key': {'type': 'scalar', 'base': 'string'}, 'value': {'type': 'scalar', 'base': 'integer'}}) == 'HashMap<String, i64>'
  rendered = '\n'.join(m.imports.render())
  assert 'use std::collections::HashMap;' in rendered
  assert 'DecimalString' in rendered and 'TimestampIso' in rendered and 'serde_json' in rendered


def test_records_render_serde_structs_with_options_and_double_option():
  """Optional -> `Option` with `default`; nullable -> `Option`; both -> `Option<Option>`
  behind `double_option`; snake_case with `rename`; a flattened `extra` map; `Default`
  only when every required field has one."""
  m = _module({
    'Label': {'type': 'record', 'id': 'Label', 'docstring': 'A label.', 'fields': {
      'id': {'type': {'type': 'scalar', 'base': 'integer'}, 'required': True, 'docstring': 'Label id.'},
      'htmlUrl': {'type': {'type': 'scalar', 'base': 'string'}, 'required': False},
      'color': {'type': {'type': 'union', 'variants': [{'type': {'type': 'scalar', 'base': 'string'}}, {'type': {'type': 'scalar', 'base': 'null'}}]}, 'required': True},
      'description': {'type': {'type': 'union', 'variants': [{'type': {'type': 'scalar', 'base': 'string'}}, {'type': {'type': 'scalar', 'base': 'null'}}]}, 'required': False},
      'type': {'type': {'type': 'scalar', 'base': 'string'}, 'required': True},
      'extra': {'type': {'type': 'scalar', 'base': 'string'}, 'required': True},
    }},
    'Stamped': {'type': 'record', 'id': 'Stamped', 'fields': {
      'at': {'type': {'type': 'scalar', 'base': 'string', 'format': 'date-time'}, 'required': True},
    }},
  })
  m.define_all(m.local)
  out = m.writer.render()
  assert '/// A label.\n#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]\npub struct Label {' in out
  assert '    /// Label id.\n    pub id: i64,\n' in out
  assert '    #[serde(rename = "htmlUrl", default, skip_serializing_if = "Option::is_none")]\n    pub html_url: Option<String>,\n' in out
  assert '    pub color: Option<String>,\n' in out
  assert (
    '    #[serde(default, skip_serializing_if = "Option::is_none")]\n'
    '    #[serde(with = "truewire_core::validation::double_option")]\n'
    '    pub description: Option<Option<String>>,\n'
  ) in out
  assert '    #[serde(rename = "type")]\n    pub type_: String,\n' in out
  assert '    #[serde(rename = "extra")]\n    pub extra2: String,\n' in out
  assert '    #[serde(flatten)]\n    pub extra: serde_json::Map<String, serde_json::Value>,\n}' in out
  assert '#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]\npub struct Stamped {' in out


def test_inline_literals_and_unions_are_hoisted_and_cycles_boxed():
  m = _module({
    'Node': {'type': 'record', 'id': 'Node', 'fields': {
      'kind': {'type': {'type': 'literal', 'values': ['leaf', 'branch']}, 'required': True},
      'level': {'type': {'type': 'literal', 'values': [1, 2]}, 'required': True},
      'parent': {'type': {'type': 'union', 'variants': [{'type': {'type': 'ref', 'id': 'Node'}}, {'type': {'type': 'scalar', 'base': 'null'}}]}, 'required': False},
      'children': {'type': {'type': 'list', 'item': {'type': 'ref', 'id': 'Node'}}, 'required': True},
      'value': {'type': {'type': 'union', 'variants': [{'type': {'type': 'scalar', 'base': 'string'}, 'docstring': 'Text.'}, {'type': {'type': 'ref', 'id': 'Pair'}}]}, 'required': True},
    }},
    'Pair': {'type': 'tuple', 'items': [{'type': 'scalar', 'base': 'string', 'format': 'decimal-string'}, {'type': 'ref', 'id': 'Node'}]},
    'Side': {'type': 'literal', 'values': ['buy', 'sell'], 'docstring': 'Which side.'},
    'Maybe': {'type': 'union', 'variants': [{'type': {'type': 'ref', 'id': 'Pair'}}, {'type': {'type': 'scalar', 'base': 'integer'}}, {'type': {'type': 'scalar', 'base': 'null'}}]},
  })
  m.define_all(m.local)
  out = m.writer.render()
  assert 'pub enum NodeKind {\n    #[serde(rename = "leaf")]\n    Leaf,\n    #[serde(rename = "branch")]\n    Branch,\n}' in out
  assert out.index('pub enum NodeKind') < out.index('pub struct Node')
  assert '    pub kind: NodeKind,\n    pub level: i64,\n' in out
  assert '    pub parent: Option<Option<Box<Node>>>,\n' in out
  assert '    pub children: Vec<Node>,\n' in out
  assert '#[serde(untagged)]\npub enum NodeValue {\n    /// Text.\n    String(String),\n    Pair(Box<Pair>),\n}' in out
  assert '    pub value: NodeValue,\n' in out
  assert 'pub type Pair = (DecimalString, Node);' in out
  assert '/// Which side.\n#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]\npub enum Side {' in out
  assert 'pub enum MaybeValue {\n    Pair(Pair),\n    Integer(i64),\n}' in out
  assert 'pub type Maybe = Option<MaybeValue>;' in out
  assert not m.defaultable(m.local['Node']) and m.copyable(m.local['Side']) and not m.copyable(m.local['Node'])


def test_shared_references_are_imported_from_their_scope():
  shared = {'': {'Label': {'type': 'record', 'id': 'Label', 'fields': {}}}, 'futures': {'Side': {'type': 'literal', 'values': ['long']}}}
  m = _module({'Row': {'type': 'record', 'id': 'Row', 'fields': {
    'label': {'type': {'type': 'ref', 'id': 'Label'}, 'required': True},
    'side': {'type': {'type': 'ref', 'id': 'Side'}, 'required': True},
  }}}, shared)
  m.define_all(m.local)
  assert m.imports.render()[-2:] == ['use crate::types::futures::Side;', 'use crate::types::Label;']
  assert tokenize(m, 'Vec<Label>') == [('text', 'Vec<'), ('shared', 'crate::types::Label'), ('text', '>')]
  assert tokenize(m, 'Option<Row>') == [('text', 'Option<'), ('local', 'Row'), ('text', '>')]
  plan = PackagePlan(name='p', root_class='P', schemas={'': {}, 'a/b': {}, 'a': {}})
  assert scope_file(plan, '') == 'types/mod.rs'
  assert scope_file(plan, 'a') == 'types/a/mod.rs'
  assert scope_file(plan, 'a/b') == 'types/a/b.rs'


def test_response_paths_read_through_options():
  """A path is a method chain: owned hops move (the rows' last use), borrowed hops go
  through `as_ref` and clone or copy the leaf."""
  m = _module({
    'Page': {'type': 'record', 'id': 'Page', 'fields': {
      'rows': {'type': {'type': 'list', 'item': {'type': 'scalar', 'base': 'string'}}, 'required': True},
      'meta': {'type': {'type': 'ref', 'id': 'Meta'}, 'required': False},
      'cursor': {'type': {'type': 'union', 'variants': [{'type': {'type': 'scalar', 'base': 'string'}}, {'type': {'type': 'scalar', 'base': 'null'}}]}, 'required': False},
    }},
    'Meta': {'type': 'record', 'id': 'Meta', 'fields': {'total': {'type': {'type': 'scalar', 'base': 'integer'}, 'required': True}}},
    'Response': {'type': 'union', 'variants': [{'type': {'type': 'ref', 'id': 'Page'}}, {'type': {'type': 'scalar', 'base': 'null'}}]},
  })
  m.define_all(m.local)
  page = {'type': 'ref', 'id': 'Page'}
  assert read_path(m, 'response', page, 'rows', borrow=False).elements == ['.rows']
  assert read_path(m, 'response', page, 'meta.total', borrow=True).elements == ['.meta', '.as_ref()', '.map(|value| &value.total)', '.copied()']
  assert read_path(m, 'response', page, 'cursor', borrow=True).elements == ['.cursor', '.flatten()', '.clone()']
  nullable = read_path(m, 'response', {'type': 'ref', 'id': 'Response'}, 'rows', borrow=False)
  assert nullable.elements == ['.map(|value| value.rows)'] and nullable.optional
  assert read_path(m, 'response', {'type': 'ref', 'id': 'Response'}, 'meta.total', borrow=True).elements == [
    '.as_ref()', '.and_then(|value| value.meta.as_ref())', '.map(|value| &value.total)', '.copied()',
  ]


def test_meta_module_renders_one_struct_per_core_with_a_schema():
  assert meta_module({'chain': CorePlan()}) is None
  cores = {
    'default': CorePlan(meta={'type': 'object', 'properties': {'public': {'type': 'boolean', 'description': 'No auth.'}}}),
    'spot': CorePlan(meta={'type': 'object', 'required': ['scope'], 'properties': {
      'scope': {'type': 'string', 'enum': ['read', 'trade']}, 'weight': {'type': 'integer'}, 'tags': {'type': 'array', 'items': {'type': 'string'}}, 'extra': {'type': 'object'},
    }}),
    'chain': CorePlan(),
  }
  out = meta_module(cores)
  assert out is not None and out.startswith(BANNER)
  assert 'pub struct DefaultMeta {\n    /// No auth.\n    pub public: Option<bool>,\n}' in out
  assert 'pub struct SpotMeta {\n    pub scope: String,\n    pub weight: Option<i64>,\n    pub tags: Option<Vec<String>>,\n    pub extra: Option<serde_json::Value>,\n}' in out
  assert 'use truewire_core::serde_json;' in out
  assert meta_type({'type': ['string', 'null']}) == 'serde_json::Value'
  assert meta_value('String', 'x') == '"x".to_string()'
  assert meta_value('Vec<i64>', [1, 2]) == 'vec![1, 2]'
  assert meta_value('serde_json::Value', {'a': 1}) == 'serde_json::json!({"a": 1})'


# -- the whole package --------------------------------------------------------------------


@pytest.fixture(scope='module')
def fixture_rendered():
  return render_package(build_plan(FIXTURE_ROOT))


def test_fixture_package_layout(fixture_rendered):
  files = fixture_rendered.files
  assert 'lib.rs' in files and 'client.rs' in files and 'meta.rs' in files
  assert 'types/mod.rs' in files and 'types/futures.rs' in files
  assert 'market/mod.rs' in files and 'market/order_list.rs' in files
  assert 'token/nfts/list.rs' in files and 'token/nfts/mod.rs' in files
  assert 'market/ticker_stream.rs' not in files
  assert all(content.startswith(BANNER + '\n') for content in files.values())
  assert fixture_rendered.skipped == ['market.ticker_stream: a stream endpoint has no Rust rendering yet']
  assert 'pub struct FixtureClient' in files['client.rs']
  assert files['lib.rs'].split('\n\n')[1] == (
    'pub mod account;\npub mod client;\npub mod core;\npub mod futures;\npub mod market;\n'
    'pub mod meta;\npub mod mixed_dir;\npub mod token;\npub mod types;'
  )
  assert 'pub use client::FixtureClient;\npub use truewire_core::CallOptions;' in files['lib.rs']
  assert 'pub mod futures;' in files['types/mod.rs']


def test_fixture_endpoints_hold_the_core_behind_the_contract(fixture_rendered):
  """A method dumps the request, hands the core an `HttpCall` with its declared `meta`,
  and decodes the reply; the `_raw` twin returns the value the core returned; a fixed
  field is filled in after dumping; a core with no `meta` schema takes the unit meta."""
  files = fixture_rendered.files
  order = files['market/order.rs']
  assert 'pub struct Order {\n    core: Arc<dyn HttpEndpoint<DefaultMeta>>,\n}' in order
  assert 'pub async fn order(&self, request: Request, options: CallOptions) -> Result<OrderAck> {\n        let raw = self.order_raw(request, options).await?;\n        decode(raw)\n    }' in order
  assert '    ) -> Result<serde_json::Value> {\n        let meta = DefaultMeta {\n            signed: Some(true),\n            public: None,\n        };' in order
  assert 'request: Some(dump(&request)?),\n            meta: &meta,\n            options,\n        };\n        self.core.request(call).await' in order
  dispatch = files['market/order_dispatch.rs']
  assert 'object.insert("action".to_string(), serde_json::json!("list"));' in dispatch
  assert 'pub action' not in dispatch
  chain = files['token/balances/get.rs']
  assert 'core: Arc<dyn HttpEndpoint>,' in chain and 'meta: &(),' in chain
  submit = files['market/order_submit.rs']
  assert '#[serde(untagged)]\npub enum Request {\n    LimitOrderRequest(LimitOrderRequest),\n    MarketOrderRequest(MarketOrderRequest),\n}' in submit
  leverage = files['futures/leverage.rs']
  assert 'let meta = FuturesMeta {\n            signed: false,\n            public: Some(true),\n        };' in leverage


def test_fixture_walkers_render_every_resumable_shape(fixture_rendered):
  files = fixture_rendered.files
  token = files['market/order_list.rs']
  assert 'pub fn order_list_paged(\n        &self,\n        request: OrderListPagedRequest,\n        options: CallOptions,\n    ) -> PaginatedResponse<OrderListItem, String> {' in token
  assert 'pub struct OrderListPagedRequest {' in token and 'pub page_key' not in token.split('pub struct OrderListPagedRequest {')[1].split('}')[0]
  assert 'fn at(&self, page_key: Option<String>) -> Request {' in token
  assert 'let page_key = cursor_or_done(Some(page_key));\n                let request = request.at(page_key);' in token
  assert 'let cursor = response.page_key.clone();\n                let rows = response.orders;\n                Ok((rows, cursor_or_done(cursor)))' in token
  assert 'PaginatedResponse::new(String::new(), next)' in token
  nullable = files['market/order_nullable_list.rs']
  assert '.and_then(|value| value.page_key.as_ref())\n                    .cloned();' in nullable
  assert 'let rows = response.map(|value| value.orders).unwrap_or_default();' in nullable
  total = files['market/order_page_total.rs']
  assert 'let total_seen = Arc::new(Mutex::new(TotalSeen::new()));' in total
  assert 'seen.check(walker, Some(total))?' in total
  assert 'if total_reached(false, current, 1, size, rows.len(), total) {' in total
  assert 'PaginatedResponse::new(1, next)' in total
  shared = files['market/order_ledger.rs']
  assert 'use crate::types::{OrderLedgerEntry, OrderLedgerResponse};' in shared
  assert ') -> PaginatedResponse<OrderLedgerEntry, String> {' in shared


def test_fixture_routers_delegate_with_qualified_types(fixture_rendered):
  files = fixture_rendered.files
  market = files['market/mod.rs']
  assert market.startswith(BANNER + '\n\npub mod order;\n')
  assert 'pub struct Market {\n    order: order::Order,\n' in market
  assert 'pub fn new(core: Arc<dyn HttpEndpoint<DefaultMeta>>) -> Self {' in market
  assert 'order: order::Order::new(core.clone()),' in market
  assert '    ) -> Result<order::OrderAck> {\n        self.order.order(request, options).await\n    }' in market
  assert 'pub async fn orderbook(&self, request: orderbook::Request, options: CallOptions) -> Result<orderbook::Orderbook> {' not in market
  assert ') -> PaginatedResponse<order_list::OrderListItem, String> {\n        self.order_list.order_list_paged(request, options)\n    }' in market
  assert ') -> Result<OrderSide> {\n        self.order_last_side.order_last_side(request, options).await' in market
  root = files['client.rs']
  assert 'pub fn new<C>(core: Arc<C>) -> Self\n    where\n        C: HttpEndpoint + HttpEndpoint<DefaultMeta> + HttpEndpoint<FuturesMeta> + \'static,\n    {' in root
  assert '    pub market: Market,\n' in root and 'token: Token::new(core),' in root
  account = files['account/mod.rs']
  assert 'pub deposits: deposits::Deposits,' in account and 'pub mod deposits;\npub mod withdrawals;' in account


def test_a_composite_root_renders_and_ws_only_endpoints_are_skipped():
  """A router under a composite core takes one parameter per declared field, so the
  subtrees that *can* render do, and only the endpoints that cannot are left out.

  Kraken's root is composite (`spot` on HTTP, `streams`/`trading_ws` on a socket). Before
  the composite constructor existed the whole client vanished behind one skip; now the
  HTTP half is a real client and the socket half is skipped endpoint by endpoint, which
  is what the skip list should have said all along.
  """
  root = _example('kraken')
  rendered = render_package(build_plan(root))
  notes = '\n'.join(rendered.skipped)
  assert 'a router under a composite core' not in notes
  assert 'a stream endpoint has no Rust rendering yet' in notes
  assert 'an rpc endpoint over a WebSocket has no Rust rendering yet' in notes

  # The HTTP subtree is rendered and reachable from the root.
  assert 'client.rs' in rendered.files and 'spot/mod.rs' in rendered.files
  assert 'pub use client::' in rendered.files['lib.rs']
  # Nothing that could not render is declared: a module `lib.rs` names but never wrote
  # would not compile.
  assert not any(path.startswith('streams/') or path.startswith('trading_ws/') for path in rendered.files)
  assert 'pub mod streams;' not in rendered.files['lib.rs']

  # The root's own constructor names the field its HTTP children are handed, and not the
  # socket field, which nothing rendered can be built from.
  client = rendered.files['client.rs']
  assert 'pub fn new(spot_client: Arc<dyn HttpEndpoint<SpotMeta>>) -> Self {' in client or \
    'pub fn new(client: Arc<dyn HttpEndpoint<SpotMeta>>) -> Self {' in client, client
  assert 'Arc<dyn >' not in client


def test_root_struct_name_prefers_the_rust_section(tmp_path: Path):
  root = _with_rust(FIXTURE_ROOT, tmp_path / 'client')
  project = load_project(root)
  plan = build_plan(project)
  assert root_struct_name(plan, None) == plan.root_class
  assert root_struct_name(plan, project) == 'Client'


def test_github_example_output_is_what_the_backend_renders():
  """The committed `examples/github/src/github/**/*.rs` is the backend's current output;
  a change to either side shows up here before CI's `generate rust --check`."""
  root = _example('github')
  project = load_project(root)
  if project.rust is None:
    pytest.skip('examples/github declares no [rust] section')
  rendered = render_package(build_plan(project), project)
  package = project.rust_package_dir
  manifest_path = root / '.truewire' / 'rust-files.json'
  manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
  for path, content in rendered.files.items():
    assert (package / path).read_text() == content, path
  if manifest is not None:
    assert sorted(manifest['files']) == sorted(rendered.files)
  assert rendered.skipped == []
  assert ') -> PaginatedResponse<Commit, i64> {' in rendered.files['repos/list_commits.rs']


@pytest.mark.skipif(shutil.which('rustfmt') is None, reason='rustfmt is not installed')
def test_rendered_output_satisfies_rustfmt(fixture_rendered, tmp_path: Path):
  """No formatter runs over the output, so the printer's own layout must be what
  `rustfmt` produces: every file of the fixture package passes `rustfmt --check`."""
  for path, content in fixture_rendered.files.items():
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
  (tmp_path / 'core.rs').write_text('// The hand-written core, stubbed for the check.\n')
  for path in fixture_rendered.files:
    checked = subprocess.run(
      ['rustfmt', '--edition', '2021', '--check', str(tmp_path / path)], capture_output=True, text=True,
    )
    assert checked.returncode == 0, f'{path}:\n{checked.stdout}{checked.stderr}'


# -- the CLI ------------------------------------------------------------------------------


def test_generate_rust_writes_checks_and_deletes(tmp_path: Path):
  root = _with_rust(FIXTURE_ROOT, tmp_path / 'client')
  runner = CliRunner()
  result = runner.invoke(app, ['generate', 'rust', '--project', str(root)])
  assert result.exit_code == 0, result.output
  assert 'skipped market.ticker_stream' in result.output
  manifest = root / '.truewire' / 'rust-files.json'
  assert manifest.is_file()
  files = json.loads(manifest.read_text())['files']
  assert 'lib.rs' in files and 'market/order_list.rs' in files
  package = root / 'rust' / 'client'
  assert (package / 'lib.rs').read_text().startswith(BANNER)
  assert 'pub struct Client' in (package / 'client.rs').read_text()

  check = runner.invoke(app, ['generate', 'rust', '--project', str(root), '--check'])
  assert check.exit_code == 0, check.output

  (package / 'client.rs').write_text('// edited\n')
  drifted = runner.invoke(app, ['generate', 'rust', '--project', str(root), '--check'])
  assert drifted.exit_code == 1
  assert 'out of date: client.rs' in drifted.output

  (package / 'core.rs').write_text('pub struct Core;\n')
  deleted = runner.invoke(app, ['generate', 'rust', '--project', str(root), '--delete'])
  assert deleted.exit_code == 0, deleted.output
  assert not (package / 'client.rs').exists()
  assert (package / 'core.rs').exists()
  assert not manifest.exists()


def test_generate_rust_needs_a_rust_section(tmp_path: Path):
  shutil.copytree(FIXTURE_ROOT, tmp_path / 'client', ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  result = CliRunner().invoke(app, ['generate', 'rust', '--project', str(tmp_path / 'client')])
  assert result.exit_code == 1
  assert 'no [rust] section' in result.output
