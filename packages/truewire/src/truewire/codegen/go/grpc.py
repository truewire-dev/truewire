"""One gRPC endpoint package (ADR 0017): aliases for the protobuf-go messages the call sends
and returns, and an `Endpoint` holding a `twgrpc.Endpoint` core (`truewire.dev/core/grpc`), with the method and,
when the plan resolved a pagination, its `Paged` walker.

The messages are the stubs `truewire protos go` builds from `spec/proto/` under
`<package>/protos/<proto dir>` (buf's managed mode, `go_package_prefix`), so an endpoint
package imports each proto directory it needs under an alias spelled from that directory
(`demobankv1`) and names its types the way protoc-gen-go does: nested messages joined by
`_`, fields in `GoCamelCase`, read through their nil-safe `Get` accessors. The method takes
a `*Request` (nil sends the empty message) and returns the `*Response`; there is no `Raw`
twin, since a protobuf reply has no unvalidated form.
"""
from truewire.plan.model import EndpointPlan, PackagePlan, ProtoFieldPlan, ProtoTypePlan

from .endpoint import (
  EndpointModule, Method, Tokens, _declare_struct, _header, _meta_field, _new_module, _Skipped,
  emit_method, method_docs,
)
from .meta import MetaShape
from .names import package_ident, pascal_ident, string, unique
from .types import CORE, Module, Package

PROTOS_DIR = 'protos'
PAGED_SUFFIX = 'Paged'

WELL_KNOWN_PACKAGES = {
  'google/protobuf/any.proto': 'google.golang.org/protobuf/types/known/anypb',
  'google/protobuf/duration.proto': 'google.golang.org/protobuf/types/known/durationpb',
  'google/protobuf/empty.proto': 'google.golang.org/protobuf/types/known/emptypb',
  'google/protobuf/field_mask.proto': 'google.golang.org/protobuf/types/known/fieldmaskpb',
  'google/protobuf/struct.proto': 'google.golang.org/protobuf/types/known/structpb',
  'google/protobuf/timestamp.proto': 'google.golang.org/protobuf/types/known/timestamppb',
  'google/protobuf/wrappers.proto': 'google.golang.org/protobuf/types/known/wrapperspb',
}
"""Where protobuf-go publishes each well-known file's package."""

SCALAR_GO_TYPES = {
  'double': 'float64', 'float': 'float32', 'int32': 'int32', 'int64': 'int64', 'uint32': 'uint32',
  'uint64': 'uint64', 'sint32': 'int32', 'sint64': 'int64', 'fixed32': 'uint32', 'fixed64': 'uint64',
  'sfixed32': 'int32', 'sfixed64': 'int64', 'bool': 'bool', 'string': 'string', 'bytes': '[]byte',
}

_CONFLICTING_FIELDS = frozenset((
  'Reset', 'String', 'ProtoMessage', 'Marshal', 'Unmarshal', 'ExtensionRangeArray', 'ExtensionMap', 'Descriptor',
))
"""Field names protoc-gen-go suffixes with `_`, since the message type has a method by that name."""


def go_camel_case(name: str) -> str:
  """protoc-gen-go's `GoCamelCase`: `next_key` -> `NextKey`, `Outer.Inner` -> `Outer_Inner`."""
  out: list[str] = []
  i = 0
  while i < len(name):
    c = name[i]
    following = name[i + 1] if i + 1 < len(name) else ''
    if c == '.' and following.islower() and following.isascii():
      pass
    elif c == '.':
      out.append('_')
    elif c == '_' and (i == 0 or name[i - 1] == '.'):
      out.append('X')
    elif c == '_' and following.islower() and following.isascii():
      pass
    elif c.isdigit():
      out.append(c)
    else:
      out.append(c.upper() if c.islower() and c.isascii() else c)
      while i + 1 < len(name) and name[i + 1].islower() and name[i + 1].isascii():
        i += 1
        out.append(name[i])
    i += 1
  return ''.join(out)


def go_field_name(field: ProtoFieldPlan) -> str:
  name = go_camel_case(field.name)
  return f'{name}_' if name in _CONFLICTING_FIELDS else name


class ProtoImports:
  """The proto packages one Go file imports, each under an alias spelled from its directory."""

  def __init__(self, module: Module, package: Package):
    self.module = module
    self.package = package

  def ident(self, t: ProtoTypePlan) -> str:
    """`alias.GoName` of a message or enum; `_Skipped` when its file is unknown."""
    if t.file is None:
      raise _Skipped(f'`{t.name}` is declared by no file under spec/proto/')
    well_known = WELL_KNOWN_PACKAGES.get(t.file)
    local = t.name.rsplit('.', 1)[-1] if well_known is not None else None
    if well_known is not None:
      path = well_known
      alias = well_known.rsplit('/', 1)[-1]
    else:
      directory = t.file.rsplit('/', 1)[0] if '/' in t.file else ''
      path = self.package.path(PROTOS_DIR, *directory.split('/')) if directory else self.package.path(PROTOS_DIR)
      alias = package_ident(directory.replace('/', '') or 'protos', fallback='protos')
    self.module.imports.add(path, alias)
    if local is None:
      local = go_camel_case(self._relative(t))
    return f'{alias}.{local}'

  @staticmethod
  def _relative(t: ProtoTypePlan) -> str:
    """The name inside its proto package (`SearchResponse.Hit`)."""
    if t.package and t.name.startswith(t.package + '.'):
      return t.name[len(t.package) + 1:]
    return t.name.rsplit('.', 1)[-1]


def _getters(subject: str, hops: list[ProtoFieldPlan]) -> str:
  return subject + ''.join(f'.Get{go_field_name(hop)}()' for hop in hops)


def _scalar(hop: ProtoFieldPlan) -> str | None:
  if hop.type.kind == 'scalar' and not hop.repeated and hop.map_key is None:
    return SCALAR_GO_TYPES.get(hop.type.name)
  return None


def render_grpc_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, package: Package, meta: MetaShape | None,
) -> tuple[EndpointModule, list[str]]:
  """Render one gRPC endpoint's package; the second value lists what was left out."""
  grpc = endpoint.grpc
  if grpc is None:
    raise _Skipped('a gRPC endpoint with no `grpc` plan')
  if grpc.streaming != 'unary':
    raise _Skipped(f'a `{grpc.streaming}` streaming gRPC method has no Go rendering yet')
  module, dirs, file = _new_module(plan, package, endpoint, {})
  struct, constructor = _declare_struct(module)
  protos = ProtoImports(module, package)
  request_ident = protos.ident(grpc.request)
  response_ident = protos.ident(grpc.response)
  notes: list[str] = []
  w = module.writer

  request_name = unique('Request', module.names)
  module.declare(request_name)
  response_name = unique('Response', module.names)
  module.declare(response_name)
  w.blank()
  w.doc(f'{request_name} is the `{grpc.request.name}` message the call sends.')
  w.line(f'type {request_name} = {request_ident}')
  w.blank()
  w.doc(f'{response_name} is the `{grpc.response.name}` message the call returns.')
  w.line(f'type {response_name} = {response_ident}')

  paged = _paged(module, protos, endpoint, notes, request_name=request_name)

  _header(module, struct, constructor, 'grpc.Endpoint', endpoint)
  receiver = f'e *{struct}'
  name = pascal_ident(endpoint.path[-1], fallback='Call')
  params: list[tuple[str, Tokens]] = [('request', [('text', '*'), ('local', request_name)])]
  returns: list[Tokens] = [[('text', '*'), ('local', response_name)], [('text', 'error')]]
  main = Method(name, params, returns, doc=method_docs(endpoint), deprecated=endpoint.deprecated)
  fields = [
    f'Method: {string(endpoint.wire.path or "")}', 'Request: request', 'Response: response',
  ]
  meta_field = _meta_field(module, meta, endpoint)
  if meta_field:
    fields.append(meta_field)
  fields.append(f'Options: {module.core("Options")}(opts...)')
  body = [
    'if request == nil {',
    f'\trequest = &{request_name}{{}}',
    '}',
    f'response := &{response_name}{{}}',
    f'if err := e.core.Invoke(ctx, {module.core("grpc.Call")}{{{", ".join(fields)}}}); err != nil {{',
    '\treturn nil, err',
    '}',
    'return response, nil',
  ]
  methods: list[Method] = []
  if paged is not None:
    paged_method, paged_body = paged
    paged_body = [line.replace('{METHOD}', name) for line in paged_body]
    paged_method = Method(
      f'{name}{PAGED_SUFFIX}', paged_method.params, paged_method.returns, context=False,
      doc=[*method_docs(endpoint)[:1], f'{name}{PAGED_SUFFIX} is the paged variant of {name}: All for every row, or range over Rows/Pages one page at a time.', *method_docs(endpoint)[1:]],
      deprecated=endpoint.deprecated, kind='paged',
    )
    methods.append(paged_method)
    w.blank()
    emit_method(module, paged_method, receiver=receiver, qualifier=None, body=paged_body)
  methods.append(main)
  w.blank()
  emit_method(module, main, receiver=receiver, qualifier=None, body=body)
  return EndpointModule(
    file=file, dirs=dirs, package=module.name, import_path=package.path(*dirs), struct_name=struct,
    constructor=constructor, contract='grpc.Endpoint', transport='grpc', methods=methods,
    source=module.render(doc=f'Package {module.name} is the `{endpoint.function}` gRPC endpoint.'),
  ), notes


def _paged(
  module: Module, protos: ProtoImports, endpoint: EndpointPlan, notes: list[str], *, request_name: str,
) -> tuple[Method, list[str]] | None:
  """The walker's signature and body (`{METHOD}` standing for the call it wraps), or `None`
  with a note when the declaration has no Go walker."""
  pagination, grpc = endpoint.pagination, endpoint.grpc
  if pagination is None or grpc is None:
    return None
  paging = grpc.paging
  if pagination.walker != 'paginated' or paging is None or paging.rows is None:
    notes.append(f'{endpoint.function}: its pagination resolves to no walk over the proto messages; call the method per page')
    return None
  driver = paging.driver
  leaf = driver[-1]
  state = _scalar(leaf)
  if state is None or leaf.oneof is not None or any(hop.oneof is not None for hop in driver):
    notes.append(f'{endpoint.function}: a walk driven through `{pagination.driver}` has no Go walker; call the method per page')
    return None
  kind = pagination.done.get('kind')
  rows_hop = paging.rows[-1]
  if rows_hop.type.kind == 'message':
    row_name = unique('Row', module.names)
    module.declare(row_name)
    row_ident = protos.ident(rows_hop.type)
    row_tokens: Tokens = [('text', '*'), ('local', row_name)]
    row_expr = f'*{row_name}'
  else:
    row_name = unique('Row', module.names)
    module.declare(row_name)
    row_ident = SCALAR_GO_TYPES.get(rows_hop.type.name) or protos.ident(rows_hop.type)
    row_tokens = [('local', row_name)]
    row_expr = row_name
  w = module.writer
  w.blank()
  w.doc(f'{row_name} is one row of a page `{endpoint.path[-1]}` walks (`{pagination.rows}`).')
  w.line(f'type {row_name} = {row_ident}')

  module.std('context')
  proto_import = 'google.golang.org/protobuf/proto'
  module.imports.add(proto_import, None)
  lines = [
    f'next := func(ctx context.Context, state {state}) ([]{row_expr}, *{state}, error) {{',
    f'\tat := &{request_name}{{}}',
    '\tif request != nil {',
    f'\t\tat = proto.Clone(request).(*{request_name})',
    '\t}',
  ]
  target = 'at'
  for hop in driver[:-1]:
    target = f'{target}.{go_field_name(hop)}'
    lines.extend((f'\tif {target} == nil {{', f'\t\t{target} = &{protos.ident(hop.type)}{{}}', '\t}'))
  target = f'{target}.{go_field_name(leaf)}'
  if leaf.optional:
    lines.extend(('\tvalue := state', f'\t{target} = &value'))
  else:
    lines.append(f'\t{target} = state')
  lines.extend((
    '\tresponse, err := e.{METHOD}(ctx, at, opts...)',
    '\tif err != nil {',
    '\t\treturn nil, nil, err',
    '\t}',
    f'\trows := {_getters("response", paging.rows)}',
  ))
  if pagination.strategy == 'token':
    if paging.cursor is None:
      notes.append(f'{endpoint.function}: a token walk with no cursor path has no Go walker')
      return None
    cursor_leaf = paging.cursor[-1]
    if _scalar(cursor_leaf) != state:
      notes.append(f'{endpoint.function}: the cursor `{pagination.cursor_from}` is not the driver\'s type; no Go walker')
      return None
    lines.append(f'\tfollowing := {_getters("response", paging.cursor)}')
    absent = _zero_check('following', state)
    if kind == 'empty':
      absent = f'len(rows) == 0 || {absent}'
    lines.extend((f'\tif {absent} {{', '\t\treturn rows, nil, nil', '\t}', '\treturn rows, &following, nil', '}'))
    seed = '[]byte{}' if state == '[]byte' else f'{state}({_zero_literal(state)})'
  elif pagination.strategy == 'page' and state not in ('string', '[]byte', 'bool', 'float32', 'float64'):
    start = pagination.start if pagination.start is not None else 1
    size_line = '\tsize := -1'
    size_lines = [size_line]
    if paging.size is not None and _scalar(paging.size[-1]) not in (None, 'string', '[]byte', 'bool'):
      size_lines.extend((
        f'\tif limit := {_getters("request", paging.size)}; limit > 0 {{',
        '\t\tsize = int(limit)',
        '\t}',
      ))
    if kind == 'total':
      if paging.total is None or _scalar(paging.total[-1]) in (None, 'string', '[]byte', 'bool'):
        notes.append(f'{endpoint.function}: a `total` that is not an integer has no Go walker')
        return None
      counts = 'true' if pagination.done.get('counts') == 'pages' else 'false'
      lines.extend(size_lines)
      lines.append(f'\ttotal := int64({_getters("response", paging.total)})')
      condition = f'len(rows) == 0 || {module.core("TotalReached")}({counts}, int64(state), {start}, size, len(rows), total)'
    elif kind in ('short_page', 'empty'):
      if kind == 'short_page':
        lines.extend(size_lines)
        condition = f'{module.core("Exhausted")}(len(rows), size)'
      else:
        condition = 'len(rows) == 0'
    else:
      notes.append(f'{endpoint.function}: a `page` walk ended by `{kind}` has no Go walker')
      return None
    lines.extend((
      f'\tif {condition} {{', '\t\treturn rows, nil, nil', '\t}',
      '\tfollowing := state + 1', '\treturn rows, &following, nil', '}',
    ))
    seed = f'{state}({start})'
  else:
    notes.append(f'{endpoint.function}: a `{pagination.strategy}` walk over gRPC has no Go walker')
    return None
  lines.append(f'return {module.core("NewPaginatedResponse")}({seed}, next)')
  state_tokens: Tokens = [('text', state)]
  returns: Tokens = [
    ('text', '*'), ('core', 'PaginatedResponse'), ('text', '['), *row_tokens, ('text', ', '), *state_tokens, ('text', ']'),
  ]
  signature = Method(
    PAGED_SUFFIX, [('request', [('text', '*'), ('local', request_name)])], [returns], context=False, kind='paged',
  )
  return signature, lines


def _zero_check(var: str, go_type: str) -> str:
  if go_type in ('[]byte', 'string'):
    return f'len({var}) == 0'
  if go_type == 'bool':
    return f'!{var}'
  return f'{var} == 0'


def _zero_literal(go_type: str) -> str:
  return {'string': '""', 'bool': 'false'}.get(go_type, '0')


__all__ = ['PROTOS_DIR', 'go_camel_case', 'go_field_name', 'render_grpc_endpoint']
