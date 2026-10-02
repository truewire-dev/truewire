"""One gRPC endpoint module (ADR 0017): aliases for the protobuf-es messages the call sends
and returns, the stub's method descriptor, and a class holding a `GrpcEndpoint<Meta>` core,
with the method and, when the plan resolved a pagination, its `Paged` walker.

The messages come from the stubs `truewire protos typescript` builds from `spec/proto/`
under `<package>/protos/` (one `<file>_pb.ts` per `.proto`), named the way protoc-gen-es
names them: nested messages joined by `_`, the schema `<Name>Schema`, fields in
`protoCamelCase`. The method takes a `Request` (`MessageInitShape` of the input: a partial
object or a full message) and returns the decoded `Response`; there is no `validate`
overload, since a protobuf reply is always decoded by its schema.
"""
from truewire.plan.model import EndpointPlan, PackagePlan, ProtoFieldPlan, ProtoTypePlan

from .endpoint import (
  META_FILE, EndpointModule, Method, Param, emit_refusal, emit_signatures, endpoint_file, meta_type_name, method_docs,
)
from .names import camel_case, literal
from .printer import BANNER, relative_specifier
from .types import Module

PROTOS_DIR = 'protos'
PAGED_SUFFIX = 'Paged'
PROTOBUF = '@bufbuild/protobuf'
WKT = '@bufbuild/protobuf/wkt'

TS_SCALARS = {
  'double': 'number', 'float': 'number', 'int32': 'number', 'uint32': 'number', 'sint32': 'number',
  'fixed32': 'number', 'sfixed32': 'number', 'int64': 'bigint', 'uint64': 'bigint', 'sint64': 'bigint',
  'fixed64': 'bigint', 'sfixed64': 'bigint', 'bool': 'boolean', 'string': 'string', 'bytes': 'Uint8Array',
}
"""protobuf-es v2's default JavaScript type of each proto scalar."""

_RESERVED_PROPERTIES = frozenset(('constructor', 'toString', 'toJSON', 'valueOf'))


def proto_camel_case(name: str) -> str:
  """protobuf-es's field `localName`: `next_key` -> `nextKey`, a reserved property `$`-suffixed."""
  out: list[str] = []
  upper = False
  for char in name:
    if char == '_':
      upper = True
    elif char.isdigit():
      out.append(char)
      upper = False
    else:
      out.append(char.upper() if upper else char)
      upper = False
  local = ''.join(out)
  return f'{local}$' if local in _RESERVED_PROPERTIES else local


def rpc_local_name(rpc: str) -> str:
  local = rpc[:1].lower() + rpc[1:]
  return f'{local}$' if local in _RESERVED_PROPERTIES else local


def _stub_name(t: ProtoTypePlan) -> str:
  if t.package and t.name.startswith(t.package + '.'):
    return t.name[len(t.package) + 1:].replace('.', '_')
  return t.name.rsplit('.', 1)[-1]


class _Skipped(Exception):
  pass


def _import(module: Module, t: ProtoTypePlan, *, schema: bool = False, type_only: bool = True) -> str:
  """The stub identifier of a message or enum (its `Schema` when `schema`), imported."""
  if t.file is None:
    raise _Skipped(f'`{t.name}` is declared by no file under spec/proto/')
  name = _stub_name(t) + ('Schema' if schema else '')
  if t.file.startswith('google/protobuf/'):
    specifier = WKT
  else:
    specifier = relative_specifier(module.file, f'{PROTOS_DIR}/{t.file.removesuffix(".proto")}_pb.ts')
  module.imports.add(specifier, name, type_only=type_only)
  return name


def _read(subject: str, hops: list[ProtoFieldPlan]) -> str:
  """`subject.a?.b`: the first hop is always there, a later one only when its message was set."""
  return subject + ''.join(
    f'{"." if index == 0 else "?."}{proto_camel_case(hop.name)}' for index, hop in enumerate(hops)
  )


def render_grpc(plan: PackagePlan, endpoint: EndpointPlan, *, class_name: str) -> EndpointModule:
  """Render one unary gRPC endpoint's module; `ValueError` for a shape with no rendering."""
  grpc = endpoint.grpc
  if grpc is None or grpc.streaming != 'unary':
    raise ValueError(f'{endpoint.function}: only unary gRPC methods have a TypeScript rendering')
  file = endpoint_file(endpoint.path)
  module = Module(plan, file, local={}, visible=[])
  module.names.add(class_name)
  w = module.writer
  try:
    request_schema = _import(module, grpc.request, schema=True)
    response_type = _import(module, grpc.response)
  except _Skipped as skipped:
    raise ValueError(f'{endpoint.function}: {skipped}') from None
  service = _stub_name(ProtoTypePlan(kind='message', name=grpc.service, file=grpc.service_file, package=grpc.service.rsplit('.', 1)[0]))
  service_specifier = relative_specifier(file, f'{PROTOS_DIR}/{grpc.service_file.removesuffix(".proto")}_pb.ts')
  module.imports.add(service_specifier, service)
  module.imports.add(PROTOBUF, 'MessageInitShape', type_only=True)
  module.core('GrpcEndpoint', type_only=True)
  module.core('GrpcCallOptions', type_only=True)
  core_plan = plan.cores.get(endpoint.core)
  meta_type = meta_type_name(endpoint.core) if core_plan is not None and core_plan.meta is not None else None
  if meta_type is not None:
    module.imports.add(relative_specifier(file, META_FILE), meta_type, type_only=True)
  core_param = f'GrpcEndpoint<{meta_type}>' if meta_type is not None else 'GrpcEndpoint'

  names = {'Request': 'Request', 'Response': 'Response', 'Row': 'Row', 'Cursor': 'Cursor', 'method': 'method'}
  for key, name in names.items():
    while name in module.names:
      name += '_'
    names[key] = name
    module.declare(name)
  w.jsdoc(f'The `{grpc.request.name}` message the call sends: a partial object or a full message.')
  w.line(f'export type {names["Request"]} = MessageInitShape<typeof {request_schema}>')
  w.blank()
  w.jsdoc(f'The `{grpc.response.name}` message the call returns.')
  w.line(f'export type {names["Response"]} = {response_type}')
  w.blank()
  w.jsdoc(f'The stub\'s descriptor of `{grpc.service}.{grpc.rpc}`.')
  w.line(f'export const {names["method"]} = {service}.method.{rpc_local_name(grpc.rpc)}')
  w.blank()

  method_name = camel_case(endpoint.path[-1])
  doc, tags = method_docs(endpoint)
  main = Method(
    method_name,
    [Param('request', ('local', names['Request']), optional=True), Param('options', ('core', 'GrpcCallOptions'), optional=True)],
    ('promise', (('local', names['Response']),)), doc=doc, tags=tags, raw=False,
  )
  paged = _paged_head(module, endpoint, names)
  methods = [paged[0], main] if paged is not None else [main]

  w.jsdoc(endpoint.docs.description)
  with w.block(f'export class {class_name} {{'):
    w.line(f'constructor(readonly core: {core_param}) {{}}')
    if paged is not None:
      w.blank()
      emit_signatures(module, paged[0])
      for line in paged[1]:
        w.line(line)
    w.blank()
    emit_signatures(module, main)
    with w.block(f'async {method_name}(request: {names["Request"]} = {{}}, options?: GrpcCallOptions): Promise<{names["Response"]}> {{'):
      emit_refusal(module, endpoint)
      meta = literal(endpoint.meta) if meta_type is not None else '{}'
      descriptor = 'method' if names['method'] == 'method' else f'method: {names["method"]}'
      w.line(f'return this.core.unary({{ {descriptor}, request, meta: {meta}, ...options }})')
  return EndpointModule(
    file=file, class_name=class_name, core_type='GrpcEndpoint', meta_type=meta_type,
    types=[names['Request'], names['Response'], *([names['Row'], names['Cursor']] if paged is not None else [])],
    methods=methods, source=module.render(BANNER),
  )


def _paged_head(module: Module, endpoint: EndpointPlan, names: dict[str, str]) -> tuple[Method, list[str]] | None:
  """The walker's method and its implementation lines, or `None` when the plan resolved no walk."""
  pagination, grpc = endpoint.pagination, endpoint.grpc
  assert grpc is not None
  paging = grpc.paging
  if pagination is None or pagination.walker != 'paginated' or paging is None or paging.rows is None:
    return None
  driver = paging.driver
  leaf = driver[-1]
  if leaf.type.kind != 'scalar' or leaf.repeated or any(hop.oneof is not None for hop in driver):
    return None
  state = TS_SCALARS[leaf.type.name]
  kind = pagination.done.get('kind')
  rows_hop = paging.rows[-1]
  row = TS_SCALARS.get(rows_hop.type.name) if rows_hop.type.kind == 'scalar' else _import(module, rows_hop.type)
  w = module.writer
  w.jsdoc(f'One row of a page `{camel_case(endpoint.path[-1])}{PAGED_SUFFIX}` walks (`{pagination.rows}`).')
  w.line(f'export type {names["Row"]} = {row}')
  w.blank()
  w.jsdoc(f'The walk\'s state: the `{pagination.driver}` of the next page.')
  w.line(f'export type {names["Cursor"]} = {state}')
  w.blank()
  request_schema = _import(module, grpc.request, schema=True, type_only=False)
  module.imports.add(PROTOBUF, 'create')
  module.imports.add(PROTOBUF, 'clone')
  module.core('PaginatedResponse')

  def plain_core(name: str):
    from .types import CORE
    module.imports.add(CORE, name)

  body: list[str] = []
  target = 'at'
  for hop in driver[:-1]:
    target = f'{target}.{proto_camel_case(hop.name)}'
    body.append(f'    {target} ??= create({_import(module, hop.type, schema=True, type_only=False)})')
  body.append(f'    {target}.{proto_camel_case(leaf.name)} = state')
  main = camel_case(endpoint.path[-1])
  method_name = f'{main}{PAGED_SUFFIX}'
  lines = [f'{method_name}(request: {names["Request"]} = {{}}, options?: GrpcCallOptions): PaginatedResponse<{names["Row"]}, {names["Cursor"]}> {{']
  one = '1n' if state == 'bigint' else '1'
  if pagination.strategy == 'token':
    if paging.cursor is None or TS_SCALARS.get(paging.cursor[-1].type.name) != state:
      return None
    absent = 'following === undefined || following.length === 0' if state in ('Uint8Array', 'string') else 'following === undefined || !following'
    if kind == 'empty':
      absent = f'rows.length === 0 || {absent}'
    seed = {'Uint8Array': 'new Uint8Array(0)', 'string': "''", 'bigint': '0n', 'boolean': 'false'}.get(state, '0')
    inner = [
      f'    const following = {_read("response", paging.cursor)}',
      f'    return [rows, {absent} ? null : following]',
    ]
  elif pagination.strategy == 'page' and state in ('bigint', 'number'):
    start = pagination.start if pagination.start is not None else 1
    seed = f'{start}n' if state == 'bigint' else str(start)
    size = f'Number({_read("at", paging.size)} ?? 0)' if paging.size is not None and paging.size[-1].type.kind == 'scalar' else '0'
    if kind == 'total':
      if paging.total is None:
        return None
      plain_core('LogicError')
      lines.insert(1, '  let totalSeen: number | null = null')
      done = (
        'pages >= total' if pagination.done.get('counts') == 'pages'
        else 'rows.length === 0 || (size > 0 ? pages * size >= total : false)'
      )
      inner = [
        f'    const totalRaw = {_read("response", paging.total)}',
        '    const total = totalRaw === undefined ? null : Number(totalRaw)',
        '    if (total === null || (totalSeen !== null && total !== totalSeen)) {',
        f'      throw new LogicError(`\\`{method_name}\\` needs a \\`total\\` on every page. The API omitted it here, or reported a value (${{totalRaw}}) that disagrees with an earlier page of this same walk (${{totalSeen}}); retry the whole walk from the start.`)',
        '    }',
        '    totalSeen = total',
        f'    const size = {size}',
        f'    const pages = Number(state){" - " + str(start - 1) if start != 1 else ""}',
        f'    return [rows, {done} ? null : state + {one}]',
      ]
    elif kind == 'short_page':
      inner = [f'    const size = {size}', f'    return [rows, rows.length === 0 || (size > 0 && rows.length < size) ? null : state + {one}]']
    elif kind == 'empty':
      inner = [f'    return [rows, rows.length === 0 ? null : state + {one}]']
    else:
      return None
  else:
    return None
  lines += [
    f'  const next = async (state: {names["Cursor"]}): Promise<[{names["Row"]}[], {names["Cursor"]} | null]> => {{',
    f'    const at = clone({request_schema}, create({request_schema}, request))',
    *body,
    f'    const response = await this.{main}(at, options)',
    f'    const rows = {_read("response", paging.rows)} ?? []',
    *inner,
    '  }',
    f'  return new PaginatedResponse<{names["Row"]}, {names["Cursor"]}>({seed}, next)',
    '}',
  ]
  doc, tags = method_docs(endpoint)
  method = Method(
    method_name,
    [Param('request', ('local', names['Request']), optional=True), Param('options', ('core', 'GrpcCallOptions'), optional=True)],
    ('paginated', (('local', names['Row']), ('local', names['Cursor']))),
    doc=[*doc, f'Paged variant of `{main}`: awaitable (flattens every page) or async-iterable (one page at a time).'],
    tags=tags, is_async=False, raw=False,
  )
  return method, lines


__all__ = ['PROTOS_DIR', 'proto_camel_case', 'render_grpc', 'rpc_local_name']
