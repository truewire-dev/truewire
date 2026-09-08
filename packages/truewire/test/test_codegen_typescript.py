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
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.typescript import render_package, root_class_name
from truewire.codegen.typescript.endpoint import (
  RAW_OPTIONS, Method, Param, emit_signatures, raw_returns, read_path,
)
from truewire.codegen.typescript.meta import meta_module, meta_type
from truewire.codegen.typescript.names import binding, camel_case, literal, property_key, string
from truewire.codegen.typescript.printer import BANNER, Writer, relative_specifier
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
    ('string', 'integer-string'): ('number', 't.integerString'),
    ('string', 'boolean-string'): ('boolean', 't.booleanString'),
    ('integer', 'epoch-millis'): ('TimestampMillis', 't.epochMillis'),
    ('integer', 'epoch-seconds'): ('TimestampSeconds', 't.epochSeconds'),
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
  assert any('token.nfts.list' in note for note in fixture_rendered.skipped)
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
  manifest = json.loads((root / '.truewire' / 'typescript-files.json').read_text()) if (root / '.truewire' / 'typescript-files.json').is_file() else None
  for path, content in rendered.files.items():
    assert (package / path).read_text() == content, path
  if manifest is not None:
    assert sorted(manifest['files']) == sorted(rendered.files)
  assert rendered.skipped == []
  assert 'listCommitsPaged(request: ListCommitsPagedRequest, options?: CallOptions): PaginatedResponse<Commit, number>' in rendered.files['repos/list_commits.ts']


# -- the command ----------------------------------------------------------------------


def test_generate_typescript_writes_checks_and_deletes(tmp_path: Path):
  root = _with_typescript(FIXTURE_ROOT, tmp_path / 'client')
  runner = CliRunner()
  result = runner.invoke(app, ['generate', 'typescript', '--project', str(root)])
  assert result.exit_code == 0, result.output
  manifest = root / '.truewire' / 'typescript-files.json'
  assert manifest.is_file()
  files = json.loads(manifest.read_text())['files']
  assert 'main.ts' in files and 'market/order_list.ts' in files
  package = root / 'ts' / 'client'
  assert (package / 'main.ts').read_text().startswith(BANNER)
  assert 'export class Client' in (package / 'main.ts').read_text()

  check = runner.invoke(app, ['generate', 'typescript', '--project', str(root), '--check'])
  assert check.exit_code == 0, check.output

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


def test_generate_typescript_needs_a_typescript_section(tmp_path: Path):
  shutil.copytree(FIXTURE_ROOT, tmp_path / 'client', ignore=shutil.ignore_patterns('node_modules', '.truewire'))
  result = CliRunner().invoke(app, ['generate', 'typescript', '--project', str(tmp_path / 'client')])
  assert result.exit_code == 1
  assert 'no [typescript] section' in result.output
