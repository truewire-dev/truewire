"""The TypeScript backend (`truewire generate typescript`): the plan rendered as an ESM
package of interfaces, codecs, endpoint classes and delegating routers.

Three things are proved here. The renderers decide TypeScript's part of the plan the
documented way (scalar formats, nullable/optional shapes, `readonly` tuples, lazy codecs
for forward references, the naming rule). The whole package renders every walker shape the
fixture client declares, and the GitHub example's committed output is exactly what the
backend renders today. And the CLI keeps the same manifest discipline as the Python one
(`--check` reports drift, `--delete` removes only what it owns).
"""
import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.typescript import render_package, root_class_name
from truewire.codegen.typescript.endpoint import (
  RAW_OPTIONS, Method, Param, channel_expr, emit_signatures, raw_returns, read_path,
)
from truewire.codegen.typescript.meta import meta_module, meta_type
from truewire.codegen.typescript.names import binding, camel_case, literal, property_key, string
from truewire.codegen.typescript.printer import BANNER, Writer, relative_specifier
from truewire.codegen.typescript.routers import core_shapes
from truewire.codegen.typescript.types import Module
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


def _with_typescript(source: Path, target: Path) -> Path:
  """A copy of a project with a `[typescript]` section added."""
  shutil.copytree(source, target, ignore=shutil.ignore_patterns('node_modules', '.truewire', 'src'))
  toml = target / 'truewire.toml'
  toml.write_text(toml.read_text() + '\n[typescript]\npackage = "client"\nsrc = "ts"\nname = "Client"\n')
  return target


# -- naming and printing --------------------------------------------------------------


def test_naming_rule():
  """Truewire's own identifiers are camelCase; a reserved word gets a `$` binding."""
  assert camel_case('list_commits') == 'listCommits'
  assert camel_case('get') == 'get'
  assert camel_case('getRepo') == 'getRepo'
  assert camel_case('market-data') == 'marketData'
  assert binding('delete') == 'delete$'
  assert binding('get', {'get'}) == 'get$'
  assert property_key('per_page') == 'per_page'
  assert property_key('X-Rate') == "'X-Rate'"
  assert string("it's") == "'it\\'s'"
  assert literal({'public': True, 'n': 1, 'a': ['x', None]}) == "{ public: true, n: 1, a: ['x', null] }"


def test_printer_jsdoc_and_specifiers():
  w = Writer()
  w.jsdoc('One line.')
  w.jsdoc('Two\nlines.', tags=['@see x'])
  assert w.render() == '/** One line. */\n/**\n * Two\n * lines.\n *\n * @see x\n */\n'
  assert relative_specifier('issues/list.ts', 'types/index.ts') == '../types/index.js'
  assert relative_specifier('issues/index.ts', 'issues/list.ts') == './list.js'
  assert relative_specifier('main.ts', 'issues/index.ts') == './issues/index.js'
  assert relative_specifier('a/b/c.ts', 'meta.ts') == '../../meta.js'


def test_response_paths_are_optional_chained():
  assert read_path('response', 'data.rows', optional=False) == 'response.data?.rows'
  assert read_path('response', 'data.rows', optional=True) == 'response?.data?.rows'
  assert read_path('rows', '[-1].id', optional=False) == 'rows.at(-1)?.id'
  assert read_path('response', 'list[0].x-y', optional=False) == "response.list?.[0]?.['x-y']"


# -- types and codecs -----------------------------------------------------------------


def _module(types: dict, schemas: dict | None = None) -> Module:
  plan = PackagePlan(name='p', root_class='P', schemas=schemas or {})
  return Module(plan, 'x/y.ts', local=types, visible=[''])


def test_scalar_formats_render_to_core_types_and_codecs():
  m = _module({})
  cases = {
    ('string', None): ('string', 't.string'),
    ('integer', None): ('number', 't.integer'),
    ('number', None): ('number', 't.number'),
    ('boolean', None): ('boolean', 't.boolean'),
    ('null', None): ('null', 't.null'),
    ('any', None): ('unknown', 't.unknown'),
    ('string', 'decimal-string'): ('Decimal', 't.decimal'),
    ('string', 'integer-string'): ('bigint', 't.integerString'),
    ('string', 'boolean-string'): ('boolean', 't.booleanString'),
    ('integer', 'int64'): ('number | bigint', 't.int64'),
    ('integer', 'epoch-millis'): ('TimestampMillis', 't.epochMillis'),
    ('integer', 'epoch-seconds'): ('TimestampSeconds', 't.epochSeconds'),
    ('number', 'epoch-seconds'): ('TimestampSeconds', 't.epochSecondsFloat'),
    ('number', 'epoch-nanos'): ('TimestampNanos', 't.epochNanosFloat'),
    ('string', 'epoch-millis'): ('TimestampMillis', 't.epochMillis'),
    ('string', 'date-time'): ('TimestampIso', 't.dateTime'),
    ('string', 'date'): ('DateIso', 't.date'),
    ('string', 'uuid'): ('string', 't.string'),
  }
  for (base, fmt), (expected_type, expected_codec) in cases.items():
    t = {'type': 'scalar', 'base': base, **({'format': fmt} if fmt else {})}
    assert m.type_expr(t) == expected_type, (base, fmt)
    assert m.codec_expr(t) == expected_codec, (base, fmt)
  rendered = '\n'.join(m.imports.render())
  assert "type Decimal" in rendered and "type TimestampIso" in rendered and ' t }' in rendered


def test_composite_shapes():
  m = _module({})
  string = {'type': 'scalar', 'base': 'string'}
  null = {'type': 'scalar', 'base': 'null'}
  nullable = {'type': 'union', 'variants': [{'type': string}, {'type': null}]}
  assert m.type_expr(nullable) == 'string | null'
  assert m.codec_expr(nullable) == 't.nullable(t.string)'
  three = {'type': 'union', 'variants': [{'type': string}, {'type': {'type': 'scalar', 'base': 'integer'}}, {'type': null}]}
  assert m.codec_expr(three) == 't.union(t.string, t.integer, t.null)'
  lit = {'type': 'literal', 'values': ['a', 'b', 1]}
  assert m.type_expr(lit) == "'a' | 'b' | 1"
  assert m.codec_expr(lit) == "t.literal('a', 'b', 1)"
  assert m.type_expr({'type': 'list', 'item': lit}) == "('a' | 'b' | 1)[]"
  tup = {'type': 'tuple', 'items': [string, string]}
  assert m.type_expr(tup) == 'readonly [string, string]'
  assert m.type_expr({'type': 'list', 'item': tup}) == '(readonly [string, string])[]'
  assert m.codec_expr(tup) == 't.tuple([t.string, t.string])'
  assert m.type_expr({'type': 'dict', 'key': string, 'value': string}) == 'Record<string, string>'
  assert m.codec_expr({'type': 'dict', 'key': string, 'value': string}) == 't.record(t.string)'


def test_records_define_an_interface_and_a_codec_with_lazy_forward_references():
  node = {
    'type': 'record', 'id': 'Node', 'docstring': 'A node.',
    'fields': {
      'id': {'type': {'type': 'scalar', 'base': 'integer'}, 'required': True, 'docstring': 'Id.'},
      'children': {'type': {'type': 'list', 'item': {'type': 'ref', 'id': 'Node'}}, 'required': False},
      'label': {'type': {'type': 'ref', 'id': 'Label'}, 'required': True},
    },
  }
  label = {'type': 'record', 'id': 'Label', 'fields': {'text': {'type': {'type': 'scalar', 'base': 'string'}, 'required': True}}}
  m = _module({'Node': node, 'Label': label})
  m.define_all({'Node': node, 'Label': label})
  code = m.render(BANNER)
  assert '/** A node. */\nexport interface Node {\n  /** Id. */\n  id: number\n  children?: Node[]\n  label: Label\n}' in code
  assert 'children: t.optional(t.array(t.lazy(() => Node))),' in code
  assert 'label: t.lazy(() => Label),' in code
  assert 'export const Label: Codec<Label> = t.object({\n  text: t.string,\n})' in code


def test_shared_references_are_imported_from_their_scope():
  shared = {'': {'Label': {'type': 'record', 'id': 'Label', 'fields': {}}}, 'a': {'Deep': {'type': 'record', 'id': 'Deep', 'fields': {}}}}
  m = Module(PackagePlan(name='p', root_class='P', schemas=shared), 'a/b.ts', local={}, visible=['a', ''])
  assert m.type_expr({'type': 'ref', 'id': 'Label'}) == 'Label'
  assert m.codec_expr({'type': 'ref', 'id': 'Deep'}) == 'Deep'
  lines = m.imports.render()
  assert "import type { Label } from '../types/index.js'" in lines
  assert "import { Deep } from '../types/a.js'" in lines
  with pytest.raises(ValueError):
    m.type_expr({'type': 'ref', 'id': 'Missing'})


def test_meta_module_renders_one_interface_per_core_with_a_schema():
  cores = {
    'root': CorePlan(),
    'spot': CorePlan(meta={'type': 'object', 'properties': {'signed': {'type': 'boolean', 'description': 'Signed call.'}}}),
    'bare': CorePlan(meta={'type': 'object'}),
  }
  code = meta_module(cores)
  assert code is not None
  assert 'export interface SpotMeta {\n  /** Signed call. */\n  signed?: boolean\n}' in code
  assert 'export type BareMeta = Record<string, never>' in code
  assert 'RootMeta' not in code
  assert meta_module({'root': CorePlan()}) is None
  assert meta_type({'enum': ['a', 'b']}) == "'a' | 'b'"
  assert meta_type({'type': ['string', 'null']}) == 'string | null'
  assert meta_type({'type': 'array', 'items': {'type': 'integer'}}) == 'number[]'
  assert meta_type({'type': 'object'}) == 'Record<string, unknown>'


# -- the whole package ----------------------------------------------------------------


@pytest.fixture(scope='module')
def fixture_rendered():
  return render_package(build_plan(FIXTURE_ROOT))


def test_fixture_package_layout(fixture_rendered):
  files = fixture_rendered.files
  assert 'index.ts' in files and 'main.ts' in files and 'meta.ts' in files
  assert 'types/index.ts' in files
  assert 'market/index.ts' in files and 'market/order_list.ts' in files
  assert all(content.startswith(BANNER + '\n') for content in files.values())
  assert fixture_rendered.skipped == []
  assert 'token/nfts/list.ts' in files and 'market/ticker_stream.ts' in files
  assert 'export class FixtureClient' in files['main.ts']


def test_fixture_walkers_render_every_resumable_shape(fixture_rendered):
  files = fixture_rendered.files
  token = files['market/order_list.ts']
  assert 'orderListPaged(request: OrderListPagedRequest, options?: CallOptions): PaginatedResponse<OrderListItem, string>' in token
  assert "export type OrderListPagedRequest = Omit<Request, 'pageKey'>" in token
  assert 'pageKey: pageKey || undefined' in token
  assert "return new PaginatedResponse('', next)" in token
  nullable = files['market/order_nullable_list.ts']
  assert 'const rows = response?.orders ?? []' in nullable
  total = files['market/order_page_total.ts']
  assert 'let totalSeen: number | null = null' in total
  assert 'throw new LogicError(' in total
  assert 'return new PaginatedResponse(1, next)' in total


def test_a_token_walk_ending_on_an_empty_page_is_a_resumable_walker(tmp_path: Path):
  """bitget's `token` walks end on an empty page: a `PaginatedResponse` over the cursor, not
  a generator, stopping on the first page with no rows whatever cursor it carries."""
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('.truewire', 'core_impl'))
  spec = root / 'spec' / 'endpoints' / 'market' / 'order_list' / 'endpoint.json'
  raw = json.loads(spec.read_text())
  raw['pagination']['done'] = {'kind': 'empty', 'rows': 'orders'}
  spec.write_text(json.dumps(raw))
  rendered = render_package(build_plan(root))
  token = rendered.files['market/order_list.ts']
  assert 'PaginatedResponse<OrderListItem, string>' in token
  assert 'async *orderListPaged' not in token
  empty = token.index('if (rows.length === 0) return [rows, null]')
  assert empty < token.index('const following = ')


def test_methods_overload_validate_false_to_unknown(fixture_rendered):
  """Every method that returns a value carries two overload signatures ahead of its
  implementation: `validate: false` first (declaration order decides, and `CallOptions`
  would otherwise match it), returning `unknown` where the declared type was -- a
  walker keeps its state type -- then the declared signature. The implementation is
  unchanged, and a router delegates both, qualified."""
  files = fixture_rendered.files
  endpoint = files['market/order_list.ts']
  raw = f'  orderList(request: Request, options: {RAW_OPTIONS}): Promise<unknown>\n'
  typed = '  orderList(request: Request, options?: CallOptions): Promise<OrderListResponse>\n'
  implementation = '  async orderList(request: Request, options?: CallOptions): Promise<OrderListResponse> {\n'
  assert endpoint.index(raw) < endpoint.index(typed) < endpoint.index(implementation)
  assert (
    f'  orderListPaged(request: OrderListPagedRequest, options: {RAW_OPTIONS}): '
    'PaginatedResponse<unknown, string>\n'
  ) in endpoint
  assert endpoint.count('orderListPaged(request: OrderListPagedRequest, options?: CallOptions): PaginatedResponse<OrderListItem, string>') == 2
  router = files['market/index.ts']
  assert f'  orderList(request: orderList.Request, options: {RAW_OPTIONS}): Promise<unknown>\n' in router
  assert '  orderList(request: orderList.Request, options?: CallOptions): Promise<orderList.OrderListResponse>\n' in router


def test_emit_signatures_for_a_generator_walker_and_a_reply_less_method():
  """A generator walker's overloads are ordinary method signatures ahead of the
  `async *` implementation, with `unknown` as the yielded type; the raw one's request is
  required since its options object is required and follows it. A method returning
  nothing has nothing to overload and gets its JSDoc alone."""
  m = _module({'Req': {}, 'Res': {}})
  request = Param('request', ('local', 'Req'), optional=True)
  options = Param('options', ('core', 'CallOptions'), optional=True)
  generator = Method(
    'listPaged', [request, options], ('generator', (('local', 'Res'),)), doc=['Pages.'],
    generator=True,
  )
  emit_signatures(m, generator)
  assert m.writer.render().splitlines() == [
    '/** With `validate: false`: the parsed body as it came, typed `unknown`. */',
    f'listPaged(request: Req, options: {RAW_OPTIONS}): AsyncGenerator<unknown, void, undefined>',
    '/** Pages. */',
    'listPaged(request?: Req, options?: CallOptions): AsyncGenerator<Res, void, undefined>',
  ]
  m = _module({})
  emit_signatures(m, Method('ping', [options], ('void', ()), doc=['Ping.']))
  assert m.writer.render().splitlines() == ['/** Ping. */']
  assert raw_returns(('void', ())) is None
  assert raw_returns(('paginated', (('local', 'Row'), ('plan', {'type': 'scalar', 'base': 'integer'})))) == (
    'paginated', (('unknown', None), ('plan', {'type': 'scalar', 'base': 'integer'})),
  )


def test_stream_endpoint_subscribes_through_the_core(fixture_rendered):
  """A `stream` endpoint is a class over `StreamEndpoint<Meta>` whose method returns the
  core's `Subscription` of the pushed message, overloaded to `unknown` for `validate:
  false`. The fixture's stream is direct-channel: its parameters are exactly the
  channel's placeholders, so the module declares a `Parameters` interface with no codec,
  fills the template itself and hands the core no parameters object, as the Python
  backend fills the channel from its locals."""
  files = fixture_rendered.files
  stream = files['market/ticker_stream.ts']
  assert 'constructor(readonly core: StreamEndpoint<DefaultMeta>) {}' in stream
  assert 'export interface Parameters {\n  /** Trading pair. */\n  symbol: string\n}' in stream
  assert 'Parameters: Codec' not in stream
  raw = f'  tickerStream(parameters: Parameters, options: {RAW_OPTIONS}): Subscription<unknown>\n'
  typed = '  tickerStream(parameters: Parameters, options?: CallOptions): Subscription<Ticker>\n'
  assert stream.index(raw) < stream.index(typed)
  assert 'channel: `/ticker/${parameters.symbol}`,' in stream
  assert 'parameters: undefined,\n      parametersCodec: undefined,\n      messageCodec: Ticker,' in stream
  assert 'meta: { public: true },' in stream
  assert 'type StreamEndpoint, type Subscription' in stream.splitlines()[1]
  router = files['market/index.ts']
  assert 'tickerStream(parameters: tickerStream.Parameters, options?: CallOptions): Subscription<tickerStream.Ticker> {' in router
  assert 'constructor(readonly core: HttpEndpoint<DefaultMeta> & StreamEndpoint<DefaultMeta>) {' in router


def test_channel_expr():
  """The channel a direct-channel or connect-only stream subscribes with: a template
  literal over the parameters, or the bare value when the whole channel is one placeholder."""
  assert channel_expr('{listenKey}', 'parameters') == 'parameters.listenKey'
  assert channel_expr('/ticker/{symbol}', 'parameters') == '`/ticker/${parameters.symbol}`'
  assert channel_expr('{pair}@kline_{interval}', 'p') == '`${p.pair}@kline_${p.interval}`'
  assert channel_expr('a`b${ {x-y}', 'p') == "`a\\`b\\${ ${p['x-y']}`"
  assert channel_expr('plain', 'p') == '`plain`'


def test_fixture_routers_delegate_with_qualified_types(fixture_rendered):
  router = fixture_rendered.files['market/index.ts']
  assert "import * as orderList from './order_list.js'" in router
  assert 'private readonly orderList_: orderList.OrderList' in router
  assert 'this.orderList_ = new orderList.OrderList(core)' in router
  assert 'orderList(request: orderList.Request, options?: CallOptions): Promise<orderList.OrderListResponse> {' in router
  assert 'return this.orderList_.orderList(request, options)' in router


def test_root_class_name_prefers_the_typescript_section(tmp_path: Path):
  project = load_project(_with_typescript(FIXTURE_ROOT, tmp_path / 'client'))
  plan = build_plan(project)
  assert root_class_name(plan, None) == plan.root_class
  assert root_class_name(plan, project) == 'Client'


def test_github_example_output_is_what_the_backend_renders():
  """The committed `examples/github/src/github/**/*.ts` is the backend's current output;
  a change to either side shows up here before CI's `generate typescript --check`."""
  root = _example('github')
  project = load_project(root)
  rendered = render_package(build_plan(project), project)
  package = project.typescript_package_dir
  manifest = json.loads((root / '.truewire' / 'codegen' / 'typescript.json').read_text())
  for path, content in rendered.files.items():
    assert (package / path).read_text() == content, path
  assert sorted(manifest['files']) == sorted(rendered.files)
  assert rendered.skipped == []
  assert 'listCommitsPaged(request: ListCommitsPagedRequest, options?: CallOptions): PaginatedResponse<Commit, number>' in rendered.files['repos/list_commits.ts']


def test_kraken_example_output_is_what_the_backend_renders():
  """`examples/kraken` is the composite and stream case: every `.ts` under `src/kraken`
  is the backend's current output, its root takes a fields object, and a composite
  child receives the whole object while a plain child receives its declared field."""
  root = _example('kraken')
  project = load_project(root)
  plan = build_plan(project)
  rendered = render_package(plan, project)
  package = project.typescript_package_dir
  for path, content in rendered.files.items():
    assert (package / path).read_text() == content, path
  assert rendered.skipped == []

  shapes = core_shapes(plan, {})
  assert shapes[()].composite and shapes[('streams',)].composite
  assert not shapes[('spot',)].composite and not shapes[('streams', 'market_data')].composite
  main = rendered.files['main.ts']
  assert (
    'export interface KrakenCore {\n'
    '  market_client: CommandEndpoint & StreamEndpoint\n'
    '  private_client: CommandEndpoint & StreamEndpoint\n'
    '  spot_client: HttpEndpoint<SpotMeta>\n'
    '}'
  ) in main
  assert 'constructor(readonly core: KrakenCore) {' in main
  assert 'this.spot = new Spot(core.spot_client)' in main
  assert 'this.streams = new Streams(core)' in main
  assert 'this.tradingWs = new TradingWs(core.private_client)' in main
  streams = rendered.files['streams/index.ts']
  assert 'export interface StreamsCore {\n  market_client: CommandEndpoint & StreamEndpoint\n  private_client: StreamEndpoint\n}' in streams
  assert 'this.marketData = new MarketData(core.market_client)' in streams
  assert 'this.private = new Private(core.private_client)' in streams
  assert "export { Kraken, type KrakenCore } from './main.js'" in rendered.files['index.ts']
  ticker = rendered.files['streams/market_data/ticker.ts']
  assert 'ticker(parameters: Parameters, options?: CallOptions): Subscription<TickerMessage> {' in ticker
  assert "channel: 'ticker',\n      parameters,\n      parametersCodec: Parameters,\n      messageCodec: TickerMessage,\n      meta: {}," in ticker
  balances = rendered.files['streams/private/balances.ts']
  assert 'balances(parameters?: Parameters, options?: CallOptions): Subscription<Payload> {' in balances
  assert 'parameters: parameters ?? {},' in balances
  status = rendered.files['streams/market_data/status.ts']
  assert 'status(options?: CallOptions): Subscription<StatusMessage> {' in status
  assert 'parameters: undefined,\n      parametersCodec: undefined,' in status

  # `spot.account.retrieve_export` declares `surface: handwritten`: no module is rendered for
  # it, and `[typescript.extras."spot.account"]` folds the hand-written class into its router.
  assert 'spot/account/retrieve_export.ts' not in rendered.files
  account = rendered.files['spot/account/index.ts']
  assert "import { RetrieveExport } from './retrieve_export.js'" in account
  assert 'private readonly retrieveExport_: RetrieveExport' in account
  assert "readonly retrieveExport: RetrieveExport['retrieveExport']" in account
  assert 'this.retrieveExport_ = new RetrieveExport(core)' in account
  assert 'this.retrieveExport = this.retrieveExport_.retrieveExport.bind(this.retrieveExport_)' in account


def test_a_plain_router_over_a_composite_child_takes_the_fields_object():
  """A router whose own core declares nothing but whose child is a composite has to carry
  the child's fields: it takes the fields object too, its own endpoints under `client`."""
  plan = PackagePlan.model_validate({
    'name': 'p', 'rootClass': 'P',
    'cores': {'root': {}, 'group': {'children': {'feed': 'socket'}}, 'plain': {}},
    'schemas': {},
    'routers': [
      {'path': [], 'core': 'root', 'children': [{'name': 'ping', 'kind': 'endpoint', 'class': 'Ping'}, {'name': 'group', 'kind': 'router', 'class': 'Group'}]},
      {'path': ['group'], 'core': 'group', 'children': [{'name': 'feed', 'kind': 'router', 'class': 'Feed'}, {'name': 'status', 'kind': 'endpoint', 'class': 'Status'}]},
      {'path': ['group', 'feed'], 'core': 'plain', 'children': [{'name': 'ticks', 'kind': 'endpoint', 'class': 'Ticks'}]},
    ],
    'endpoints': [
      {'path': ['ping'], 'kind': 'rpc', 'transports': ['http'], 'wire': {'path': '/ping', 'method': 'GET'}, 'core': 'root', 'request': {'shape': 'none'}, 'response': {}},
      {'path': ['group', 'status'], 'kind': 'rpc', 'transports': ['http'], 'wire': {'path': '/status', 'method': 'GET'}, 'core': 'group', 'request': {'shape': 'none'}, 'response': {}},
      {'path': ['group', 'feed', 'ticks'], 'kind': 'stream', 'transports': ['ws'], 'wire': {'channel': 'ticks'}, 'core': 'plain', 'request': {'shape': 'none'}, 'response': {}, 'stream': {}},
    ],
  })
  rendered = render_package(plan)
  assert rendered.skipped == []
  main = rendered.files['main.ts']
  assert 'export interface PCore {\n  client: HttpEndpoint\n  socket: StreamEndpoint\n}' in main
  assert 'this.ping_ = new ping.Ping(core.client)' in main
  assert 'this.group = new Group(core)' in main
  group = rendered.files['group/index.ts']
  assert 'export interface GroupCore {\n  client: HttpEndpoint\n  socket: StreamEndpoint\n}' in group
  assert 'this.feed = new Feed(core.socket)' in group
  assert 'this.status_ = new status.Status(core.client)' in group
  assert 'constructor(readonly core: StreamEndpoint) {' in rendered.files['group/feed/index.ts']
  ticks = rendered.files['group/feed/ticks.ts']
  assert 'ticks(options?: CallOptions): Subscription<unknown> {' in ticks
  assert ticks.count('ticks(options') == 1, 'no second overload when the message is unknown already'
  assert 'messageCodec: undefined,' in ticks


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
  """`children = { app = "app_client" }` on a composite child renames the child's default
  field on the parent: the root takes `app_client`, and hands it to `App` as `client`."""
  rendered = render_package(_renamed_composite_plan())
  main = rendered.files['main.ts']
  assert 'export interface PCore {\n  app_client: HttpEndpoint\n  client: HttpEndpoint\n  socket: StreamEndpoint\n}' in main
  assert 'this.app = new App({ client: core.app_client, socket: core.socket })' in main
  assert 'this.ping_ = new ping.Ping(core.client)' in main
  app = rendered.files['app/index.ts']
  assert 'export interface AppCore {\n  client: HttpEndpoint\n  socket: StreamEndpoint\n}' in app
  assert 'this.status_ = new status.Status(core.client)' in app


# -- the command ----------------------------------------------------------------------


def test_generate_typescript_writes_checks_and_deletes(tmp_path: Path):
  root = _with_typescript(FIXTURE_ROOT, tmp_path / 'client')
  runner = CliRunner()
  result = runner.invoke(app, ['generate', 'typescript', '--project', str(root)])
  assert result.exit_code == 0, result.output
  manifest = root / '.truewire' / 'codegen' / 'typescript.json'
  assert manifest.is_file()
  files = json.loads(manifest.read_text())['files']
  assert 'main.ts' in files and 'market/order_list.ts' in files
  package = root / 'ts' / 'client'
  assert (package / 'main.ts').read_text().startswith(BANNER)
  assert 'export class Client' in (package / 'main.ts').read_text()

  check = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--check'])
  assert check.exit_code == 0, check.output
  assert 'No manifest' not in check.output

  # A fresh clone: no manifest, and the plan stands in for it.
  manifest.unlink()
  unmanifested = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--check'])
  assert unmanifested.exit_code == 0, unmanifested.output
  assert 'No manifest at .truewire/codegen/typescript.json; the plan stood in for it' in unmanifested.output
  assert not manifest.exists()
  assert runner.invoke(app, ['generate', 'typescript', '--project', str(root)]).exit_code == 0
  assert manifest.is_file()

  (package / 'main.ts').write_text('// edited\n')
  drifted = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--check'])
  assert drifted.exit_code == 1
  assert 'out of date: main.ts' in drifted.output

  (package / 'core.ts').write_text('export {}\n')
  deleted = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--delete'])
  assert deleted.exit_code == 0, deleted.output
  assert not (package / 'main.ts').exists()
  assert (package / 'core.ts').exists()
  assert not manifest.exists()


def test_generate_typescript_moves_a_legacy_manifest(tmp_path: Path):
  """W16 through `generate_planned`: a manifest at `.truewire/typescript-files.json` moves to
  `.truewire/codegen/typescript.json`, and a file it owned that the plan no longer renders
  is removed."""
  root = _with_typescript(FIXTURE_ROOT, tmp_path / 'client')
  runner = CliRunner()
  assert runner.invoke(app, ['generate', 'typescript', '--project', str(root)]).exit_code == 0
  manifest = root / '.truewire' / 'codegen' / 'typescript.json'
  legacy = root / '.truewire' / 'typescript-files.json'
  owned = json.loads(manifest.read_text())
  legacy.write_text(json.dumps({**owned, 'files': sorted([*owned['files'], 'gone.ts'])}))
  manifest.unlink()
  gone = root / 'ts' / 'client' / 'gone.ts'
  gone.write_text('export {}\n')

  stale = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--check'])
  assert stale.exit_code == 1
  assert '- stale manifest: .truewire/typescript-files.json' in stale.output
  assert '- no longer planned: gone.ts' in stale.output

  moved = runner.invoke(app, ['generate', 'typescript', '--project', str(root)])
  assert moved.exit_code == 0, moved.output
  assert 'Moved manifest .truewire/typescript-files.json to .truewire/codegen/typescript.json.' in moved.output
  assert not legacy.exists() and not gone.exists()
  assert json.loads(manifest.read_text()) == owned
  check = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--check'])
  assert check.exit_code == 0, check.output


def test_generate_typescript_needs_a_typescript_section(tmp_path: Path):
  shutil.copytree(FIXTURE_ROOT, tmp_path / 'client', ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  result = CliRunner().invoke(app, ['generate', 'typescript', '--project', str(tmp_path / 'client')])
  assert result.exit_code == 1
  assert 'no [typescript] section' in result.output


DUAL_FIXTURE = Path(__file__).parents[2] / 'testing-ts' / 'test' / 'fixture'


def test_a_dual_transport_endpoint_is_rendered_in_full():
  """An `rpc` endpoint declaring both `http` and `ws` is no longer skipped: its class takes
  a `DualEndpoint`, its method a `TransportOptions` whose `transport` defaults to the
  first-declared transport, and both overloads keep the option. The committed output of
  `packages/testing-ts/test/fixture` (proved against the mock over both transports by that
  package's own tests) is what the backend renders."""
  if not (DUAL_FIXTURE / 'truewire.toml').is_file():
    pytest.skip('packages/testing-ts is not checked out beside the package')
  project = load_project(DUAL_FIXTURE)
  rendered = render_package(build_plan(project), project)
  assert rendered.skipped == []
  package = project.typescript_package_dir
  for path, content in rendered.files.items():
    assert (package / path).read_text() == content, path

  get_pet = rendered.files['pets/get_pet.ts']
  assert "import { type Codec, type DualEndpoint, type TransportOptions, t } from '@truewire/core'" in get_pet
  assert 'constructor(readonly core: DualEndpoint) {}' in get_pet
  assert 'getPet(request: Request, options: TransportOptions & { validate: false }): Promise<unknown>' in get_pet
  assert 'async getPet(request: Request, options?: TransportOptions): Promise<Pet> {' in get_pet
  assert "      method: 'POST',\n      path: 'pets_get'," in get_pet
  assert "      ...options,\n      transport: options?.transport ?? 'http',\n" in get_pet
  assert "transport: options?.transport ?? 'ws'," in rendered.files['pets/list_pets.ts']
  # The root also holds `pets.adoptions`, reached over HTTP only.
  assert 'constructor(readonly core: DualEndpoint & HttpEndpoint) {' in rendered.files['main.ts']


def test_a_seek_walk_renders_one_call_to_the_runtime_walker():
  """ADR 0013's `seek` walk is rendered, not refused: the whole request (both bounds) is the
  walker's argument, the moving bound seeds `seek`, the cursor field becomes a `rowField`
  path, the caller's `limit` is clamped once and is the cap before the size's schema default,
  and a page is fetched through the plain method with the clamped `limit` and the moving
  bound replaced. The implementation
  signature widens to the raw overload's type; the overloads stay exact."""
  if not (DUAL_FIXTURE / 'truewire.toml').is_file():
    pytest.skip('packages/testing-ts is not checked out beside the package')
  project = load_project(DUAL_FIXTURE)
  rendered = render_package(build_plan(project), project)
  assert rendered.skipped == []
  adoptions = rendered.files['pets/adoptions.ts']
  assert 'has no TypeScript walker' not in adoptions
  assert "import { type CallOptions, type Codec, type HttpEndpoint, PaginatedResponse, type SeekState, rowField, seek, t } from '@truewire/core'" in adoptions or (
    'type SeekState' in adoptions and 'rowField' in adoptions and ' seek,' in adoptions
  )
  assert 'export type AdoptionsPagedRequest = Request' in adoptions
  assert (
    'adoptionsPaged(request?: AdoptionsPagedRequest, options?: CallOptions): '
    'PaginatedResponse<Adoption, SeekState<Adoption, number>>\n'
  ) in adoptions
  assert (
    "adoptionsPaged(request: AdoptionsPagedRequest, options: CallOptions & { validate: false }): "
    "PaginatedResponse<unknown, SeekState<unknown, number>>\n"
  ) in adoptions
  assert (
    'adoptionsPaged(request: AdoptionsPagedRequest = {}, options?: CallOptions): '
    'PaginatedResponse<Adoption, SeekState<Adoption, number>> | PaginatedResponse<unknown, SeekState<unknown, number>> {'
  ) in adoptions
  assert (
    "    return seek<Adoption, number>(request.from, {\n"
    "      method: 'adoptionsPaged',\n"
    "      field: 'at',\n"
    "      read: row => rowField(row, ['at']),\n"
    "      keys: 'number',\n"
    "      unique: true,\n"
    "      descending: false,\n"
    "      cap: size ?? 3,\n"
    "      far: request.to,\n"
  ) in adoptions
  assert '    const size = request.limit == null ? undefined : Math.min(Math.max(request.limit, 2), 500)\n    return seek<' in adoptions
  assert 'const response = await this.adoptions({ ...request, limit: size, from: pos }, options)' in adoptions
  assert 'The walk requests pages of at least 2 rows and at most 500: a page must hold one new row beside the one it re-reads.' in adoptions
  # The hover doc speaks of the row's own field, as Python's docstring does: no spec path,
  # no ADR number.
  assert (
    'Paged variant of `adoptions`: walks forwards by moving `from` to the latest `at` of each '
    "page that came back full, never past the caller's own `to`; awaitable"
  ) in adoptions
  walks = [line for line in adoptions.splitlines() if 'Paged variant' in line]
  assert walks and not [line for line in walks if re.search(r'\[-1\]|\[0\]|ADR \d|extreme', line)]
  router = rendered.files['pets/index.ts']
  assert 'PaginatedResponse<adoptions.Adoption, SeekState<adoptions.Adoption, number>> | PaginatedResponse<unknown, SeekState<unknown, number>> {' in router


def _stream_project(root: Path, *, reply: dict | None) -> Path:
  """A one-stream project: `streams.book`, with or without a declared `reply` (ADR 0014)."""
  (root / 'spec' / 'endpoints' / 'streams' / 'book').mkdir(parents=True)
  (root / 'truewire.toml').write_text(
    '[project]\nname = "venue"\n\n[typescript]\npackage = "venue"\nsrc = "src"\nname = "Venue"\n'
  )
  (root / 'spec' / 'endpoints' / 'streams' / 'router.json').write_text(json.dumps({
    'description': 'Streams.', 'upstream': 'https://venue.example/ws', 'core': 'socket',
  }))
  spec: dict = {
    'kind': 'stream', 'channel': 'book', 'description': 'Order book.',
    'parameters': {
      'title': 'BookParams', 'type': 'object', 'description': 'Subscribe parameters.',
      'required': ['symbol'], 'properties': {'symbol': {'type': 'string', 'description': 'Market.'}},
    },
    'payload': {
      'title': 'BookUpdate', 'type': 'object', 'description': 'One update.',
      'required': ['levels'],
      'properties': {'levels': {'type': 'array', 'description': 'Levels.', 'items': {'type': 'array', 'description': 'Price and size.', 'prefixItems': [{'type': 'string', 'description': 'Price.'}, {'type': 'string', 'description': 'Size.'}]}}},
    },
  }
  if reply is not None:
    spec['reply'] = reply
  (root / 'spec' / 'endpoints' / 'streams' / 'book' / 'endpoint.json').write_text(json.dumps({
    'meta': {}, 'spec': spec,
  }))
  return root


def test_a_stream_reply_types_the_subscription_acknowledgement(tmp_path: Path):
  """ADR 0014: a stream declaring `reply` takes a `ReplyStreamEndpoint`, returns
  `Subscription<Message, Reply>` and hands the core `replyCodec`; its `validate: false`
  overload leaves the reply raw as well. A stream without one renders as before."""
  reply = {
    'title': 'BookSnapshot', 'type': 'object', 'description': 'The whole book, as the ack carries it.',
    'required': ['bids'],
    'properties': {'bids': {'type': 'array', 'description': 'Bids.', 'items': {'type': 'object', 'description': 'One level.', 'required': ['price'], 'properties': {'price': {'type': 'string', 'format': 'decimal-string', 'description': 'Price.'}}}}},
  }
  project = load_project(_stream_project(tmp_path / 'with', reply=reply))
  plan = build_plan(project)
  book = render_package(plan, project).files['streams/book.ts']
  assert plan.endpoints[0].stream is not None and plan.endpoints[0].stream.reply == 'BookSnapshot'
  assert 'export interface BookSnapshot {' in book
  assert 'constructor(readonly core: ReplyStreamEndpoint) {}' in book
  assert 'book(parameters: Parameters, options: CallOptions & { validate: false }): Subscription<unknown, unknown>' in book
  assert 'book(parameters: Parameters, options?: CallOptions): Subscription<BookUpdate, BookSnapshot> {' in book
  assert '      messageCodec: BookUpdate,\n      replyCodec: BookSnapshot,\n      meta: {},' in book

  plain_project = load_project(_stream_project(tmp_path / 'without', reply=None))
  plain = render_package(build_plan(plain_project), plain_project).files['streams/book.ts']
  assert 'constructor(readonly core: StreamEndpoint) {}' in plain
  assert 'Subscription<BookUpdate> {' in plain and 'replyCodec' not in plain


def _pets_with_extras(tmp_path: Path, extras: str) -> Path:
  """A copy of the `pets` fixture with `[typescript.extras]` entries appended."""
  root = tmp_path / 'pets'
  shutil.copytree(DUAL_FIXTURE, root, ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  toml = root / 'truewire.toml'
  toml.write_text(toml.read_text() + '\n' + extras)
  return root


def test_extras_replace_a_generated_child_or_add_methods(tmp_path: Path):
  """An extra that `replaces` a generated child is built in its place (the generated methods
  keep delegating to it); an added one is built from the router's core; every listed method
  becomes a property typed by the class's own method."""
  if not (DUAL_FIXTURE / 'truewire.toml').is_file():
    pytest.skip('packages/testing-ts is not checked out beside the package')
  root = _pets_with_extras(tmp_path, (
    '[[typescript.extras."pets"]]\nfile = "get_pet_cached"\nclass = "GetPetCached"\nreplaces = "get_pet"\nmethods = ["getPetCached"]\n\n'
    '[[typescript.extras."pets"]]\nfile = "adopt"\nclass = "Adopt"\nmethods = ["adopt", "release"]\n'
  ))
  project = load_project(root)
  router = render_package(build_plan(project), project).files['pets/index.ts']
  assert "import { GetPetCached } from './get_pet_cached.js'" in router
  assert "import { Adopt } from './adopt.js'" in router
  assert 'private readonly getPet_: GetPetCached' in router
  assert 'this.getPet_ = new GetPetCached(core)' in router
  assert 'return this.getPet_.getPet(request, options)' in router
  assert 'private readonly adopt_: Adopt' in router
  assert 'this.adopt_ = new Adopt(core)' in router
  assert "readonly release: Adopt['release']" in router
  assert 'this.release = this.adopt_.release.bind(this.adopt_)' in router
  assert "readonly getPetCached: GetPetCached['getPetCached']" in router


@pytest.mark.parametrize(('extras', 'message'), [
  ('[[typescript.extras."pets"]]\nfile = "x"\nclass = "X"\nmethods = ["getPet"]\n', 'collides'),
  ('[[typescript.extras."pets"]]\nfile = "x"\nclass = "X"\nreplaces = "nope"\n', 'names no generated child'),
  ('[[typescript.extras."nowhere"]]\nfile = "x"\nclass = "X"\n', 'names no router node'),
])
def test_extras_refuse_what_would_not_build(tmp_path: Path, extras: str, message: str):
  """A method name a generated member already has, a `replaces` naming no generated child,
  and a node that is no router are refused before anything is written."""
  if not (DUAL_FIXTURE / 'truewire.toml').is_file():
    pytest.skip('packages/testing-ts is not checked out beside the package')
  project = load_project(_pets_with_extras(tmp_path, extras))
  with pytest.raises(ValueError, match=message):
    render_package(build_plan(project), project)
  result = CliRunner().invoke(app, ['generate', 'typescript', '--project', str(project.root)])
  assert result.exit_code != 0
  assert message in result.output


def test_a_walker_over_tuple_rows_parenthesises_the_row_array(tmp_path: Path):
  """A readonly tuple row binds wrongly unparenthesised in `X[]`: `readonly [a, b][]` is a
  readonly array of mutable tuples, which `tsc` rejects against the walker's own rows.
  deribit's `get_volatility_index_data` (a `token` walk over candle tuples) is the case."""
  root = tmp_path / 'venue'
  endpoint_dir = root / 'spec' / 'endpoints' / 'market' / 'candles'
  endpoint_dir.mkdir(parents=True)
  (root / 'truewire.toml').write_text('[project]\nname = "venue"\n\n[typescript]\npackage = "venue"\nsrc = "src"\nname = "Venue"\n')
  (root / 'spec' / 'endpoints' / 'market' / 'router.json').write_text(json.dumps({
    'description': 'Market data.', 'upstream': 'https://venue.example/market', 'core': 'default',
  }))
  (endpoint_dir / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/candles', 'method': 'GET', 'description': 'Candles.',
      'request': {
        'title': 'CandlesRequest', 'type': 'object', 'description': 'Query.',
        'properties': {'cursor': {'type': 'string', 'description': 'Continuation cursor.'}},
      },
      'response': {
        'title': 'CandlesPage', 'type': 'object', 'description': 'A page.', 'required': ['data'],
        'properties': {
          'data': {'type': 'array', 'description': 'Candles.', 'items': {
            'type': 'array', 'description': 'Time and close.', 'minItems': 2, 'maxItems': 2,
            'prefixItems': [{'type': 'number', 'description': 'Time.'}, {'type': 'number', 'description': 'Close.'}],
          }},
          'continuation': {'anyOf': [{'type': 'string', 'description': 'Next cursor.'}, {'type': 'null'}], 'description': 'Next cursor.'},
        },
      },
    },
    'pagination': {
      'strategy': 'token', 'cursor': {'parameter': 'cursor', 'from': 'continuation'},
      'done': {'kind': 'absent_cursor', 'rows': 'data'},
    },
  }))
  project = load_project(root)
  candles = render_package(build_plan(project), project).files['market/candles.ts']
  assert 'Promise<[(readonly [number, number])[], string | null]>' in candles
  assert 'readonly [number, number][]' not in candles


def test_proto_sources_render_as_proto_ts(tmp_path: Path):
  """A project with `spec/proto/*.proto` gets `proto.ts` exporting them verbatim (ADR 0016);
  one without gets no such file."""
  root = _with_typescript(FIXTURE_ROOT, tmp_path / 'client')
  assert 'proto.ts' not in render_package(build_plan(root), load_project(root)).files
  (root / 'spec' / 'proto' / 'nested').mkdir(parents=True)
  (root / 'spec' / 'proto' / 'push.proto').write_text('syntax = "proto3";\nmessage Push { string channel = 1; }\n')
  (root / 'spec' / 'proto' / 'nested' / 'body.proto').write_text('syntax = "proto3";\nmessage Body { string "quoted" = 1; }\n')
  source = render_package(build_plan(root), load_project(root)).files['proto.ts']
  assert source.startswith(BANNER)
  assert 'export const PROTO_SOURCES: Readonly<Record<string, string>> = {' in source
  assert source.index('"nested/body.proto"') < source.index('"push.proto"')
  assert '"push.proto": "syntax = \\"proto3\\";\\nmessage Push { string channel = 1; }\\n",' in source


def _rpc(root: Path, path: str, spec: dict, pagination: dict | None = None):
  """Write one `rpc` endpoint (and its grouping's `router.json`) under `root/spec/endpoints`."""
  *groups, _ = path.split('/')
  group_dir = root / 'spec' / 'endpoints' / Path(*groups)
  (group_dir / path.split('/')[-1]).mkdir(parents=True, exist_ok=True)
  if not (group_dir / 'router.json').is_file():
    (group_dir / 'router.json').write_text(json.dumps({
      'description': 'A grouping.', 'upstream': 'https://venue.example/docs', 'core': 'default',
    }))
  document: dict = {'meta': {}, 'spec': {'kind': 'rpc', 'transports': ['http'], 'method': 'GET', **spec}}
  if pagination is not None:
    document['pagination'] = pagination
  (group_dir / path.split('/')[-1] / 'endpoint.json').write_text(json.dumps(document))


def _page(title: str, rows: dict, **extra: dict) -> dict:
  return {
    'title': title, 'type': 'object', 'description': 'A page.', 'required': ['list'],
    'properties': {'list': {'type': 'array', 'description': 'Rows.', 'items': rows}, **extra},
  }


_ROW = {'title': 'Row', 'type': 'object', 'description': 'One row.', 'required': ['id'], 'properties': {'id': {'type': 'string', 'description': 'Id.'}}}


@pytest.fixture(name='venue')
def fixture_venue(tmp_path: Path) -> dict[str, str]:
  """One project holding a shape each that the migrated client specs failed `tsc` on."""
  root = tmp_path / 'venue'
  (root / 'spec').mkdir(parents=True)
  (root / 'truewire.toml').write_text('[project]\nname = "venue"\n\n[typescript]\npackage = "venue"\nsrc = "src"\nname = "Venue"\n')
  (root / 'spec' / 'schemas.json').write_text(json.dumps({
    'Currency': {'title': 'Currency', 'type': 'object', 'description': 'A currency.', 'required': ['code'], 'properties': {'code': {'type': 'string', 'description': 'Code.'}}},
  }))
  _rpc(root, 'market/index', {'path': '/index', 'description': 'Index price.', 'response': {
    'title': 'IndexPrice', 'type': 'object', 'description': 'Index.', 'required': ['price'], 'properties': {'price': {'type': 'string', 'description': 'Price.'}},
  }})
  _rpc(root, 'market/currency', {'path': '/currency', 'description': 'One currency.', 'response': {'$ref': 'Currency'}})
  _rpc(root, 'stats/price', {
    'path': '/api', 'description': 'Price.',
    'request': {'title': 'PriceRequest', 'type': 'object', 'description': 'Query.', 'required': ['module'], 'properties': {
      'module': {'type': 'string', 'enum': ['stats'], 'default': 'stats', 'description': 'Fixed selector.'},
      'chain': {'type': 'string', 'description': 'Chain.'},
    }},
    'response': {'title': 'Price', 'type': 'object', 'description': 'Price.', 'properties': {'usd': {'type': 'string', 'description': 'USD.'}}},
  })
  cursor_request = {'title': 'ListRequest', 'type': 'object', 'description': 'Query.', 'properties': {
    'cursor': {'type': 'string', 'description': 'Cursor.'},
  }}
  _rpc(root, 'accounts/list', {
    'path': '/accounts', 'description': 'Accounts.', 'request': cursor_request,
    'response': _page('AccountsPage', _ROW, cursor={'type': 'string', 'description': 'Next cursor.'}),
  }, {'strategy': 'token', 'cursor': {'parameter': 'cursor', 'from': 'cursor'}, 'done': {'kind': 'absent_cursor'}})
  _rpc(root, 'market/instruments', {
    'path': '/instruments', 'description': 'Instruments.', 'request': cursor_request,
    'response': {'description': 'By category.', 'anyOf': [
      _page('SpotInfo', _ROW),
      _page('FuturesInfo', {**_ROW, 'title': 'FuturesRow'}, nextPageCursor={'type': 'string', 'description': 'Next cursor.'}),
    ]},
  }, {'strategy': 'token', 'cursor': {'parameter': 'cursor', 'from': 'nextPageCursor'}, 'done': {'kind': 'absent_cursor', 'rows': 'list'}})
  _rpc(root, 'market/trades', {
    'path': '/trades', 'description': 'Trades.',
    'request': {'title': 'TradesRequest', 'type': 'object', 'description': 'Query.', 'properties': {
      'page': {'type': 'integer', 'description': 'Page.'},
      'limit': {'type': 'integer', 'maximum': 100, 'description': 'Rows per page, at most 100.'},
    }},
    'response': _page('TradesPage', _ROW),
  }, {'strategy': 'page', 'index': {'parameter': 'page'}, 'size': {'parameter': 'limit'}, 'done': {'kind': 'short_page', 'rows': 'list'}})
  summary_request = {'title': 'SummaryRequest', 'type': 'object', 'description': 'Query.', 'required': ['kind'], 'properties': {
    'kind': {'type': 'integer', 'enum': [1, 2], 'description': 'Which shape.'},
    'page': {'type': 'integer', 'description': 'Page.'},
    'size': {'type': 'integer', 'default': 10, 'description': 'Rows per page.'},
  }}
  _rpc(root, 'broker/summary', {
    'path': '/summary', 'description': 'Summaries.', 'request': summary_request,
    'response': {'description': 'By kind.', 'anyOf': [
      _page('SummaryUsdm', {**_ROW, 'title': 'UsdmRow'}), _page('SummaryCoinm', {**_ROW, 'title': 'CoinmRow'}),
    ]},
  }, {'strategy': 'page', 'index': {'parameter': 'page', 'start': 1}, 'size': {'parameter': 'size'}, 'done': {'kind': 'short_page', 'rows': 'list'}})
  project = load_project(root)
  plan = build_plan(project)
  rendered = render_package(plan, project)
  assert rendered.skipped == []
  return rendered.files


def test_a_leaf_named_index_is_not_its_groupings_router_module(venue):
  """`market.index` would otherwise be written over `market/index.ts`, the router (binance
  options' `market.index`)."""
  assert 'export class Index {' in venue['market/index_.ts']
  assert "import * as index from './index_.js'" in venue['market/index.ts']


def test_a_class_grows_past_a_shared_type_the_response_is_a_bare_reference_to(venue):
  """A response that is only `$ref: Currency` imports `Currency` under its own name, so the
  endpoint class is `CurrencyEndpoint` (kucoin's `spot.currency`, moralis, bitget)."""
  currency = venue['market/currency.ts']
  assert 'export class CurrencyEndpoint {' in currency
  assert 'Promise<Currency>' in currency


def test_a_fixed_value_on_an_optional_request_reads_it_optionally(venue):
  """Every other field optional makes `request` optional, so its fixed value is read with
  `?.` (etherscan's `module`/`action`: a `tsc` error, and a `TypeError` on a bare call)."""
  assert "const wire: Request = { ...request, module: request?.module ?? 'stats' }" in venue['stats/price.ts']


def test_a_token_generator_walker_annotates_the_response(venue):
  """The cursor read back into the next call is a circular inference unless `response` is
  typed (TS7022, first seen on bitget's `empty`-terminated token walks, which are now
  resumable walkers; a token walk with no declared `rows` still renders the generator)."""
  assert 'const response: AccountsPage = await this.list({ ...request, cursor }, options)' in venue['accounts/list.ts']


def test_a_token_walk_over_a_union_payload_joins_every_variants_rows(venue):
  """A variant may lack the cursor, and a row type walked through the first variant is wrong
  for the rest (bybit's `market.instruments`): the rows are the union of every variant's,
  and both paths are read through a structural view of the page, as Python walks it."""
  instruments = venue['market/instruments.ts']
  assert 'union payload' not in instruments
  assert 'instrumentsPaged(request?: InstrumentsPagedRequest, options?: CallOptions): PaginatedResponse<Row | FuturesRow, string>' in instruments
  assert 'const page = response as { list?: (Row | FuturesRow)[] | null; nextPageCursor?: string | null }' in instruments
  assert 'const rows = page.list ?? []' in instruments
  assert 'const following = page.nextPageCursor ?? null' in instruments


def test_a_page_walk_over_a_union_payload_joins_every_variants_rows(venue):
  """binance's broker `sub_account_futures_summary_v2` answers one of two shapes by
  `futuresType`: a page walk reads the rows through a structural view of the page, named
  apart from its own `page` state."""
  summary = venue['broker/summary.ts']
  assert 'union payload' not in summary
  assert 'PaginatedResponse<UsdmRow | CoinmRow, number>' in summary
  assert 'const view = response as { list?: (UsdmRow | CoinmRow)[] | null }' in summary
  assert 'const rows = view.list ?? []' in summary
  assert 'return [rows, page + 1]' in summary

def test_a_page_size_is_clamped_to_its_schema_maximum(venue):
  """A caller asking for 500 rows of a 100-row endpoint gets full pages of 100, which must
  not read as the short last page; the raw size is what narrows against `undefined`."""
  assert 'rows.length === 0 || (request.limit !== undefined && rows.length < Math.min(request.limit, 100))' in venue['market/trades.ts']


def test_a_seek_walk_sends_the_size_it_clamps_at_least_2():
  """The walk clamps a given size once, before `seek`: at most the schema `maximum`, at least
  2 (a page of 1 cannot advance past an inclusive moving bound), and that value is both the
  cap and what every request sends. An omitted size stays unset, the cap falling back to the
  declared `cap` or the schema default."""
  from types import SimpleNamespace

  from truewire.codegen.typescript.endpoint import _seek_size

  def size(*, required: bool, default: int | None, maximum: int | None, fixed_cap: int | None = None, int64: bool = False) -> tuple[str | None, str]:
    field_type = {'type': 'scalar', 'base': 'integer', **({'format': 'int64'} if int64 else {})}
    endpoint = SimpleNamespace(request=SimpleNamespace(fields=[SimpleNamespace(wire='limit', required=required, type=field_type)]))
    pagination = SimpleNamespace(size='limit', size_default=default, size_maximum=maximum)
    return _seek_size(endpoint, pagination, {'cap': fixed_cap})  # type: ignore[arg-type]

  assert size(required=True, default=None, maximum=1000) == ('const size = Math.min(Math.max(request.limit, 2), 1000)', 'size')
  assert size(required=False, default=100, maximum=1000) == ('const size = request.limit == null ? undefined : Math.min(Math.max(request.limit, 2), 1000)', 'size ?? 100')
  assert size(required=False, default=None, maximum=1000) == ('const size = request.limit == null ? undefined : Math.min(Math.max(request.limit, 2), 1000)', 'size')
  assert size(required=False, default=None, maximum=None, fixed_cap=500) == ('const size = request.limit == null ? undefined : Math.max(request.limit, 2)', 'size ?? 500')
  assert size(required=True, default=None, maximum=500, int64=True) == ('const size = Math.min(Math.max(Number(request.limit), 2), 500)', 'size')
  nothing = SimpleNamespace(request=SimpleNamespace(fields=[]))
  assert _seek_size(nothing, SimpleNamespace(size=None, size_default=None, size_maximum=None), {'cap': 300}) == (None, '300')  # type: ignore[arg-type]
  # A size sent as a string (a `string`, or an `integer-string` rendered `bigint`) is left
  # alone: no clamp, nothing resent, and the cap is the fallback.
  for fmt in ({}, {'format': 'integer-string'}):
    endpoint = SimpleNamespace(request=SimpleNamespace(fields=[SimpleNamespace(wire='limit', required=True, type={'type': 'scalar', 'base': 'string', **fmt})]))
    assert _seek_size(endpoint, SimpleNamespace(size='limit', size_default=100, size_maximum=1000), {}) == (None, '100')  # type: ignore[arg-type]


def test_a_seek_walks_hover_doc_says_where_the_runtime_moves_the_bound(tmp_path: Path):
  """The shared sentence over the Rust walker fixture's seek walks: a tuple row's key is an
  element of each row (TRU-119), `field` is relative to one row, a cap-less or span walk is
  not described as moving on full pages only (TRU-117), and a string id is the last row's
  (TRU-125)."""
  root = tmp_path / 'walkers'
  shutil.copytree(Path(__file__).parent / 'fixtures' / 'rust_walkers', root)
  with (root / 'truewire.toml').open('a') as toml:
    toml.write('\n[typescript]\npackage = "walkers"\nsrc = "ts"\nname = "Walkers"\n')
  project = load_project(root)
  files = render_package(build_plan(project), project).files
  candles = files['market/candles.ts']
  assert "      field: '[0]',\n" in candles
  assert (
    'walks forwards by moving `start` to the latest first element among the rows of each page '
    "that came back full, never past the caller's own `end`;"
  ) in candles
  assert (
    'walks backwards by moving `idLessThan` to the `id` of the last row of each page that came '
    'back full, or of every page while `limit` is unset;'
  ) in files['market/ledger.ts']
  assert (
    'walks backwards by moving `end` to the earliest `time` of each page that came back full, and '
    "to the edge of the range it requested after a short one, never past the caller's own `start`;"
  ) in files['market/candles_chunked.ts']
  walks = [line for source in files.values() for line in source.splitlines() if 'Paged variant' in line]
  assert walks and not [line for line in walks if re.search(r'\[-?\d+\]|position|ADR \d|extreme|``', line)]


def test_a_seek_walk_with_a_span_sends_the_clamped_size_and_leaves_a_string_size_alone(tmp_path: Path):
  """A `span` destructures the request into `base`; the clamped size still goes out on every
  page. An optional `int64` size is counted through `Number`, and a size under a maximum
  below 2 is that maximum. A string size renders no clamp and no rule sentence."""
  root = tmp_path / 'venue'
  (root / 'spec').mkdir(parents=True)
  (root / 'truewire.toml').write_text('[project]\nname = "venue"\n\n[typescript]\npackage = "venue"\nsrc = "src"\nname = "Venue"\n')
  candle = {'title': 'Candle', 'type': 'object', 'description': 'One candle.', 'required': ['time'], 'properties': {'time': {'type': 'integer', 'format': 'epoch-seconds', 'description': 'Open time.'}}}
  pagination = {
    'strategy': 'seek', 'cursor': {'field': '[-1].time', 'unique': True}, 'bound': {'start': 'start', 'end': 'end'},
    'anchor': 'end', 'rows': 'candles', 'span': {'parameter': 'span', 'default': 3600, 'unit': 's'}, 'size': {'parameter': 'limit'},
  }
  for name, limit in {
    'int64': {'type': 'integer', 'format': 'int64', 'maximum': 1000},
    'tiny': {'type': 'integer', 'maximum': 1},
    'text': {'type': 'string', 'maximum': 1000},
  }.items():
    title = name.capitalize()
    _rpc(root, f'market/{name}', {
      'path': f'/candles/{name}', 'description': 'Candles.',
      'request': {'title': f'{title}Request', 'type': 'object', 'description': 'Query.', 'required': ['start', 'end'], 'properties': {
        'start': {'type': 'integer', 'format': 'epoch-seconds', 'description': 'From.'},
        'end': {'type': 'integer', 'format': 'epoch-seconds', 'description': 'To.'},
        'limit': {**limit, 'description': 'Rows.'},
      }},
      'response': {'title': f'{title}Page', 'type': 'object', 'description': 'A page.', 'required': ['candles'], 'properties': {
        'candles': {'type': 'array', 'description': 'Rows.', 'items': {**candle, 'title': f'{title}Candle'}},
      }},
    }, pagination)
  project = load_project(root)
  rendered = render_package(build_plan(project), project)
  assert rendered.skipped == []
  int64 = rendered.files['market/int64.ts']
  assert '    const size = request.limit == null ? undefined : Math.min(Math.max(Number(request.limit), 2), 1000)\n' in int64
  assert 'const { span: span = 3600, ...base } = request' in int64
  assert 'const response = await this.int64({ ...base, limit: size, end: pos!, start: edge! }, options)' in int64
  assert 'The walk requests pages of at least 2 rows and at most 1000' in int64
  tiny = rendered.files['market/tiny.ts']
  assert 'Math.min(Math.max(request.limit, 2), 1)' in tiny
  assert 'The walk requests pages of 1 row.' in tiny and 'at least 2' not in tiny
  text = rendered.files['market/text.ts']
  assert 'Math.max' not in text and 'const size' not in text and 'limit: size' not in text
  assert 'The walk requests pages' not in text
  assert '      cap: undefined,\n' in text
  assert 'const response = await this.text({ ...base, end: pos!, start: edge! }, options)' in text


def _hand_written_streams_project(root: Path) -> Path:
  """A composite root over `streams`, whose `market` router holds only a hand-written stream
  (mexc's protobuf-framed spot leaves) beside a generated `rest` endpoint."""
  root.mkdir(parents=True)
  (root / 'truewire.toml').write_text(
    '[project]\nname = "venue"\n\n'
    '[cores.socket]\nmeta = { type = "object", properties = { proto_field = { type = "string" } }, required = ["proto_field"], additionalProperties = false }\n\n'
    '[typescript]\npackage = "venue"\nsrc = "src"\nname = "Venue"\n\n'
    '[go]\npackage = "venue"\nsrc = "go"\nname = "Venue"\nmodule = "example.com/fixture_client"\nroot = "go/venue"\n\n'
    '[python.cores.root]\nbase = "venue.core:Root"\nchildren = { streams = "stream_client", rest = "rest_client" }\n\n'
    '[[typescript.extras."streams.market"]]\nfile = "trades"\nclass = "Trades"\nmethods = ["trades"]\n'
  )
  endpoints = root / 'spec' / 'endpoints'
  (endpoints / 'streams' / 'market' / 'trades').mkdir(parents=True)
  (endpoints / 'rest' / 'time').mkdir(parents=True)
  (endpoints / 'router.json').write_text(json.dumps({'description': 'Venue.', 'upstream': 'https://venue.example', 'core': 'root'}))
  (endpoints / 'streams' / 'router.json').write_text(json.dumps({'description': 'Streams.', 'upstream': 'https://venue.example/ws', 'core': 'socket'}))
  (endpoints / 'rest' / 'router.json').write_text(json.dumps({'description': 'REST.', 'upstream': 'https://venue.example/rest', 'core': 'default'}))
  (endpoints / 'streams' / 'market' / 'trades' / 'endpoint.json').write_text(json.dumps({
    'meta': {'proto_field': 'public_deals'},
    'surface': {'kind': 'handwritten', 'symbol': 'streams.market.trades:trades', 'reason': 'Protobuf frames.'},
    'spec': {
      'kind': 'stream', 'channel': 'deals@{symbol}', 'description': 'Trades.',
      'parameters': {'title': 'TradesParams', 'type': 'object', 'description': 'Parameters.', 'required': ['symbol'], 'properties': {'symbol': {'type': 'string', 'description': 'Market.'}}},
      'payload': {'title': 'TradesPush', 'type': 'object', 'description': 'One push.', 'properties': {'price': {'type': 'string', 'description': 'Price.'}}},
    },
  }))
  (endpoints / 'rest' / 'time' / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/time', 'method': 'GET', 'description': 'Server time.',
      'response': {'title': 'ServerTime', 'type': 'object', 'description': 'Time.', 'properties': {'serverTime': {'type': 'integer', 'description': 'Millis.'}}},
    },
  }))
  return root


def test_a_router_of_only_hand_written_endpoints_holds_their_contract(tmp_path: Path):
  """A router whose endpoints are all hand-written still holds the transport a generated one
  would: its composite parent hands it the mapped field, typed by the endpoint's contract and
  meta, and the `[typescript.extras]` class is built from it (not `undefined`)."""
  project = load_project(_hand_written_streams_project(tmp_path / 'venue'))
  rendered = render_package(build_plan(project), project)
  root = rendered.files['main.ts']
  assert '  stream_client: StreamEndpoint<SocketMeta>\n' in root
  assert 'this.streams = new Streams(core.stream_client)' in root
  market = rendered.files['streams/market/index.ts']
  assert 'constructor(readonly core: StreamEndpoint<SocketMeta>) {' in market
  assert 'this.trades_ = new Trades(core)' in market
  assert 'streams/market/trades.ts' not in rendered.files


def test_a_page_walk_over_int64_index_and_size_counts_in_numbers(tmp_path: Path):
  """binance's page walks declare `format: int64` on the page index and size, which render
  `number | bigint`. The walker's state is a page count and its size a row count, so the
  index walks as a `number` (which the request field accepts) and the size is read through
  `Number(...)`; `page + 1` and `Math.min(size, max)` would not type-check on a bigint."""
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns('.truewire', 'core_impl'))
  spec = root / 'spec' / 'endpoints' / 'market' / 'order_page_total' / 'endpoint.json'
  raw = json.loads(spec.read_text())
  properties = raw['spec']['request']['properties']
  properties['current']['format'] = 'int64'
  properties['size']['format'] = 'int64'
  properties['size']['maximum'] = 100
  spec.write_text(json.dumps(raw))
  walker = render_package(build_plan(root)).files['market/order_page_total.ts']
  assert re.search(r'current\??: number \| bigint', walker)
  assert re.search(r'orderPageTotalPaged\([^)]*\): PaginatedResponse<\w+, number>', walker)
  assert 'const next = async (current: number): Promise<' in walker
  assert 'Number(request.size)' in walker
  assert 'Math.min(request.size, 100)' not in walker


def test_an_int64_seek_bound_compares_its_keys_as_bigints():
  """A row id beyond 2^53 (kucoin's hf_ledgers `lastId`) must not be read through `Number`,
  which rounds it and leaves a value `t.int64` refuses to dump."""
  from truewire.codegen.typescript.endpoint import _seek_keys
  assert _seek_keys(None, {'type': 'scalar', 'base': 'integer', 'format': 'int64'}) == ("'bigint'", False)  # type: ignore[arg-type]
  assert _seek_keys(None, {'type': 'scalar', 'base': 'integer'}) == ("'number'", False)  # type: ignore[arg-type]


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_offset_page_total_counts_a_partial_last_page():
  """Execute the generated walker: TS already counts the current partial page."""
  import subprocess

  supported = subprocess.run(
    ['node', '--input-type=module', '-e', "import { stripTypeScriptTypes } from 'node:module'"],
    capture_output=True, text=True,
  )
  if supported.returncode != 0:
    pytest.skip('node does not support stripTypeScriptTypes')

  root = Path(__file__).parent / 'fixtures' / 'rust_walkers'
  rendered = render_package(build_plan(root))
  source = rendered.files['market/withdrawal_pages.ts']
  start = source.index('  async *withdrawalPagesPaged(')
  end = source.index('\n  }\n', start) + len('\n  }\n')
  # Like the Python walker tests, give the generated method a canned single-call method.
  method = source[start:end]
  script = """
import assert from 'node:assert/strict'
import { stripTypeScriptTypes } from 'node:module'
const LogicError = Error
const Walk = eval(stripTypeScriptTypes(`(class {
  calls = []
  constructor(count) { this.count = count }
  async withdrawalPages(request) {
    this.calls.push(request.from)
    return {
      pages: Math.ceil(this.count / request.limit),
      items: Array.from({ length: Math.max(0, Math.min(request.limit, this.count - request.from)) },
                       (_, i) => ({ id: String(request.from + i) })),
    }
  }
${METHOD}
})`))
for (const count of [0, 2, 8, 10]) {
  const walk = new Walk(count)
  const rows = []
  for await (const page of walk.withdrawalPagesPaged({ limit: 4 })) rows.push(...page.items)
  assert.deepEqual(rows.map(row => row.id), Array.from({ length: count }, (_, i) => String(i)))
  assert.deepEqual(walk.calls, Array.from({ length: Math.max(1, Math.ceil(count / 4)) }, (_, i) => i * 4))
}
""".replace('${METHOD}', '${' + json.dumps(method) + '}')
  result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True)
  assert result.returncode == 0, result.stdout + result.stderr
