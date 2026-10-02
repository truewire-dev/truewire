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
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.rust import render_package, root_struct_name
from truewire.codegen.rust.endpoint import _as_total, _Skipped, read_path, tokenize
from truewire.codegen.rust.meta import meta_module, meta_type, meta_value
from truewire.codegen.rust.names import literal, pascal_ident, snake_ident, string, unique
from truewire.codegen.rust.printer import BANNER, INDENT, Imports, Writer
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


def test_a_plan_type_name_that_is_not_upper_camel_case_is_allowed_not_renamed():
  """A spec `title` (`annotation`) names the type verbatim in every backend and in every
  reference to it; the Rust definition keeps it and silences `non_camel_case_types`."""
  m = _module({
    'annotation': {'type': 'record', 'id': 'annotation', 'fields': {
      'category': {'type': {'type': 'scalar', 'base': 'string'}, 'required': True},
    }},
    'side_kind': {'type': 'literal', 'values': ['A', 'B']},
    'Label': {'type': 'record', 'id': 'Label', 'fields': {
      'kind': {'type': {'type': 'ref', 'id': 'side_kind'}, 'required': True},
    }},
  })
  m.define_all(m.local)
  out = m.writer.render()
  assert '#[allow(non_camel_case_types)]\n#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]\npub struct annotation {' in out
  assert '#[allow(non_camel_case_types)]\n#[derive(' in out.split('pub enum side_kind')[0].rsplit('\n\n', 1)[-1]
  assert 'pub kind: side_kind,' in out
  assert '#[allow(non_camel_case_types)]\n#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]\npub struct Label' not in out


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


def test_a_total_reads_as_i64_whatever_json_base_its_integer_string_has():
  """dYdX Comet's `total_count` is `{type: string, format: integer-string}`: it clamps like an
  integer-based one; a plain string parses; anything else has no walker."""
  m = _module({'Page': {'type': 'record', 'id': 'Page', 'fields': {
    'wide': {'type': {'type': 'scalar', 'base': 'string', 'format': 'integer-string'}, 'required': True},
    'wide_int': {'type': {'type': 'scalar', 'base': 'integer', 'format': 'integer-string'}, 'required': True},
    'text': {'type': {'type': 'scalar', 'base': 'string'}, 'required': True},
    'flag': {'type': {'type': 'scalar', 'base': 'boolean'}, 'required': True},
  }}})
  m.define_all(m.local)
  page = {'type': 'ref', 'id': 'Page'}
  for field in ('wide', 'wide_int'):
    total = _as_total(m, read_path(m, 'response', page, field, borrow=True))
    assert total.elements[-1] == '.saturating_i64()' and not total.optional
  assert _as_total(m, read_path(m, 'response', page, 'text', borrow=True)).elements[-2:] == ['.parse::<i64>().ok()', '.unwrap_or(i64::MAX)']
  with pytest.raises(_Skipped):
    _as_total(m, read_path(m, 'response', page, 'flag', borrow=True))


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
  assert 'market/ticker_stream.rs' in files
  assert all(content.startswith(BANNER + '\n') for content in files.values())
  assert fixture_rendered.skipped == []
  assert 'pub struct FixtureClient' in files['client.rs']
  assert files['lib.rs'].split('\n\n')[1] == (
    'pub mod account;\npub mod client;\npub mod contract;\npub mod core;\nmod dispatch;\npub mod futures;\npub mod market;\n'
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
  # ADR 0013: `total` only decides when to stop; no state is kept outside `next`'s argument.
  assert 'TotalSeen' not in total and 'Mutex' not in total
  assert 'if rows.is_empty() || total_reached(false, current, 1, size, rows.len(), total) {' in total
  assert 'PaginatedResponse::new(1, next)' in total
  shared = files['market/order_ledger.rs']
  assert 'use crate::types::{OrderLedgerEntry, OrderLedgerResponse};' in shared
  assert ') -> PaginatedResponse<OrderLedgerEntry, String> {' in shared


def test_a_token_walk_ending_on_an_empty_page_stops_there():
  """`done: empty` on a `token` walk: an empty page ends the walk even when it carries a
  cursor; an `absent_cursor` walk keeps reading the cursor alone."""
  plan = build_plan(FIXTURE_ROOT)
  endpoints = []
  for endpoint in plan.endpoints:
    if endpoint.function == 'market.order_list':
      pagination = endpoint.pagination.model_copy(update={'done': {'kind': 'empty', 'rows': 'orders'}})
      endpoint = endpoint.model_copy(update={'pagination': pagination})
    endpoints.append(endpoint)
  rendered = render_package(plan.model_copy(update={'endpoints': endpoints}))
  token = rendered.files['market/order_list.rs']
  assert (
    'let rows = response.orders;\n'
    '                if rows.is_empty() {\n'
    '                    return Ok((rows, None));\n'
    '                }\n'
    '                Ok((rows, cursor_or_done(cursor)))'
  ) in token
  assert 'rows.is_empty()' not in render_package(plan).files['market/order_list.rs']


def test_a_page_walk_ending_on_an_empty_page_ignores_short_pages():
  """`done: empty` on a `page` walk (dYdX's indexer fills, funding payments, transfers): only
  a page with no rows ends it, a short page does not, so the size is never read."""
  plan = build_plan(FIXTURE_ROOT)
  endpoints = []
  for endpoint in plan.endpoints:
    if endpoint.function == 'market.order_page_total':
      pagination = endpoint.pagination.model_copy(update={'done': {'kind': 'empty', 'rows': 'rows'}})
      endpoint = endpoint.model_copy(update={'pagination': pagination})
    endpoints.append(endpoint)
  rendered = render_package(plan.model_copy(update={'endpoints': endpoints}))
  page = rendered.files['market/order_page_total.rs']
  assert 'if rows.is_empty() {\n                    return Ok((rows, None));\n' in page
  assert 'exhausted' not in page and 'let size' not in page
  assert 'Ok((rows, Some(current + 1)))' in page


def test_fixture_routers_delegate_with_qualified_types(fixture_rendered):
  files = fixture_rendered.files
  market = files['market/mod.rs']
  assert market.startswith(BANNER + '\n\npub mod order;\n')
  assert 'pub struct Market {\n    order: order::Order,\n' in market
  # `ticker_stream` subscribes over the same core, so the router holds the combined trait.
  assert 'pub fn new(core: Arc<dyn HttpDefaultStreamDefaultEndpoint>) -> Self {' in market
  assert 'order: order::Order::new(core.clone()),' in market
  assert '    ) -> Result<order::OrderAck> {\n        self.order.order(request, options).await\n    }' in market
  assert 'pub async fn orderbook(&self, request: orderbook::Request, options: CallOptions) -> Result<orderbook::Orderbook> {' not in market
  assert ') -> PaginatedResponse<order_list::OrderListItem, String> {\n        self.order_list.order_list_paged(request, options)\n    }' in market
  assert ') -> Result<OrderSide> {\n        self.order_last_side.order_last_side(request, options).await' in market
  root = files['client.rs']
  # The root takes its core by value and wraps it itself, and is named `from_core` so the
  # hand-written core can define `new`. A grouping (`market/mod.rs`, above) keeps `new`
  # with the `Arc` its parent already holds.
  assert (
    'pub fn from_core<C>(core: C) -> Self\n    where\n        C: HttpEndpoint\n            + HttpEndpoint<DefaultMeta>\n'
    '            + HttpEndpoint<FuturesMeta>\n            + StreamEndpoint<DefaultMeta>\n            + \'static,\n    {'
  ) in root
  assert '        let core = Arc::new(core);\n' in root
  assert '    pub market: Market,\n' in root and 'token: Token::new(core),' in root
  account = files['account/mod.rs']
  assert 'pub deposits: deposits::Deposits,' in account and 'pub mod deposits;\npub mod withdrawals;' in account


def test_a_composite_root_renders_websocket_commands_beside_streams():
  """A router under a composite core takes one parameter per declared field, and every
  subtree beneath it renders: HTTP `spot`, the `streams` subscriptions and the `trading_ws`
  commands.

  Kraken's root is composite (`spot` on HTTP, `streams`/`trading_ws` on a socket). A
  socket field handed both a command (`streams.market_data.ping`, `trading_ws.*`) and
  subscriptions has to be one value satisfying both traits, which Rust can only hold as
  one named trait: `contract.rs` declares it.
  """
  root = _example('kraken')
  rendered = render_package(build_plan(root))
  notes = '\n'.join(rendered.skipped)
  assert 'WebSocket' not in notes and 'composite' not in notes
  assert rendered.skipped == []

  files = rendered.files
  assert 'client.rs' in files and 'spot/mod.rs' in files and 'streams/market_data/book.rs' in files
  assert 'pub mod contract;' in files['lib.rs'] and 'pub mod trading_ws;' in files['lib.rs']

  # A WebSocket command hands the core a `CommandCall` whose path is the wire method name.
  add_order = files['trading_ws/add_order.rs']
  assert 'core: Arc<dyn CommandEndpoint>,' in add_order
  assert 'let call = CommandCall {\n            path: "add_order",\n            request: Some(dump(&request)?),\n            meta: &(),\n            options,\n        };\n        self.core.request(call).await' in add_order
  assert 'HttpCall' not in add_order

  # Commands and streams on one field: the combined trait, declared once with a blanket impl.
  contract = files['contract.rs']
  assert 'pub trait CommandStreamEndpoint: CommandEndpoint + StreamEndpoint {}' in contract
  assert 'impl<T: CommandEndpoint + StreamEndpoint + ?Sized> CommandStreamEndpoint for T {}' in contract
  client = files['client.rs']
  assert "        market_client: impl CommandEndpoint + StreamEndpoint + 'static,\n" in client, client
  assert "        spot_client: impl HttpEndpoint<SpotMeta> + 'static,\n    ) -> Self {" in client
  assert 'let market_client: Arc<dyn CommandStreamEndpoint> = Arc::new(market_client);' in client
  assert 'streams: Streams::new(market_client, private_client.clone())' in client or 'Streams::new(' in client
  assert 'spot: Spot::new(spot_client)' in client
  assert 'Arc<dyn >' not in client and 'dyn CommandEndpoint + ' not in client
  market_data = files['streams/market_data/mod.rs']
  assert 'pub fn new(core: Arc<dyn CommandStreamEndpoint>) -> Self {' in market_data
  assert 'use crate::contract::CommandStreamEndpoint;' in market_data

  # Every endpoint is reachable by its function path.
  dispatch = files['dispatch.rs']
  assert 'impl Kraken {' in dispatch
  assert '"trading_ws.add_order" => {\n                let request = decode(request)?;\n                let response = self.trading_ws.add_order(request, options).await?;\n                dump(&response)\n            }' in dispatch
  assert '"streams.market_data.ping" => {\n                let _ = request;' in dispatch
  assert 'Ok(stream.map(|message| dump(&message)))' in dispatch
  assert 'pub async fn subscribe_raw(' in dispatch
  assert '_ => Err(Error::logic(format!("no rpc endpoint {function}"))),' in dispatch
  # Private and hidden from the docs, still callable on the root; the doc's example is one
  # of this plan's own functions.
  assert 'mod dispatch;' in files['lib.rs'] and 'pub mod dispatch;' not in files['lib.rs']
  assert dispatch.count('#[doc(hidden)]\n    pub async fn ') == 4
  first = re.search(r'pub async fn call\(.*?"([\w.]+)" => \{', dispatch, re.DOTALL)
  assert first and f'/// Call the `rpc` endpoint `function` names (`{first[1]}`)' in dispatch


def test_a_dual_transport_endpoint_picks_its_transport_per_call(tmp_path: Path):
  """An `rpc` endpoint declaring `http` and `ws` holds a core satisfying both and picks one
  per call from `CallOptions::transport`, defaulting to the transport listed first."""
  root = tmp_path / 'kraken'
  shutil.copytree(_example('kraken'), root, ignore=shutil.ignore_patterns('node_modules', '.truewire', 'src', 'test', 'target'))
  spec_path = root / 'spec' / 'endpoints' / 'spot' / 'market_data' / 'time' / 'endpoint.json'
  spec = json.loads(spec_path.read_text())
  spec['spec']['transports'] = ['http', 'ws']
  spec_path.write_text(json.dumps(spec))
  rendered = render_package(build_plan(root))
  time = rendered.files['spot/market_data/time.rs']
  assert 'core: Arc<dyn CommandSpotHttpSpotEndpoint>,' in time
  assert 'use crate::contract::CommandSpotHttpSpotEndpoint;' in time
  assert 'let transport = options.transport.unwrap_or(Transport::Http);' in time
  assert 'Transport::Http => {\n                let call = HttpCall {' in time
  assert 'HttpEndpoint::request(&*self.core, call).await\n            }' in time
  assert 'Transport::Ws => {\n                let call = CommandCall {' in time
  assert 'CommandEndpoint::request(&*self.core, call).await\n            }' in time
  contract = rendered.files['contract.rs']
  assert 'pub trait CommandSpotHttpSpotEndpoint: CommandEndpoint<SpotMeta> + HttpEndpoint<SpotMeta> {}' in contract
  assert 'use crate::meta::SpotMeta;' in contract
  # The subtree holding it now needs both traits of its core, and says so once.
  assert 'pub fn new(core: Arc<dyn CommandSpotHttpSpotEndpoint>) -> Self {' in rendered.files['spot/market_data/mod.rs']
  assert "spot_client: impl CommandEndpoint<SpotMeta> + HttpEndpoint<SpotMeta> + 'static," in rendered.files['client.rs']


def test_printer_matches_rustfmt_widths_for_chains_imports_and_long_fields():
  """Three `rustfmt` limits measured against `rustfmt` itself: a trailing `?` costs an
  extra chain column, a brace import list breaks two columns short of `max_width`,
  and a struct-literal field ending in an overflowing call breaks its arguments."""
  w = Writer(3)
  w.chain('let response = ', 'self', ['.spot', '.account', '.create_subaccount(request, options)', '.await?'], ';')
  assert w.render().split('\n')[1] == '                .spot'
  w = Writer(3)
  w.chain('', 'self', ['.spot', '.account', '.create_subacct_raw(request, options)', '.await'])
  assert len(w.render().strip().split('\n')) == 1  # 60 columns without a trailing `?`
  w = Writer(3)
  w.chain('', 'self', ['.spot', '.account', '.create_subaccount_raw(request, options)', '.await'])
  assert len(w.render().strip().split('\n')) == 4  # 61 columns
  imports = Imports()
  for name in ('decode', 'serde_json', 'CallOptions', 'HttpCall', 'HttpEndpoint', 'Result', 'TimestampIso'):
    imports.add('truewire_core', name)
  assert imports.render()[0].startswith('use truewire_core::{\n    decode, serde_json,')
  w = Writer(3)
  w.field('cancel_all_orders_after: cancel_all_orders_after::CancelAllOrdersAfter::new(core.clone())')
  assert w.render() == (
    '            cancel_all_orders_after: cancel_all_orders_after::CancelAllOrdersAfter::new(\n'
    '                core.clone(),\n'
    '            ),\n'
  )


@pytest.mark.parametrize('suffix', ['', ';'], ids=['tail', 'statement'])
@pytest.mark.parametrize(('root', 'method', 'last', 'width', 'vertical'), [
  ('self', 'create_subaccount', '.await', 59, False),
  ('self', 'create_subacct_raw', '.await', 60, False),
  ('api', 'create_subaccount', '.await?', 59, False),
  ('self', 'create_subaccount', '.await?', 60, True),
])
def test_chain_width_boundary(root, method, last, width, vertical, suffix):
  """A semicolon is free; a final `?` costs one column beyond its printed width."""
  elements = ['.spot', '.account', f'.{method}(request, options)', last]
  joined = root + ''.join(elements)
  assert len(joined) == width
  w = Writer(1)
  w.chain('', root, elements, suffix)
  if vertical:
    assert w.render() == (
      '    self.spot\n'
      '        .account\n'
      '        .create_subaccount(request, options)\n'
      f'        .await?{suffix}\n'
    )
  else:
    assert w.render() == f'    {joined}{suffix}\n'
  if shutil.which('rustfmt'):
    source = f'async fn example() {{\n{w.render()}}}\n'
    formatted = subprocess.run(
      ['rustfmt', '--edition', '2021'], input=source, capture_output=True, text=True,
    )
    assert formatted.returncode == 0, formatted.stderr
    assert formatted.stdout == source


def test_bitget_page_size_chain_stays_flat():
  w = Writer(3)
  w.chain('let size = ', 'usize::try_from(request.limit)', ['.ok()', '.filter(|&size| size > 0)'], ';')
  assert w.render() == '            let size = usize::try_from(request.limit).ok().filter(|&size| size > 0);\n'
  if shutil.which('rustfmt'):
    source = f'fn example() {{\n    loop {{\n        loop {{\n{w.render()}        }}\n    }}\n}}\n'
    formatted = subprocess.run(
      ['rustfmt', '--edition', '2021'], input=source, capture_output=True, text=True,
    )
    assert formatted.returncode == 0, formatted.stderr
    assert formatted.stdout == source


@pytest.mark.parametrize(('binding', 'wrapped'), [('page_size_limits', False), ('page_size_limits_', True)])
def test_chain_semicolon_still_counts_toward_full_line_width(binding, wrapped):
  w = Writer(4)
  w.chain(f'let {binding} = ', 'usize::try_from(request.limit)', ['.ok()', '.filter(|&size| size > 0)'], ';')
  assert w.column + len(f'let {binding} = usize::try_from(request.limit).ok().filter(|&size| size > 0);') == (101 if wrapped else 100)
  if wrapped:
    assert w.render() == (
      '                let page_size_limits_ =\n'
      '                    usize::try_from(request.limit).ok().filter(|&size| size > 0);\n'
    )
  else:
    assert w.render() == (
      '                let page_size_limits = usize::try_from(request.limit).ok().filter(|&size| size > 0);\n'
    )
  _assert_chain_binding_rustfmt(w, 4)


@pytest.mark.parametrize(('field', 'last', 'vertical'), [
  ('limi', '', False),  # Continuation ends at column 100, including the semicolon.
  ('limit', '', True),  # Continuation would end at column 101.
  ('l', '?', False),  # Continuation fits with the question mark's two reserved columns.
  ('li', '?', True),  # Printed continuation fits, but the reserved columns do not.
  ('lim', '?', True),
  ('limi', '?', True),  # The question mark also exceeds the chain budget.
])
def test_chain_binding_continuation_width(field, last, vertical):
  root = f'usize::try_from(request.{field})'
  elements = ['.ok()', f'.filter(|&size| size > 0){last}']
  w = Writer(9)
  w.chain('let mut size = ', root, elements, ';')
  if vertical:
    assert w.render() == (
      f'{" " * 36}let mut size = {root}\n'
      f'{" " * 40}.ok()\n'
      f'{" " * 40}{elements[-1]};\n'
    )
  else:
    assert w.render() == f'{" " * 36}let mut size =\n{" " * 40}{root}{"".join(elements)};\n'
  _assert_chain_binding_rustfmt(w, 9)


@pytest.mark.parametrize(('level', 'prefix', 'name'), [
  (9, 'let r = ', 'x' * 23),  # Moved value ends at column 99, with one column to reserve.
  (3, 'let mut size: Option<usize> = ', 'x' * 22),  # Flat line reaches 100 before the reserve.
])
def test_chain_binding_await_question_mark_reserve(level, prefix, name):
  """Both let layouts reserve one column after `.await?`, rather than zero or two."""
  w = Writer(level)
  joined = f'self.spot.{name}(request, options).await?'
  w.chain(prefix, 'self', ['.spot', f'.{name}(request, options)', '.await?'], ';')
  assert w.render() == (
    f'{"    " * level}{prefix.rstrip()}\n'
    f'{"    " * (level + 1)}{joined};\n'
  )
  _assert_chain_binding_rustfmt(w, level)


def _assert_chain_binding_rustfmt(w: Writer, level: int):
  if shutil.which('rustfmt'):
    source = (
      'fn example() {\n'
      + ''.join('    ' * depth + 'loop {\n' for depth in range(1, level))
      + w.render()
      + ''.join('    ' * depth + '}\n' for depth in reversed(range(level)))
    )
    formatted = subprocess.run(
      ['rustfmt', '--edition', '2021'], input=source, capture_output=True, text=True,
    )
    assert formatted.returncode == 0, formatted.stderr
    assert formatted.stdout == source


def test_printer_matches_rustfmt_for_call_arguments_and_overflowing_fields():
  """Three more `rustfmt` decisions, measured against `rustfmt` itself (etherscan's long
  module/action names hit all three): a call's arguments past `fn_call_width` (60) go
  vertical even on a line that fits; a struct field whose type overflows puts it on the
  next line; and a struct-literal field whose `name: callee(` head overflows moves the whole
  value to the next line."""
  w = Writer(3)
  w.call('object.insert', ['"action".to_string()', 'serde_json::json!("xxxxxxxxxxxxxxxxx")'], ';')  # 60
  w.call('object.insert', ['"action".to_string()', 'serde_json::json!("xxxxxxxxxxxxxxxxxx")'], ';')  # 61
  assert w.render() == (
    '            object.insert("action".to_string(), serde_json::json!("xxxxxxxxxxxxxxxxx"));\n'
    '            object.insert(\n'
    '                "action".to_string(),\n'
    '                serde_json::json!("xxxxxxxxxxxxxxxxxx"),\n'
    '            );\n'
  )
  w = Writer(1)
  w.struct_field('f100', 'a::' + 'T' * 86)  # 100 columns
  w.struct_field('f101', 'a::' + 'T' * 87)  # 101
  assert w.render() == f'    f100: a::{"T" * 86},\n    f101:\n        a::{"T" * 87},\n'
  w = Writer(2)
  w.field('nnnnnnnnnn: ' + 'p' * 71 + '::T::new(core.clone())')  # head ends at column 100
  w.field('nnnnnnnnnn: ' + 'p' * 72 + '::T::new(core.clone())')  # 101
  w.field('daily_average_network_difficulty: daily_average_network_difficulty::DailyAverageNetworkDifficulty::new(core.clone())')
  assert w.render() == (
    f'        nnnnnnnnnn: {"p" * 71}::T::new(\n            core.clone(),\n        ),\n'
    f'        nnnnnnnnnn:\n            {"p" * 72}::T::new(\n                core.clone(),\n            ),\n'
    '        daily_average_network_difficulty:\n'
    '            daily_average_network_difficulty::DailyAverageNetworkDifficulty::new(core.clone()),\n'
  )


WALKERS_ROOT = Path(__file__).parent / 'fixtures' / 'rust_walkers'


@pytest.fixture(scope='module')
def walkers_rendered():
  return render_package(build_plan(WALKERS_ROOT))


def test_an_int64_seek_bound_and_cursor_walk_as_i64(tmp_path: Path):
  """`format: int64` renders the base integer, so a seek bound or cursor carrying it walks
  as an `i64` rather than being skipped."""
  import shutil
  from truewire.codegen.rust.endpoint import _state_type
  assert _state_type({'type': 'scalar', 'base': 'integer', 'format': 'int64'}) == 'i64'
  root = tmp_path / 'walkers'
  shutil.copytree(WALKERS_ROOT, root)
  ledger = root / 'spec' / 'endpoints' / 'market' / 'ledger' / 'endpoint.json'
  text = ledger.read_text()
  marked = text.replace('"idLessThan": {\n          "type": "string"', '"idLessThan": {"type": "integer", "format": "int64"', 1)
  assert marked != text
  ledger.write_text(marked)
  rendered = render_package(build_plan(root))
  assert rendered.skipped == []
  assert 'SeekState<i64' in rendered.files['market/ledger.rs']


def test_every_walk_and_a_typed_stream_reply_render(walkers_rendered):
  """`seek` (ADR 0013) in every shape, `offset` walks and a typed stream `reply` (ADR 0014)
  render with nothing skipped."""
  assert walkers_rendered.skipped == []
  files = walkers_rendered.files
  candles = files['market/candles.rs']
  assert "pub type CandlesPagedRequest = Request;" in candles
  # A return type too wide for the closing line puts the brace on its own, as `rustfmt` does.
  assert '    ) -> PaginatedResponse<Vec<serde_json::Value>, SeekState<TimestampMillis, Vec<serde_json::Value>>>\n    {\n' in candles
  assert 'let seek = Seek::new("candles_paged", "[-1][0]", true, false);\n        let seek = seek.cap(size);' in candles
  # The caller's `limit` is clamped once, into the request every page sends, before it is
  # read as the cap: at most the schema's `maximum`, at least 2 (a page of 1 cannot advance
  # past an inclusive moving bound).
  assert (
    '        let mut request = request;\n'
    '        request.limit = request.limit.map(|size| size.clamp(2, 1000));\n'
    '        let size = request.limit.unwrap_or(3);\n'
  ) in candles
  assert 'The walk requests pages of at least 2 rows and at most 1000: a page must hold one new row beside the one it re-reads.' in candles
  assert 'let init = SeekState::new(request.start);' in candles
  assert 'request.start = state.pos;\n' in candles
  assert 'seek.step(&state, rows, None, far.as_ref())' in candles

  chunked = files['market/candles_chunked.rs']
  assert 'let seek = seek.cap(Some(300)).span(3600, SpanUnit::Seconds);' in chunked
  assert 'let init = SeekState::new(Some(request.end));' in chunked
  assert 'let edge = seek.edge(state.pos.as_ref(), far.as_ref())?;' in chunked
  assert 'if let Some(edge) = edge {\n                    request.start = edge;' in chunked
  assert 'seek.step(&state, rows, edge.as_ref(), far.as_ref())' in chunked

  fills = files['market/fills.rs']
  assert 'Seek::new("fills_paged", "[-1].t", false, false)' in fills and '.cap(Some(3))' in fills
  # No size parameter: nothing to clamp, and the request is not rebound.
  assert 'let mut request = request;\n        let seek' not in fills and 'at least 2 rows' not in fills

  # The doc names the row's own field and links the single call: no spec path, no ADR, no
  # "extreme".
  assert (
    '/// Paged variant of [`Self::candles`]: await it for every row, or walk `rows()`/`pages()` one page at a time. '
    'Walks forwards by moving `start` to the latest first element among the rows of each page that came back full, '
    "never past the caller's own `end`;"
  ) in candles
  assert "moving `start_time` to the latest `t` of each page that came back full, never past the caller's own `end_time`" in fills
  # Where the bound moves is what the runtime does (TRU-117, TRU-125): every page while no cap
  # is known, the next range after a short page of a span walk, the last row's string id.
  assert (
    'moving `id_less_than` to the `id` of the last row of each page that came back full, '
    'or of every page while `limit` is unset;'
  ) in files['market/ledger.rs']
  assert (
    'moving `end` to the earliest `time` of each page that came back full, and to the edge of '
    "the range it requested after a short one, never past the caller's own `start`;"
  ) in chunked
  walks = [line for source in files.values() for line in source.splitlines() if '/// Paged variant' in line]
  assert walks and not [line for line in walks if re.search(r'\[-1\]|\[0\]|ADR \d|extreme', line)]

  ledger = files['market/ledger.rs']
  assert 'PaginatedResponse<LedgerRow, SeekState<String, LedgerRow>>' in ledger
  assert 'let far: Option<String> = None;' in ledger
  assert 'request.id_less_than = state.pos.clone();' in ledger

  deposits = files['market/deposits.rs']
  assert 'let following = offset + rows.len() as i64;' in deposits
  assert 'if exhausted(rows.len(), size) {' in deposits and 'Ok((rows, Some(following)))' in deposits
  assert 'PaginatedResponse::new(0, next)' in deposits
  # An `int64` page size (binance's `limit`) is the `i64` it renders as.
  assert 'let size = request.limit.unwrap_or(1000);\n        let size = usize::try_from(size).ok().filter(|&size| size > 0);' in deposits
  withdrawals = files['market/withdrawals.rs']
  assert 'if rows.is_empty() || total.is_some_and(|total| from + rows.len() as i64 >= total) {' in withdrawals

  book = files['streams/book.rs']
  assert ') -> Result<Stream<BookDelta, BookSnapshot>> {' in book
  assert 'stream.map(decode).map_reply(decode)' in book
  dispatch = files['dispatch.rs']
  assert '.map(|message| dump(&message))\n                    .map_reply(|reply| dump(&reply))' in dispatch
  # The routers delegate the walkers with their state types qualified.
  assert 'SeekState<TimestampMillis, Vec<serde_json::Value>>' in files['market/mod.rs']


def test_a_seek_walk_clamps_its_size_at_least_2_in_every_field_shape():
  """`min(max(size, 2), maximum)`, as `clamp` (clippy's `manual_clamp`) floored at
  `min(2, maximum)`; `max(2)` alone without one. A string size, and no size at all, are left
  as they are."""
  from types import SimpleNamespace

  from truewire.codegen.rust.endpoint import _seek_size, _seek_size_rule
  from truewire.codegen.rust.types import Field

  integer = {'type': {'type': 'scalar', 'base': 'integer'}}

  def clamp(*, optional: bool, nullable: bool, maximum: int | None, tree: dict = integer, size: str | None = 'limit'):
    pagination = SimpleNamespace(size=size, size_maximum=maximum)
    fields = [Field('limit', 'limit', 'i64', optional, nullable)]
    return _seek_size(pagination, fields, {'limit': tree})  # type: ignore[arg-type]

  assert clamp(optional=False, nullable=False, maximum=500) == ('request.limit = ', 'request', ['.limit', '.clamp(2, 500)'])
  assert clamp(optional=True, nullable=False, maximum=None) == ('request.limit = ', 'request', ['.limit', '.map(|size| size.max(2))'])
  assert clamp(optional=True, nullable=True, maximum=500) == ('request.limit = ', 'request', ['.limit', '.map(|size| size.map(|size| size.clamp(2, 500)))'])
  assert clamp(optional=False, nullable=True, maximum=1) == ('request.limit = ', 'request', ['.limit', '.map(|size| size.clamp(1, 1))'])
  assert clamp(optional=True, nullable=False, maximum=500, tree={'type': {'type': 'scalar', 'base': 'string'}}) is None
  assert clamp(optional=True, nullable=False, maximum=500, size=None) is None
  # Under a maximum below 2 the doc claims no floor of 2.
  assert _seek_size_rule(SimpleNamespace(size_maximum=1)) == 'The walk requests pages of 1 row.'  # type: ignore[arg-type]
  assert _seek_size_rule(SimpleNamespace(size_maximum=500)).startswith('The walk requests pages of at least 2 rows and at most 500:')  # type: ignore[arg-type]


@pytest.mark.skipif(shutil.which('rustfmt') is None, reason='rustfmt is not installed')
def test_walkers_output_satisfies_rustfmt(walkers_rendered, tmp_path: Path):
  for path, content in walkers_rendered.files.items():
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
  (tmp_path / 'core.rs').write_text('// The hand-written core, stubbed for the check.\n')
  for path in walkers_rendered.files:
    checked = subprocess.run(
      ['rustfmt', '--edition', '2021', '--check', str(tmp_path / path)], capture_output=True, text=True,
    )
    assert checked.returncode == 0, f'{path}:\n{checked.stdout}{checked.stderr}'


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
  manifest = json.loads((root / '.truewire' / 'codegen' / 'rust.json').read_text())
  for path, content in rendered.files.items():
    assert (package / path).read_text() == content, path
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
  assert 'skipped' not in result.output
  manifest = root / '.truewire' / 'codegen' / 'rust.json'
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


def test_printer_moves_an_overflowing_field_value_to_the_next_line():
  """Measured against `rustfmt` on moralis's routers: when even `name: callee(` overflows,
  a struct-literal field puts its whole value on the next line (the kraken-shaped field
  above, whose head fits, still breaks its arguments), and a struct declaration too wide
  for one line breaks after the colon."""
  w = Writer(3)
  w.field('resolve_address_from_ens_domain: resolve_address_from_ens_domain::ResolveAddressFromEnsDomain::new(core.clone())')
  assert w.render() == (
    '            resolve_address_from_ens_domain:\n'
    '                resolve_address_from_ens_domain::ResolveAddressFromEnsDomain::new(core.clone()),\n'
  )
  w = Writer(1)
  w.declaration('send_webhook_data_by_block_number: send_webhook_data_by_block_number::SendWebhookDataByBlockNumber')
  w.declaration('pub short: Type')
  assert w.render() == (
    '    send_webhook_data_by_block_number:\n'
    '        send_webhook_data_by_block_number::SendWebhookDataByBlockNumber,\n'
    '    pub short: Type,\n'
  )


def test_a_core_name_the_module_defines_is_reached_by_its_path():
  """An endpoint whose row record is named `Result` must not also import the runtime's
  `Result` alias: the module spells the alias `truewire_core::Result` instead."""
  from truewire.codegen.rust.endpoint import render_tokens
  m = _module({'Result': {'type': 'record', 'id': 'Result', 'fields': {}}}, {})
  assert render_tokens(m, [('core', 'Result'), ('text', '<'), ('local', 'Result'), ('text', '>')]) == 'truewire_core::Result<Result>'
  assert render_tokens(m, [('core', 'CallOptions')]) == 'CallOptions'
  assert m.imports.render() == ['use truewire_core::CallOptions;']


def test_a_broken_generic_whose_argument_overflows_closes_without_a_space():
  """Measured against `rustfmt` on moralis's `auth.bind` router: a return type's generic
  argument wider than the line leaves the closer as `>{`."""
  w = Writer(1)
  w.signature(
    'pub async fn request_bind_between_profile_of_two_addresses',
    ['&self', 'request: request_bind_between_profile_of_two_addresses::Request', 'options: CallOptions'],
    ' -> Result<request_bind_between_profile_of_two_addresses::RequestBindBetweenProfileOfTwoAddressesResponse> {',
  )
  assert w.render().endswith('RequestBindBetweenProfileOfTwoAddressesResponse,\n    >{\n')


def test_a_deprecated_method_allows_calling_its_deprecated_twin():
  """A deprecated typed method calls its deprecated `_raw` twin, so it carries
  `#[allow(deprecated)]` too, not only a router delegate (clippy on bit2me and moralis)."""
  from truewire.codegen.rust.endpoint import Method, emit_method
  m = _module({}, {})
  emit_method(m, Method('get', [], [('core', 'Result'), ('text', '<()>')], is_async=True, doc=[], deprecated=True), qualifier=None, body=['self.get_raw().await'])
  assert m.writer.render().startswith('#[deprecated]\n#[allow(deprecated)]\npub async fn get(')


# -- hand-written surfaces and long tuple variants --------------------------------------


def test_a_hand_written_surface_is_skipped_and_its_router_keeps_the_core():
  """kraken's `retrieve_export` declares a hand-written surface: the backend renders no
  module, no router method and no dispatch arm for it, and `spot.account` keeps its core
  as `pub(crate) core` for the inherent `impl` written beside it. Routers without one do not."""
  project = load_project(_example('kraken'))
  rendered = render_package(build_plan(project), project)
  assert 'spot.account.retrieve_export: a hand-written surface (spot.account.retrieve_export:retrieve_export)' in rendered.skipped
  assert 'spot/account/retrieve_export.rs' not in rendered.files
  account = rendered.files['spot/account/mod.rs']
  assert 'retrieve_export' not in account
  assert '    pub(crate) core: Arc<dyn HttpEndpoint<SpotMeta>>,\n}' in account
  assert '            trades_history: trades_history::TradesHistory::new(core.clone()),\n            core,\n' in account
  assert 'spot.account.retrieve_export' not in rendered.files['dispatch.rs']
  assert 'pub(crate) core' not in rendered.files['spot/market_data/mod.rs']


def test_an_overlong_tuple_variant_breaks_as_rustfmt_does():
  w = Writer(level=1)
  members = ', '.join(['TimestampSeconds'] + ['DecimalString'] * 6 + ['i64'])
  w.variant('TupleList', f'Vec<({members})>')
  w.variant('Integer', 'i64')
  assert w.render() == (
    '    TupleList(\n'
    '        Vec<(\n'
    '            TimestampSeconds,\n'
    + '            DecimalString,\n' * 6
    + '            i64,\n'
    '        )>,\n'
    '    ),\n'
    '    Integer(i64),\n'
  )


def test_a_signature_holding_a_wire_tuple_allows_type_complexity():
  """bit2me's `candles_paged` returns `PaginatedResponse<(TimestampMillis, f64, ...), SeekState<_, (...)>>`:
  the row is a positional wire tuple with no name to give it, so the lint is silenced. A
  unit `Result<()>` is not a tuple and gets no attribute."""
  from truewire.codegen.rust.endpoint import Method, emit_method
  m = _module({}, {})
  row = '(i64, f64, f64)'
  emit_method(m, Method('candles_paged', [], [('text', f'PaginatedResponse<{row}, SeekState<i64, {row}>>')], is_async=False, doc=[]), qualifier=None, body=['todo!()'])
  assert m.writer.render().startswith('#[allow(clippy::type_complexity)]\npub fn candles_paged(')
  m = _module({}, {})
  emit_method(m, Method('ping', [], [('core', 'Result'), ('text', '<()>')], is_async=True, doc=[]), qualifier=None, body=['Ok(())'])
  assert m.writer.render().startswith('pub async fn ping(')


def test_a_null_response_is_dispatched_without_binding_it():
  """An endpoint whose response is an alias of `null` (bit2me `v1.account.delete`) has a
  `_raw` twin but a `()` typed result; `dispatch.rs` must not `let response = ...` it
  (`clippy::let_unit_value`)."""
  from truewire.codegen.rust.dispatch import render_dispatch
  from truewire.codegen.rust.endpoint import EndpointModule, renders_unit
  m = _module({'Gone': {'type': 'ref', 'id': 'Nothing'}, 'Nothing': {'type': 'scalar', 'base': 'null'}, 'Row': {'type': 'scalar', 'base': 'string'}})
  assert renders_unit(m, 'Gone') and renders_unit(m, 'Nothing')
  assert not renders_unit(m, 'Row') and not renders_unit(m, None) and not renders_unit(m, 'Vec<Row>')
  unit = EndpointModule(file='a/delete.rs', struct_name='Delete', bound='HttpEndpoint', meta_type=None, methods=[], source='', main='delete', raw='delete_raw', unit_response=True)
  plan = PackagePlan(name='p', root_class='P', schemas={})
  rendered = render_dispatch(plan, root_name='P', endpoints={'a.delete': unit})
  typed_arm = rendered.split('pub async fn call_raw')[0]
  assert 'let response' not in typed_arm
  assert 'self.a.delete(options).await?;\n                Ok(serde_json::Value::Null)' in typed_arm
  assert 'self.a.delete_raw(options).await' in rendered.split('pub async fn call_raw')[1]


def test_a_stream_whose_parameters_fill_its_channel_takes_a_parameters_struct(fixture_rendered):
  """`/ticker/{symbol}` has no parameters object on the wire: the method takes a generated
  `Parameters`, fills the channel from its dump and hands the core `parameters: None`."""
  ticker = fixture_rendered.files['market/ticker_stream.rs']
  assert 'pub struct Parameters {\n    /// Trading pair.\n    pub symbol: String,' in ticker
  assert 'use truewire_core::http::query_value;' in ticker
  assert (
    '        let values = dump(&parameters)?;\n'
    '        let channel = format!(\n'
    '            "/ticker/{}",\n'
    '            query_value(&values["symbol"]).unwrap_or_default(),\n'
    '        );\n'
  ) in ticker
  assert 'channel: &channel,\n            parameters: None,' in ticker
  dispatch = fixture_rendered.files['dispatch.rs']
  assert '"market.ticker_stream" => {\n                let parameters = decode(parameters)?;' in dispatch


def test_a_token_walk_over_a_union_payload_joins_every_variants_rows(walkers_rendered):
  instruments = walkers_rendered.files['market/instruments.rs']
  assert 'pub enum InstrumentsPagedRequestRow {\n    SpotRow(SpotRow),\n    ContractRow(ContractRow),\n    OptionRow(OptionRow),\n}' in instruments
  assert ') -> PaginatedResponse<InstrumentsPagedRequestRow, String> {' in instruments
  assert 'let (rows, cursor) = match response {' in instruments
  # A variant without the cursor ends the walk; a required one is wrapped, a nullable one flattened.
  assert '.map(InstrumentsPagedRequestRow::SpotRow)\n                            .collect::<Vec<_>>();\n                        (rows, None)' in instruments
  assert 'let cursor = page.next_page_cursor;' in instruments and '(rows, Some(cursor))' in instruments
  assert 'let cursor = page.next_page_cursor.flatten();' in instruments and '(rows, cursor)\n' in instruments
  assert 'Ok((rows, cursor_or_done(cursor)))' in instruments


def test_a_page_walk_over_a_union_payload_joins_every_variants_rows(walkers_rendered):
  """binance's broker `sub_account_futures_summary_v2` answers one of two shapes by
  `futuresType`: a page walk joins whichever variant's rows arrived, with no cursor to read."""
  summary = walkers_rendered.files['market/summary.rs']
  assert ') -> PaginatedResponse<SummaryPagedRequestRow, i64> {' in summary
  assert 'let rows = match response {\n                    Response::SummaryUsdm(page) => page\n                        .data\n' in summary
  assert '.map(SummaryPagedRequestRow::CoinmRow)\n                        .collect::<Vec<_>>(),\n                };' in summary
  assert 'if exhausted(rows.len(), size) {' in summary
  # No cursor is read, so none is imported (`unused_imports` under clippy's -D warnings).
  assert 'cursor_or_done' not in summary

def test_a_page_size_and_total_sent_as_strings_are_parsed(walkers_rendered):
  conversions = walkers_rendered.files['market/conversions.rs']
  assert '.as_deref()\n            .and_then(|size| size.parse::<usize>().ok())\n            .filter(|&size| size > 0);' in conversions
  assert 'let total = response.last_page.parse::<i64>().ok().unwrap_or(i64::MAX);' in conversions
  assert 'if rows.is_empty() || total_reached(true, cursor, 1, size, rows.len(), total) {' in conversions


def test_a_page_size_is_clamped_to_its_schema_maximum(tmp_path: Path):
  """A caller asking for more rows than the size's `maximum` gets a full page at the
  maximum, which must not read as a short last page (weather-gov's `limit` over 500 ended
  the walk after one page). Required, defaulted and optional sizes are all capped."""
  root = tmp_path / 'walkers'
  shutil.copytree(WALKERS_ROOT, root)
  market = root / 'spec' / 'endpoints' / 'market'

  def cap(name: str, parameter: str, maximum: int, *, required: bool = False) -> None:
    path = market / name / 'endpoint.json'
    doc = json.loads(path.read_text())
    request = doc['spec']['request']
    request['properties'][parameter]['maximum'] = maximum
    if required:
      request['properties'][parameter].pop('default', None)
      request.setdefault('required', []).append(parameter)
    path.write_text(json.dumps(doc, indent=2))

  cap('deposits', 'limit', 1000)
  cap('ledger', 'limit', 500)
  cap('summary', 'size', 100, required=True)
  rendered = render_package(build_plan(root))
  assert rendered.skipped == []
  files = rendered.files
  # `offset`, defaulted.
  assert 'let size = request.limit.unwrap_or(1000).min(1000);\n' in files['market/deposits.rs']
  # `seek`, optional without a default: the size is clamped once, in the request every page
  # sends (`_seek_size`), and the cap reads it as is.
  assert (
    'request.limit = request.limit.map(|size| size.clamp(2, 500));\n'
    '        let size = request\n'
    '            .limit\n'
    '            .and_then(|size| usize::try_from(size).ok())\n'
    '            .filter(|&size| size > 0);\n'
  ) in files['market/ledger.rs']
  assert '.min(500)' not in files['market/ledger.rs']
  # `page`, required.
  assert 'let size = usize::try_from(request.size.min(100))\n            .ok()\n            .filter(|&size| size > 0);\n' in files['market/summary.rs']
  # A size with no `maximum` is left as the caller sent it.
  assert 'let size = request.limit.unwrap_or(3);\n' in files['market/candles.rs']
  assert all(' as usize' not in binding for binding in _size_bindings(files)), _size_bindings(files)



def _size_bindings(files: dict[str, str]) -> list[str]:
  """Every `let size = ...;` statement a walker binds, whole when it wraps."""
  return [m.group(0) for text in files.values() for m in re.finditer(r'let size = [^;]*;', text)]


def test_a_page_size_of_zero_or_less_is_unknown(walkers_rendered):
  """A size of 0 or less binds `None`, the same as an unset size: cast with `as usize`, a 0
  divided by zero in a page-counting `total` (a panic in library code) and a negative size
  became `usize::MAX`, so every page read as short and the walk stopped after the first."""
  assert walkers_rendered.skipped == []
  files = walkers_rendered.files
  bindings = _size_bindings(files)
  # Required and string sizes are covered by `test_a_page_size_is_clamped_to_its_schema_maximum`
  # and `test_a_page_size_and_total_sent_as_strings_are_parsed`.
  assert len(bindings) >= 8
  assert all(' as usize' not in binding for binding in bindings), bindings
  assert all('.filter(|&size| size > 0);' in binding for binding in bindings if 'unwrap_or(' not in binding), bindings
  pages = files['market/withdrawal_pages.rs']
  assert '.and_then(|size| usize::try_from(size.min(500)).ok())\n            .filter(|&size| size > 0);' in pages
  assert 'from / size as i64 + 1 >= total' in pages



@pytest.mark.skipif(shutil.which('cargo') is None, reason='cargo is not installed')
def test_offset_page_total_stops_on_the_partial_last_page(walkers_rendered, tmp_path: Path):
  """Aligned and unaligned resumed walks return every remaining row over HTTP."""
  import os
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
  from threading import Thread
  from urllib.parse import parse_qs, urlparse

  offsets = []

  class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
      query = parse_qs(urlparse(self.path).query)
      offset, limit = int(query['from'][0]), int(query['limit'][0])
      offsets.append(offset)
      limit = min(limit, 500)
      body = json.dumps({'pages': (10 + limit - 1) // limit, 'items': [
        {'id': str(i)} for i in range(offset, min(offset + limit, 10))
      ]}).encode()
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, *_args):
      pass

  src = tmp_path / 'src'
  src.mkdir()
  (src / 'withdrawal_pages.rs').write_text(walkers_rendered.files['market/withdrawal_pages.rs'])
  (src / 'main.rs').write_text(OFFSET_PAGES_WIRE_TEST)
  core = Path(__file__).parents[3] / 'crates' / 'truewire-core'
  (tmp_path / 'Cargo.toml').write_text(
    '[package]\nname = "offset-pages-test"\nversion = "0.0.0"\nedition = "2021"\n'
    '[dependencies]\n'
    f'truewire-core = {{ path = "{core}" }}\n'
    'serde = { version = "1", features = ["derive"] }\n'
    'tokio = { version = "1", features = ["macros", "rt-multi-thread"] }\n'
    'reqwest = "0.12"\nasync-trait = "0.1"\n'
  )
  server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
  thread = Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    env = {**os.environ, 'WALK_URL': f'http://127.0.0.1:{server.server_port}',
           'CARGO_TARGET_DIR': os.environ.get('TRUEWIRE_CARGO_TARGET_DIR', str(tmp_path / 'target'))}
    result = subprocess.run(['cargo', 'run', '--quiet'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert offsets == [0, 4, 8, 1, 5, 9, 5, 9, 0], offsets
  finally:
    server.shutdown()
    thread.join()
    server.server_close()


OFFSET_PAGES_WIRE_TEST = r'''mod withdrawal_pages;

use std::sync::Arc;
use truewire_core::http::{query_from, HttpClient, HttpClientOptions, RequestOptions};
use async_trait::async_trait;
use truewire_core::{serde_json::Value, CallOptions, HttpCall, HttpEndpoint, Result};
use withdrawal_pages::{WithdrawalPagesEndpoint, WithdrawalPagesEndpointPagedRequest};

struct Core(HttpClient);

#[async_trait]
impl HttpEndpoint for Core {
    async fn request(&self, call: HttpCall<'_, ()>) -> Result<Value> {
        let query = query_from(call.request.unwrap().as_object().unwrap().clone());
        self.0
            .request(
                "GET",
                &format!("{}{}", std::env::var("WALK_URL").unwrap(), call.path),
                RequestOptions::new().query(query),
            )
            .await?
            .json()
    }
}

#[tokio::main]
async fn main() {
    let core = Core(HttpClient::new(HttpClientOptions {
        client: Some(reqwest::Client::builder().no_proxy().build().unwrap()),
        ..Default::default()
    }));
    let endpoint = WithdrawalPagesEndpoint::new(Arc::new(core));
    for (start, limit) in [(0, 4), (1, 4), (5, 4), (0, 9000)] {
        let rows = endpoint
            .withdrawal_pages_paged(
                WithdrawalPagesEndpointPagedRequest { limit: Some(limit), ..Default::default() },
                CallOptions::default(),
            )
            .resume(start)
            .await
            .unwrap();
        assert_eq!(rows.iter().map(|row| row.id.clone()).collect::<Vec<_>>(),
                   (start..10).map(|id| id.to_string()).collect::<Vec<_>>());
    }
}
'''

def test_an_int64_max_page_size_maximum_is_no_cap(tmp_path: Path):
  """Spec generators emit `maximum: 9223372036854775807` for an `int64` size. As a float it
  rounds up to 2**63, which `.min(...)` would emit as a literal out of range for `i64`
  (`overflowing_literals` is deny-by-default, so the crate stops compiling)."""
  root = tmp_path / 'walkers'
  shutil.copytree(WALKERS_ROOT, root)
  path = root / 'spec' / 'endpoints' / 'market' / 'deposits' / 'endpoint.json'
  doc = json.loads(path.read_text())
  doc['spec']['request']['properties']['limit']['maximum'] = 2**63 - 1
  path.write_text(json.dumps(doc, indent=2))
  plan = build_plan(root)
  deposits = next(e for e in plan.endpoints if e.path[-1] == 'deposits')
  assert deposits.pagination is not None and deposits.pagination.size_maximum is None
  assert 'let size = request.limit.unwrap_or(1000);\n' in render_package(plan).files['market/deposits.rs']

def test_a_seek_over_wide_tuple_rows_breaks_the_tuple_as_rustfmt_does(walkers_rendered):
  klines = walkers_rendered.files['market/klines.rs']
  tuple_at = lambda indent: (  # noqa: E731
    f'{indent}(\n' + ''.join(f'{indent}    {member},\n' for member in ['TimestampMillisString'] + ['String'] * 6) + f'{indent})'
  )
  assert f'    pub list: Vec<(\n' in klines
  assert f'    ) -> PaginatedResponse<\n{tuple_at("        ")},\n        SeekState<\n' in klines
  assert f'        let next = move |state: SeekState<\n            TimestampMillis,\n{tuple_at("            ")},\n        >| {{\n' in klines
  assert 'let seek = Seek::new("klines_paged", "[-1][0]", true, true);' in klines


def test_a_seek_over_a_narrow_tuple_row_breaks_after_the_binding(walkers_rendered):
  """bit2me `v1/trading/candles`: a six-member row keeps the closure head within the line
  one level in, so `rustfmt` breaks after `let next =` instead of splitting `SeekState`'s
  arguments. The whole binding would be 105 columns where it lands; the head alone is 98."""
  ohlcv = walkers_rendered.files['market/ohlcv.rs']
  assert (
    '        let next =\n'
    '            move |state: SeekState<TimestampMillis, (TimestampMillis, f64, f64, f64, f64, f64)>| {\n'
    '                let endpoint = endpoint.clone();\n'
  ) in ohlcv
  assert 'let next = move |state: SeekState<\n' not in ohlcv
  assert max(len(line) for line in ohlcv.splitlines() if 'move |state' in line) == 98


def test_printer_follows_rustfmt_for_lone_attributes_where_bounds_and_wide_tuples():
  w = Writer(level=1)
  long = 'F_MART_LIMIT_CHECK_CODE_F_MART_CHECK_CODE_SUCCESS_UNSPECIFIED'
  w.attribute('serde', [f'rename = "{long}"'])
  w.attribute('serde', ['rename = "a"', 'default', 'skip_serializing_if = "Option::is_none"', 'with = "xxxxxxxxxx"'])
  w.struct_field('pub short', 'Vec<(TimestampMillisString, String, String, String, String)>')
  w.call('let seek = Seek::new', ['"funding_history_paged"', '"[-1].fundingRateTimestamp"', 'true', 'true'], ';')
  w.bounds('C: ', ['HttpEndpoint', 'HttpEndpoint<DefaultMeta>', 'HttpEndpoint<FuturesMeta>', 'StreamEndpoint<DefaultMeta>', "'static"], ',')
  assert w.render() == (
    f'    #[serde(rename = "{long}")]\n'
    '    #[serde(\n'
    '        rename = "a",\n'
    '        default,\n'
    '        skip_serializing_if = "Option::is_none",\n'
    '        with = "xxxxxxxxxx"\n'
    '    )]\n'
    '    pub short: Vec<(TimestampMillisString, String, String, String, String)>,\n'
    '    let seek = Seek::new(\n'
    '        "funding_history_paged",\n'
    '        "[-1].fundingRateTimestamp",\n'
    '        true,\n'
    '        true,\n'
    '    );\n'
    '    C: HttpEndpoint\n'
    '        + HttpEndpoint<DefaultMeta>\n'
    '        + HttpEndpoint<FuturesMeta>\n'
    '        + StreamEndpoint<DefaultMeta>\n'
    "        + 'static,\n"
  )


def test_clippy_allows_for_wide_roots_tuple_signatures_and_unit_dispatch(walkers_rendered, tmp_path: Path):
  """Ten transports on one root, a candle-tuple walker and a `()` response stay clippy-clean."""
  klines = walkers_rendered.files['market/klines.rs']
  assert '    #[allow(clippy::type_complexity)]\n    pub fn klines_paged(' in klines
  assert '#[allow(clippy::type_complexity)]\n    pub fn klines_paged(' in walkers_rendered.files['market/mod.rs']
  from truewire.codegen.rust.dispatch import _arm
  from truewire.codegen.rust.endpoint import EndpointModule
  from truewire.codegen.rust.printer import Imports
  w = Writer(level=3)
  unit = EndpointModule(file='x.rs', struct_name='X', bound='HttpEndpoint', meta_type=None, methods=[], source='',
                        main='set_dcp', raw='set_dcp_raw', takes_request=True, unit_response=True)
  _arm(w, 'trade.set_dcp', unit, subject='request', raw=False, imports=Imports())
  rendered = w.render()
  assert 'let response' not in rendered
  assert 'self.trade.set_dcp(request, options).await?;' in rendered and 'Ok(serde_json::Value::Null)' in rendered


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
  """`children = { app = "app_client" }` on a composite child: the root takes `app_client`
  and passes it where `App::new` takes its own `client`."""
  rendered = render_package(_renamed_composite_plan())
  source = '\n'.join(rendered.files.values())
  assert 'App::new(app_client, socket)' in ' '.join(source.split())


def test_a_tuple_variant_with_wide_members_and_an_overflowing_root_binding_break_as_rustfmt_does():
  w = Writer(level=1)
  w.variant('Tuple', '(PayloadTupleN0, String, String, String, DecimalString, DecimalString, TimestampIso)')
  w.variant('Tuple2', '(PayloadTuple2N0, String, String, String, TimestampIso)')
  with w.indented():
    w.binding('let feed_client: Arc<dyn StreamEndpoint<ExchangeStreamsMeta>>', 'Arc::new(feed_client)')
    w.binding('let international_client: Arc<dyn HttpEndpoint<InternationalHttpMeta>>', 'Arc::new(international_client)')
  assert w.render() == (
    '    Tuple(\n'
    '        (\n'
    '            PayloadTupleN0,\n'
    + '            String,\n' * 3
    + '            DecimalString,\n' * 2
    + '            TimestampIso,\n'
    '        ),\n'
    '    ),\n'
    '    Tuple2((PayloadTuple2N0, String, String, String, TimestampIso)),\n'
    '        let feed_client: Arc<dyn StreamEndpoint<ExchangeStreamsMeta>> = Arc::new(feed_client);\n'
    '        let international_client: Arc<dyn HttpEndpoint<InternationalHttpMeta>> =\n'
    '            Arc::new(international_client);\n'
  )


def test_a_value_moved_to_its_own_line_may_reach_max_width_without_its_comma():
  """Deribit's `block_trade/mod.rs`: `rustfmt` keeps the moved value whole on a line of
  exactly 100 columns plus the trailing comma."""
  from truewire.codegen.rust.printer import Writer
  w = Writer(3)
  w.field('invalidate_block_trade_signature: invalidate_block_trade_signature::InvalidateBlockTradeSignature::new(client.clone())')
  assert w.render() == (
    '            invalidate_block_trade_signature:\n'
    '                invalidate_block_trade_signature::InvalidateBlockTradeSignature::new(client.clone()),\n'
  )


def test_a_twin_named_like_a_sibling_endpoint_is_numbered_in_the_router(tmp_path: Path):
  """Deribit has a stream `user.orders_by_instrument` (whose `_raw` twin is
  `orders_by_instrument_raw`) beside a stream named `user.orders_by_instrument_raw`. The
  endpoint keeps its own name; the twin is delegated as `..._raw2`, as Go numbers it, and
  `dispatch.rs` calls that name."""
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  market = root / 'spec' / 'endpoints' / 'market'
  shutil.copytree(market / 'ticker_stream', market / 'ticker_stream_raw')
  files = render_package(build_plan(root)).files
  router = files['market/mod.rs']
  assert 'pub async fn ticker_stream_raw2(' in router
  assert router.count('pub async fn ticker_stream_raw(') == 1
  assert '.ticker_stream_raw2(parameters, options)' in files['dispatch.rs']
  assert 'pub async fn ticker_stream_raw(' in files['market/ticker_stream.rs']


def test_a_walk_skipped_part_way_leaves_no_paged_request_behind(tmp_path: Path):
  """Deribit's `get_delivery_prices` declares a `total` that is not an integer: the walk is
  skipped after its request type and `at` were written, which nothing would call (an unused
  `at` fails `clippy -D warnings`). The module is restored, in place, to before the walk."""
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  endpoint = root / 'spec' / 'endpoints' / 'market' / 'order_page_total' / 'endpoint.json'
  endpoint.write_text(endpoint.read_text().replace('"total": {"type": "integer"', '"total": {"type": "number"'))
  rendered = render_package(build_plan(root))
  assert any(s.startswith('market.order_page_total: a `total` that is not an integer') for s in rendered.skipped), rendered.skipped
  module = rendered.files['market/order_page_total.rs']
  assert 'fn at(' not in module and 'PagedRequest' not in module
  assert module.index('impl OrderPageTotal {') < module.index('pub async fn order_page_total(')


def test_a_record_holding_a_wire_tuple_allows_clippy_type_complexity():
  """Deribit's `get_volatility_index_data` rows are `(i64, f64, f64, f64, f64)` in a record
  field; clippy's `type_complexity` would refuse the struct under `-D warnings`."""
  record = {'type': 'record', 'id': 'Candles', 'fields': {
    'data': {'required': False, 'type': {'type': 'list', 'item': {'type': 'tuple', 'items': [
      {'type': 'scalar', 'base': 'integer'}, *[{'type': 'scalar', 'base': 'number'}] * 4]}}},
    'name': {'required': True, 'type': {'type': 'scalar', 'base': 'string'}},
  }}
  module = _module({'Candles': record})
  module.define_all({'Candles': record})
  assert '#[allow(clippy::type_complexity)]\n#[derive(' in module.writer.render()
  plain = _module({'Plain': {**record, 'id': 'Plain', 'fields': {'name': record['fields']['name']}}})
  plain.define_all(plain.local)
  assert 'type_complexity' not in plain.writer.render()


def test_a_packed_import_list_may_end_on_a_line_of_exactly_max_width():
  """`rustfmt` does not count the last packed line's trailing comma: deribit's
  `streams/user/access_log.rs` imports end on a 100-column line it keeps."""
  from truewire.codegen.rust.printer import Imports
  imports = Imports()
  for name in ('decode', 'serde_json', 'CallOptions', 'Result', 'Stream', 'StreamEndpoint', 'SubscribeCall', 'TimestampMillis'):
    imports.add('truewire_core', name)
  line = '    decode, serde_json, CallOptions, Result, Stream, StreamEndpoint, SubscribeCall, TimestampMillis,'
  assert len(line) == 100
  assert imports.render() == ['use truewire_core::{\n' + line + '\n};']


# -- bitget: inline tuple rows, and a router whose child is all hand-written -------------


def test_a_tuple_wider_than_fn_call_width_breaks_even_where_it_fits():
  """Measured against `rustfmt` on bitget's UTA candles: a tuple type whose members exceed
  `fn_call_width` goes vertical inside an alias even though the line fits in 100 columns."""
  w = Writer()
  w.type_alias('pub type Response = ', 'Vec<(TimestampMillisString, String, String, String, String, String, String)>')
  assert w.render() == (
    'pub type Response = Vec<(\n'
    '    TimestampMillisString,\n'
    + '    String,\n' * 6
    + ')>;\n'
  )
  w = Writer()
  w.type_alias('pub type Short = ', '(i64, String)')
  assert w.render() == 'pub type Short = (i64, String);\n'


def test_a_struct_field_too_wide_for_the_next_line_breaks_inside_its_type():
  w = Writer(1)
  members = ', '.join(['String'] + ['DecimalString'] * 7)
  w.struct_field('pub data', f'Vec<({members})>')
  assert w.render() == (
    '    pub data: Vec<(\n'
    '        String,\n'
    + '        DecimalString,\n' * 7
    + '    )>,\n'
  )


def test_a_walker_signature_and_closure_break_their_tuple_rows():
  row = '(' + ', '.join(['TimestampMillisString'] + ['DecimalString'] * 7) + ')'
  w = Writer(1)
  w.signature('pub fn candles_paged', ['&self', 'request: CandlesPagedRequest', 'options: CallOptions'],
              f' -> PaginatedResponse<{row}, SeekState<TimestampMillis, {row}>> {{')
  out = w.render()
  assert '    ) -> PaginatedResponse<\n        (\n            TimestampMillisString,\n' in out
  assert '        SeekState<\n            TimestampMillis,\n            (\n' in out
  assert out.endswith('        >,\n    > {\n')
  w = Writer(2)
  with w.closure_binding('let next', 'move |state: ', f'SeekState<TimestampMillis, {row}>', '| {'):
    w.line('let endpoint = endpoint.clone();')
  assert w.render().startswith('        let next = move |state: SeekState<\n            TimestampMillis,\n            (\n')
  assert '            ),\n        >| {\n            let endpoint = endpoint.clone();\n        };\n' in w.render()


def test_a_seek_closure_head_moves_below_the_binding_as_rustfmt_does():
  """bit2me `v1/trading/candles`: the whole `let next = move |state: SeekState<..>| {` passes
  100 columns, but the head alone fits one level in, so `rustfmt` breaks after `let next =`
  rather than splitting the generic arguments. A line reaching column 100 is already too
  wide, and the walker body is rendered one level shallower than it finally sits, so the
  widths are measured with `offset`."""
  def render(type_: str) -> str:
    w = Writer(1)
    with w.closure_binding('let next', 'move |state: ', type_, '| {', offset=len(INDENT)):
      w.line('let endpoint = endpoint.clone();')
    return w.render()

  candle = 'SeekState<TimestampMillis, (TimestampMillis, f64, f64, f64, f64, f64)>'
  assert render(candle) == (
    '    let next =\n'
    '        move |state: SeekState<TimestampMillis, (TimestampMillis, f64, f64, f64, f64, f64)>| {\n'
    '            let endpoint = endpoint.clone();\n'
    '        };\n'
  )
  # The head line is 98 columns where it lands, the whole binding would be 105.
  assert len('        move |state: ' + candle + '| {') + len(INDENT) == 98

  assert render('SeekState<TimestampMillis, SpotCandle>') == (
    '    let next = move |state: SeekState<TimestampMillis, SpotCandle>| {\n'
    '        let endpoint = endpoint.clone();\n'
    '    };\n'
  )

  wide = 'SeekState<TimestampMillis, ' + 'I' + 'x' * 43 + '>'
  assert render(wide).startswith('    let next = move |state: SeekState<\n        TimestampMillis,\n')
  assert render(wide).endswith('    >| {\n        let endpoint = endpoint.clone();\n    };\n')


def test_an_overflowing_let_binding_breaks_after_the_equals_sign():
  w = Writer(2)
  w.let_binding('let classic_streams_client: Arc<dyn StreamEndpoint<StreamsMeta>>', 'Arc::new(classic_streams_client)')
  assert w.render() == (
    '        let classic_streams_client: Arc<dyn StreamEndpoint<StreamsMeta>> =\n'
    '            Arc::new(classic_streams_client);\n'
  )


def test_the_last_packed_import_may_end_at_max_width():
  imports = Imports()
  for name in ['decode', 'dump', 'serde_json', 'CallOptions', 'DateIso', 'HttpCall', 'HttpEndpoint', 'Result', 'TimestampMillis']:
    imports.add('truewire_core', name)
  assert imports.render() == [
    'use truewire_core::{\n'
    '    decode, dump, serde_json, CallOptions, DateIso, HttpCall, HttpEndpoint, Result, TimestampMillis,\n'
    '};'
  ]


def test_a_router_whose_child_router_is_all_hand_written_keeps_its_core():
  """Every endpoint of a router hand-written: nothing renders for it, so its parent keeps
  `pub(crate) core` for the hand-written accessor (bitget's `classic_streams.order`)."""
  from truewire.codegen.rust.routers import render_router
  import inspect
  source = inspect.getsource(render_router)
  assert "routers.get((*router.path, child.name)) is None" in source


def test_the_raw_twin_of_a_keyword_method_drops_the_escape():
  """bitget's `uta.account.collateral.type`: the method is `type_`, its twin `type_raw`
  (`type__raw` is not snake_case, so rustc warns and clippy -D warnings fails)."""
  from truewire.codegen.rust.endpoint import raw_name
  assert raw_name('type_') == 'type_raw'
  assert raw_name('get') == 'get_raw'
  assert raw_name('type_') != 'type__raw'


def test_tuple_rows_allow_type_complexity_and_unit_responses_are_not_bound():
  """bitget: a walker or field over inline candle tuples trips clippy's `type_complexity`;
  a `null` payload renders `Response = ()`, which `dispatch.rs` must not bind (`let_unit_value`)."""
  from truewire.codegen.rust.dispatch import _arm
  from truewire.codegen.rust.endpoint import EndpointModule, Method, emit_method
  m = _module({}, {})
  emit_method(m, Method('candles_paged', [], [('text', 'PaginatedResponse<(i64, String), i64>')], is_async=False, complex_type=True), qualifier=None, body=['todo!()'])
  assert '#[allow(clippy::type_complexity)]\npub fn candles_paged(' in m.writer.render()
  w = Writer()
  unit = EndpointModule(file='a/b.rs', struct_name='B', bound='HttpEndpoint', meta_type=None, methods=[], source='', main='b', raw='b_raw', unit_response=True)
  _arm(w, 'a.b', unit, subject='request', raw=False, imports=Imports())
  out = w.render()
  assert 'let response' not in out and 'Ok(serde_json::Value::Null)' in out


def test_only_a_long_tuple_counts_as_complex():
  from truewire.codegen.rust.printer import long_tuple
  assert long_tuple('Vec<(String, DecimalString, DecimalString, DecimalString, DecimalString)>')
  assert not long_tuple('Vec<(DecimalString, DecimalString)>')


def test_a_walker_condition_past_the_line_width_breaks_as_rustfmt_does():
  """Kucoin's page walks: the brace alone overflows, or the `||` operands must break."""
  from truewire.codegen.rust.endpoint import if_block
  from truewire.codegen.rust.printer import Writer

  def render(condition: str) -> str:
    w = Writer(4)
    with if_block(w, condition):
      w.line('return Ok((rows, None));')
    return w.render()

  assert render('rows.is_empty()') == '                if rows.is_empty() {\n                    return Ok((rows, None));\n                }\n'
  assert render('rows.is_empty() || total_reached(true, current_page, 1, size, rows.len(), total)') == (
    '                if rows.is_empty() || total_reached(true, current_page, 1, size, rows.len(), total)\n'
    '                {\n'
    '                    return Ok((rows, None));\n'
    '                }\n'
  )
  assert render('rows.is_empty() || total.is_some_and(|total| total_reached(true, page, 1, size, rows.len(), total))') == (
    '                if rows.is_empty()\n'
    '                    || total\n'
    '                        .is_some_and(|total| total_reached(true, page, 1, size, rows.len(), total))\n'
    '                {\n'
    '                    return Ok((rows, None));\n'
    '                }\n'
  )


def test_router_modules_are_ordered_by_name_as_rustfmt_does():
  from truewire.codegen.rust.routers import _declared_module
  lines = ['pub mod orderbook_level50;', 'pub mod orderbook_level5;', 'pub mod klines;']
  assert sorted(lines, key=_declared_module) == ['pub mod klines;', 'pub mod orderbook_level5;', 'pub mod orderbook_level50;']


def test_a_walker_condition_measures_the_column_its_body_finally_lands_at():
  """A walker body is rendered one level shallower than the method it is placed in: the
  101-column `if` of kucoin's page walks must break although its own writer sees 97."""
  from truewire.codegen.rust.endpoint import if_block
  from truewire.codegen.rust.printer import Writer

  condition = 'rows.is_empty() || total_reached(true, current_page, 1, size, rows.len(), total)'
  shallow, deep = Writer(3), Writer(4)
  with if_block(shallow, condition, offset=4):
    shallow.line('return Ok((rows, None));')
  with if_block(deep, condition):
    deep.line('return Ok((rows, None));')
  assert shallow.render().replace('\n    ', '\n').lstrip() != shallow.render()
  assert [line.strip() for line in shallow.render().splitlines()] == [line.strip() for line in deep.render().splitlines()]
  assert '{' == shallow.render().splitlines()[1].strip()


def test_router_module_declarations_sort_by_name_as_rustfmt_does():
  from truewire.codegen.rust.routers import _declared_module
  lines = ['pub mod register_asset2;', 'pub mod register_asset;', 'pub mod halt_trading;']
  assert sorted(lines, key=_declared_module) == ['pub mod halt_trading;', 'pub mod register_asset;', 'pub mod register_asset2;']


def test_a_long_type_alias_breaks_after_the_equals_as_rustfmt_does():
  w = Writer()
  w.type_alias('pub type Short = ', '(String, i64)')
  w.type_alias('pub type GossipPriorityAuctionStatusResponse = ', '(Vec<Option<String>>, Vec<GossipPriorityAuctionSlotStatus>)')
  assert w.render() == (
    'pub type Short = (String, i64);\n'
    'pub type GossipPriorityAuctionStatusResponse =\n'
    '    (Vec<Option<String>>, Vec<GossipPriorityAuctionSlotStatus>);\n'
  )


def test_a_router_of_only_hand_written_endpoints_holds_their_core(tmp_path: Path):
  """mexc's protobuf-framed spot streams: every endpoint of a router hand-written. The router
  is still rendered, holding the core its endpoints would (`pub(crate) core`), so the methods
  written beside it have a struct and a transport; its parent builds it like any child."""
  root = _with_rust(FIXTURE_ROOT, tmp_path / 'client')
  for name in ('history', 'list'):
    path = root / 'spec' / 'endpoints' / 'account' / 'deposits' / name / 'endpoint.json'
    spec = json.loads(path.read_text())
    spec['surface'] = {'kind': 'handwritten', 'symbol': f'account.deposits.{name}:{name}', 'reason': 'test'}
    path.write_text(json.dumps(spec))
  project = load_project(root)
  rendered = render_package(build_plan(project), project)
  assert 'account/deposits/history.rs' not in rendered.files
  deposits = rendered.files['account/deposits/mod.rs']
  assert 'pub mod ' not in deposits
  assert '    pub(crate) core: Arc<dyn HttpEndpoint' in deposits
  assert 'Self { core }' in deposits
  assert 'deposits: deposits::Deposits::new(' in rendered.files['account/mod.rs']
  assert 'pub mod deposits;' in rendered.files['account/mod.rs']
  assert 'account.deposits' not in rendered.files['dispatch.rs']


def test_proto_sources_render_as_protos_rs(tmp_path: Path):
  """ADR 0016: `spec/proto/**/*.proto` is embedded verbatim as `protos::SOURCES`, escaped for
  Rust and declared by `lib.rs`; a project without one gets no such module."""
  from truewire.codegen.rust.protos import rust_string
  root = _with_rust(FIXTURE_ROOT, tmp_path / 'client')
  project = load_project(root)
  assert 'protos.rs' not in render_package(build_plan(project), project).files
  (root / 'spec' / 'proto' / 'nested').mkdir(parents=True)
  (root / 'spec' / 'proto' / 'nested' / 'push.proto').write_text('syntax = "proto3";\n// a \\ "quote"\tend\n')
  (root / 'spec' / 'proto' / 'a.proto').write_text('x')
  project = load_project(root)
  rendered = render_package(build_plan(project), project)
  protos = rendered.files['protos.rs']
  assert '    ("a.proto", "x"),\n' in protos
  assert '    ("nested/push.proto", "syntax = \\"proto3\\";\\n// a \\\\ \\"quote\\"\\tend\\n"),\n' in protos
  (root / 'spec' / 'proto' / 'wide.proto').write_text('// ' + 'x' * 120 + '\n')
  project = load_project(root)
  wide = render_package(build_plan(project), project).files['protos.rs']
  assert '    (\n        "wide.proto",\n        "// ' + 'x' * 120 + '\\n",\n    ),\n' in wide
  assert 'pub mod protos;' in rendered.files['lib.rs']
  assert rust_string('\x01') == '"\\u{1}"'


def test_a_combined_trait_names_the_smaller_combined_traits_it_contains_as_supertraits():
  """mexc's `user_client` is held as `CommandEndpoint + StreamEndpoint + StreamEndpoint<M>` and
  handed to a router holding `CommandEndpoint + StreamEndpoint`: a trait object upcasts only to
  a supertrait, so the larger combined trait declares the smaller one."""
  from truewire.codegen.rust.contract import Contracts
  from truewire.codegen.rust.printer import Imports
  contracts = Contracts()
  contracts.sets.add(('CommandEndpoint', 'StreamEndpoint'))
  contracts.sets.add(('CommandEndpoint', 'StreamEndpoint', 'StreamEndpoint<SpotMeta>'))
  contracts.sets.add(('StreamEndpoint', 'StreamEndpoint<SpotMeta>'))
  rendered = contracts.render()
  assert 'pub trait CommandStreamEndpoint: CommandEndpoint + StreamEndpoint {}' in rendered
  assert (
    'CommandEndpoint + StreamEndpoint + StreamEndpoint<SpotMeta> + CommandStreamEndpoint + StreamStreamSpotEndpoint'
    in ' '.join(rendered.split())
  )
  wide = Contracts()
  wide.sets.add(('CommandEndpoint', 'StreamEndpoint'))
  wide.sets.add(('CommandEndpoint', 'StreamEndpoint', 'StreamEndpoint<SpotStreamsEndpointMeta>'))
  wide.sets.add(('StreamEndpoint', 'StreamEndpoint<SpotStreamsEndpointMeta>'))
  assert (
    'pub trait CommandStreamStreamSpotStreamsEndpointEndpoint:\n    CommandEndpoint\n    + StreamEndpoint\n'
    '    + StreamEndpoint<SpotStreamsEndpointMeta>\n    + CommandStreamEndpoint\n'
    '    + StreamStreamSpotStreamsEndpointEndpoint\n{\n}\n'
  ) in wide.render()
  assert 'pub trait StreamStreamSpotEndpoint: StreamEndpoint + StreamEndpoint<SpotMeta> {}' in rendered


def test_a_struct_literal_call_past_fn_call_width_breaks_its_arguments():
  """mexc's composite root: `futures: Futures::new(a, b, c)` fits the line, but its arguments
  pass `fn_call_width`, so `rustfmt` puts one per line."""
  w = Writer(3)
  w.field('futures: Futures::new(http_client.clone(), market_client.clone(), user_client.clone())')
  assert w.render() == (
    '            futures: Futures::new(\n'
    '                http_client.clone(),\n'
    '                market_client.clone(),\n'
    '                user_client.clone(),\n'
    '            ),\n'
  )


def test_an_overflowing_impl_parameter_breaks_one_bound_per_line():
  w = Writer(1)
  w.signature('pub fn from_cores', [
    "http_client: impl HttpEndpoint<FuturesHttpMeta> + HttpEndpoint<SpotHttpMeta> + 'static",
    "user_client: impl CommandEndpoint + StreamEndpoint + StreamEndpoint<SpotStreamsEndpointMeta> + 'static",
  ], ' -> Self {')
  assert w.render() == (
    '    pub fn from_cores(\n'
    "        http_client: impl HttpEndpoint<FuturesHttpMeta> + HttpEndpoint<SpotHttpMeta> + 'static,\n"
    '        user_client: impl CommandEndpoint\n'
    '            + StreamEndpoint\n'
    '            + StreamEndpoint<SpotStreamsEndpointMeta>\n'
    "            + 'static,\n"
    '    ) -> Self {\n'
  )


def test_an_overflowing_terminator_condition_breaks_at_or_as_rustfmt_does():
  """mexc's `funding_records` page walk ended by an optional `total`: one column wider than
  kucoin's, the `.is_some_and(...)` line would pass 100, so `rustfmt` keeps the chain whole
  and puts the closure's body in a block."""
  from truewire.codegen.rust.endpoint import if_block
  w = Writer(4)
  with if_block(w, 'rows.is_empty() || total.is_some_and(|total| total_reached(true, page_num, 1, size, rows.len(), total))'):
    w.line('return Ok((rows, None));')
  assert w.render() == (
    '                if rows.is_empty()\n'
    '                    || total.is_some_and(|total| {\n'
    '                        total_reached(true, page_num, 1, size, rows.len(), total)\n'
    '                    })\n'
    '                {\n'
    '                    return Ok((rows, None));\n'
    '                }\n'
  )
  short = Writer(4)
  with if_block(short, 'exhausted(rows.len(), size)'):
    pass
  assert short.render() == '                if exhausted(rows.len(), size) {\n                }\n'


def test_a_channel_that_is_one_placeholder_is_that_value_without_format(fixture_rendered):
  """binance's listenKey streams: `format!("{}", x)` would be clippy's `useless_format`."""
  user = fixture_rendered.files['market/user_stream.rs']
  assert '        let channel = query_value(&values["listenKey"]).unwrap_or_default();\n' in user
  assert 'format!' not in user


def test_a_combined_trait_exactly_as_wide_as_the_line_is_split():
  """rustfmt splits a declaration that reaches column 100, not only one past it (binance's
  `CommandWsRpcStreamWsRpcEndpoint` is exactly 100 wide on one line)."""
  from truewire.codegen.rust.contract import Contracts
  contracts = Contracts()
  contracts.sets.add(('CommandEndpoint<WsRpcMeta>', 'StreamEndpoint<WsRpcMeta>'))
  rendered = contracts.render()
  assert rendered is not None
  assert (
    'pub trait CommandWsRpcStreamWsRpcEndpoint:\n'
    '    CommandEndpoint<WsRpcMeta> + StreamEndpoint<WsRpcMeta>\n'
    '{\n'
    '}\n'
  ) in rendered


@pytest.mark.skipif(shutil.which('rustfmt') is None, reason='rustfmt is not installed')
def test_offset_page_total_condition_is_rustfmt_clean(tmp_path: Path):
  """The original 98-column operand stays whole when it fits the condition width."""
  from truewire.codegen.rust.endpoint import if_block

  w = Writer(4)
  with if_block(w, 'rows.is_empty() || size.is_some_and(|size| (from + rows.len() as i64) / size as i64 >= total)'):
    w.line('return Ok((rows, None));')
  assert '|| size.is_some_and(' in w.render()
  source = 'fn main() {\n    {\n        {\n            {\n' + w.render() + '            }\n        }\n    }\n}\n'
  target = tmp_path / 'condition.rs'
  target.write_text(source)
  checked = subprocess.run(['rustfmt', '--edition', '2021', '--check', str(target)], capture_output=True, text=True)
  assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.skipif(shutil.which('rustfmt') is None, reason='rustfmt is not installed')
def test_optional_offset_page_total_is_rustfmt_clean(tmp_path: Path):
  root = tmp_path / 'walkers'
  shutil.copytree(WALKERS_ROOT, root)
  path = root / 'spec/endpoints/market/withdrawal_pages/endpoint.json'
  doc = json.loads(path.read_text())
  doc['spec']['response']['required'].remove('pages')
  path.write_text(json.dumps(doc))
  rendered = render_package(build_plan(root))
  target = tmp_path / 'withdrawal_pages.rs'
  target.write_text(rendered.files['market/withdrawal_pages.rs'])
  checked = subprocess.run(['rustfmt', '--edition', '2021', '--check', str(target)], capture_output=True, text=True)
  assert checked.returncode == 0, checked.stdout + checked.stderr
