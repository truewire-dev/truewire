"""One gRPC endpoint module (ADR 0017): aliases for the `prost` messages the call sends and
returns, and a struct holding its core as an `Arc<dyn GrpcEndpoint<Meta>>`, with the method
and, when the plan resolved a pagination, its `<method>_paged` walker.

The messages are the stubs `truewire protos rust` builds from `spec/proto/` into
`<package>/protos/`, named here the way `prost-build` names them (`prost_names`). The
method takes the `Request` message by value, encodes it, hands the core a `GrpcCall` with
the method's HTTP/2 path, and decodes the reply into `Response` (`grpc_codec.rs`, one per
crate); there is no `_raw` twin, since a protobuf reply has no unvalidated form.

A walk clones the request, sets the driver path (creating nested messages with
`get_or_insert_with`) and reads rows, cursor and total straight off the `prost` fields.
Every statement is kept short enough that `rustfmt` leaves it as the printer lays it out.
"""
from collections.abc import Callable

from truewire.plan.model import EndpointPlan, PackagePlan, ProtoFieldPlan

from .contract import Contracts
from .endpoint import EndpointModule, Method, Tokens, _Skipped, core_tokens, emit_method, endpoint_file, method_docs, refusal
from .meta import META_FILE, MetaShape
from .names import snake_ident, string
from .printer import BANNER, MAX_WIDTH, Writer
from .prost_names import COPY_SCALARS, field_ident, is_option, type_path, value_type
from .types import CORE, Module

PAGED_SUFFIX = '_paged'
CODEC_FILE = 'grpc_codec.rs'
CODEC_MODULE = 'grpc_codec'
DECODE = 'decode_message'

Step = Callable[[Writer], None]
"""One statement of a generated body, written at the writer's current indentation."""


def codec_module() -> str:
  """`grpc_codec.rs`: the reply decoding every gRPC endpoint of the crate shares."""
  w = Writer()
  w.line(BANNER)
  w.line('//!')
  w.doc('What every gRPC endpoint of this crate shares: a reply decoded into the `prost` message it returns.', inner=True)
  w.blank()
  w.line('use std::any::type_name;')
  w.blank()
  w.line('use prost::{DecodeError, Message};')
  w.line(f'use {CORE}::{{Error, Result}};')
  w.blank()
  w.doc('`bytes` decoded as the message `M`; a `ValidationError` naming `M` when they do not decode.')
  with w.block(f'pub fn {DECODE}<M: Message + Default>(bytes: &[u8]) -> Result<M> {{'):
    w.line('M::decode(bytes).map_err(invalid::<M>)')
  w.blank()
  with w.block('fn invalid<M>(error: DecodeError) -> Error {'):
    w.line('let name = type_name::<M>();')
    w.line('Error::validation(format!("`{name}`: {error}"))')
  return w.render()


def _alias(w: Writer, name: str, path: str):
  """`pub type Name = path;`, broken after `=` as `rustfmt` does past `MAX_WIDTH`."""
  line = f'pub type {name} = {path};'
  if w.column + len(line) <= MAX_WIDTH:
    w.line(line)
    return
  w.line(f'pub type {name} =')
  with w.indented():
    w.line(f'{path};')


def render_grpc_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, struct_name: str, meta: MetaShape | None,
  contracts: Contracts | None = None,
) -> tuple[EndpointModule, list[str]]:
  """Render one unary gRPC endpoint's module; the second value lists what was left out."""
  contracts = contracts if contracts is not None else Contracts()
  grpc = endpoint.grpc
  if grpc is None:
    raise _Skipped('a gRPC endpoint with no `grpc` plan')
  if grpc.streaming != 'unary':
    raise _Skipped(f'a `{grpc.streaming}` streaming gRPC method has no Rust rendering yet')
  for message in (grpc.request, grpc.response):
    if message.kind != 'message' or message.file is None:
      raise _Skipped(f'`{message.name}` is declared by no file under spec/proto/')
  file = endpoint_file(endpoint.path)
  module = Module(plan, file, local={}, visible=[])
  w = module.writer
  notes: list[str] = []

  meta_type = meta.name if meta is not None else None
  bound = f'GrpcEndpoint<{meta_type}>' if meta_type is not None else 'GrpcEndpoint'
  holder = contracts.holder(module, [bound])
  for name in ('CallOptions', 'GrpcCall', 'Result'):
    module.core(name)
  module.imports.add('prost', 'Message')
  module.imports.add('std::sync', 'Arc')
  module.imports.add(f'crate::{CODEC_MODULE}', DECODE)
  if meta_type is not None:
    module.imports.add(f'crate::{META_FILE[:-3]}', meta_type)

  method = snake_ident(endpoint.path[-1], fallback='call')
  for name in ('Request', 'Response'):
    module.declare(name)
  w.blank()
  w.doc(f'The HTTP/2 path of `{grpc.service}/{grpc.rpc}`.')
  w.line(f'pub const METHOD: &str = {string(endpoint.wire.path or f"/{grpc.service}/{grpc.rpc}")};')
  w.blank()
  w.doc(f'The `{grpc.request.name}` message the call sends.')
  _alias(w, 'Request', type_path(grpc.request))
  w.blank()
  w.doc(f'The `{grpc.response.name}` message the call returns.')
  _alias(w, 'Response', type_path(grpc.response))

  params: list[tuple[str, Tokens]] = [('request', [('local', 'Request')]), ('options', core_tokens('CallOptions'))]
  returns: Tokens = [('core', 'Result'), ('text', '<'), ('local', 'Response'), ('text', '>')]
  main = Method(method, params, returns, is_async=True, doc=method_docs(endpoint), deprecated=endpoint.deprecated)

  paged: tuple[Method, list[str]] | None = None
  if endpoint.pagination is not None:
    state = module.checkpoint()
    try:
      paged = _paged(module, endpoint, method=method)
    except _Skipped as skipped:
      module.restore(state)
      notes.append(f'{endpoint.function}: {skipped.reason}')

  w.blank()
  w.doc(endpoint.docs.description)
  w.line('#[derive(Clone)]')
  with w.block(f'pub struct {struct_name} {{'):
    w.line(f'core: Arc<dyn {holder}>,')
  w.blank()
  methods: list[Method] = []
  with w.block(f'impl {struct_name} {{'):
    w.line(f'pub fn new(core: Arc<dyn {holder}>) -> Self {{')
    with w.indented():
      w.line('Self { core }')
    w.line('}')
    if paged is not None:
      signature, body = paged
      methods.append(signature)
      w.blank()
      # A walk reads fields a `.proto` may mark `deprecated`, which `prost` carries over.
      w.doc(*signature.doc)
      w.line('#[allow(deprecated)]')
      bare = Method(signature.name, signature.params, signature.returns, is_async=False, deprecated=signature.deprecated)
      emit_method(module, bare, qualifier=None, body=body)
    methods.append(main)
    w.blank()
    emit_method(module, main, qualifier=None, body=[*refusal(module, endpoint), *_call_body(endpoint, meta)])
  return EndpointModule(
    file=file, struct_name=struct_name, bound=bound, meta_type=meta_type, methods=methods,
    source=module.render(BANNER), kind='grpc', main=main.name, raw=None, takes_request=True,
  ), notes


def _lines(steps: list[Step], level: int) -> list[str]:
  """`steps` written at `level`, as lines relative to the method body."""
  w = Writer(level)
  for step in steps:
    step(w)
  return [line[4:] if line else line for line in w.render().rstrip('\n').split('\n')]


def _line(text: str) -> Step:
  return lambda w: w.line(text)


def _chain(prefix: str, root: str, elements: list[str], suffix: str = ';') -> Step:
  return lambda w: w.chain(prefix, root, elements, suffix)


def _call_body(endpoint: EndpointPlan, meta: MetaShape | None) -> list[str]:
  def body(w: Writer):
    if meta is not None:
      w.struct_literal('let meta = ', meta.name, meta.literal_fields(endpoint.meta), ';')
    fields = ['method: METHOD', 'request: request.encode_to_vec()', f'meta: {"&meta" if meta is not None else "&()"}', 'options']
    w.struct_literal('let call = ', 'GrpcCall', fields, ';')
    w.line('let reply = self.core.invoke(call).await?;')
    w.line(f'{DECODE}(&reply)')

  return _lines([body], 1)


# -- pagination -------------------------------------------------------------------------


def _through(hops: list[ProtoFieldPlan]):
  for hop in hops[:-1]:
    if hop.type.kind != 'message' or not is_option(hop):
      raise _Skipped(f'a walk through `{hop.name}` (not a singular message) has no Rust walker')


def _read(hops: list[ProtoFieldPlan]) -> tuple[list[str], str]:
  """The chain elements of a borrowed read of a scalar path, and the Rust type it yields.
  `_Skipped` for a path through anything but singular messages to a singular scalar."""
  leaf = hops[-1]
  rust = value_type(leaf)
  if rust is None or leaf.type.kind != 'scalar' or leaf.oneof is not None:
    raise _Skipped(f'`{".".join(hop.name for hop in hops)}` is not a scalar field a walk can read')
  _through(hops)
  copy = rust in COPY_SCALARS
  if len(hops) == 1:
    elements = [f'.{field_ident(leaf)}']
    if not copy:
      elements.append('.clone()')
    if is_option(leaf):
      elements.append('.unwrap_or_default()')
    return elements, rust
  access = f'value.{field_ident(leaf)}' + ('' if copy else '.clone()')
  elements = [f'.{field_ident(hops[0])}', '.as_ref()']
  elements.extend(f'.and_then(|value| value.{field_ident(hop)}.as_ref())' for hop in hops[1:-1])
  elements.append(f'.and_then(|value| {access})' if is_option(leaf) else f'.map(|value| {access})')
  elements.append('.unwrap_or_default()')
  return elements, rust


def _rows(hops: list[ProtoFieldPlan]) -> list[str]:
  """The chain elements of an owned read of the rows (a repeated field), moving out of the response."""
  leaf = hops[-1]
  if not leaf.repeated or leaf.map_key is not None:
    raise _Skipped(f'the rows `{leaf.name}` are not a repeated field')
  _through(hops)
  if len(hops) == 1:
    return [f'.{field_ident(leaf)}']
  elements = [f'.{field_ident(hops[0])}']
  elements.extend(f'.and_then(|value| value.{field_ident(hop)})' for hop in hops[1:-1])
  elements.extend((f'.map(|value| value.{field_ident(leaf)})', '.unwrap_or_default()'))
  return elements


def _assign(hops: list[ProtoFieldPlan], value: str) -> list[Step]:
  """Set the driver path on `request`, creating the messages on the way."""
  _through(hops)
  leaf = hops[-1]
  if value_type(leaf) is None or leaf.type.kind != 'scalar' or leaf.oneof is not None:
    raise _Skipped(f'a walk driven through `{leaf.name}` (not a scalar) has no Rust walker')
  steps: list[Step] = []
  parent = 'request'
  for hop in hops[:-1]:
    steps.append(_chain('let parent = ', parent, [f'.{field_ident(hop)}', '.get_or_insert_with(Default::default)']))
    parent = 'parent'
  steps.append(_line(f'{parent}.{field_ident(leaf)} = {f"Some({value})" if is_option(leaf) else value};'))
  return steps


def _to_i64(var: str, rust: str) -> list[Step]:
  if rust == 'i64':
    return []
  if rust in ('i32', 'u32'):
    return [_line(f'let {var} = i64::from({var});')]
  return [_chain(f'let {var} = ', f'i64::try_from({var})', ['.unwrap_or(i64::MAX)'])]


def _is_zero(var: str, rust: str) -> str:
  if rust in ('String', 'Vec<u8>'):
    return f'{var}.is_empty()'
  if rust == 'bool':
    return f'!{var}'
  if rust in ('f32', 'f64'):
    raise _Skipped('a floating-point cursor has no Rust walker')
  return f'{var} == 0'


_INTEGERS = ('i32', 'i64', 'u32', 'u64')


def _paged(module: Module, endpoint: EndpointPlan, *, method: str) -> tuple[Method, list[str]]:
  """The walker's signature and body, or `_Skipped` for a declaration with no Rust walker."""
  pagination, grpc = endpoint.pagination, endpoint.grpc
  assert pagination is not None and grpc is not None
  paging = grpc.paging
  if pagination.walker != 'paginated' or paging is None or paging.rows is None:
    raise _Skipped('its pagination resolves to no walk over the proto messages; call the method per page')
  kind = pagination.done.get('kind')
  state = value_type(paging.driver[-1])
  if state is None:
    raise _Skipped(f'a walk driven through `{pagination.driver}` has no Rust walker; call the method per page')
  before: list[Step] = []
  steps: list[Step] = [
    *_assign(paging.driver, 'state'),
    _chain('let response = ', 'endpoint', [f'.{method}(request, options)', '.await?']),
  ]
  rows_leaf = paging.rows[-1]
  rows_step = _chain('let rows = ', 'response', _rows(paging.rows))

  def size_steps():
    limit = _read(paging.size) if paging.size is not None else None
    if limit is None or limit[1] not in _INTEGERS:
      before.append(_line('let size: Option<usize> = None;'))
      return
    before.append(_chain('let limit = ', 'request', limit[0]))
    before.append(_chain('let size = ', 'usize::try_from(limit)', ['.ok()', '.filter(|size| *size > 0)']))

  if pagination.strategy == 'token':
    if paging.cursor is None:
      raise _Skipped('a token walk with no cursor path has no Rust walker')
    cursor, cursor_type = _read(paging.cursor)
    if cursor_type != state:
      raise _Skipped(f'the cursor `{pagination.cursor_from}` is not the driver\'s type; no Rust walker')
    done = _is_zero('following', state)
    if kind == 'empty':
      done = f'rows.is_empty() || {done}'
    steps.extend((
      _chain('let following = ', 'response', cursor),
      rows_step,
      _line(f'let done = {done};'),
      _line('Ok((rows, if done { None } else { Some(following) }))'),
    ))
    seed = {'Vec<u8>': 'Vec::new()', 'String': 'String::new()', 'bool': 'false'}.get(state, '0')
  elif pagination.strategy == 'page' and state in _INTEGERS:
    start = pagination.start if pagination.start is not None else 1
    if kind == 'total':
      if paging.total is None:
        raise _Skipped('a `total` walk with no total path has no Rust walker')
      total, total_type = _read(paging.total)
      if total_type not in _INTEGERS:
        raise _Skipped('a `total` that is not an integer has no Rust walker')
      module.imports.add(f'{CORE}::paging', 'total_reached')
      size_steps()
      counts = 'true' if pagination.done.get('counts') == 'pages' else 'false'
      steps.append(_chain('let total = ', 'response', total))
      steps.extend(_to_i64('total', total_type))
      steps.append(rows_step)
      steps.append(_line(f'let page = {"state" if state == "i64" else "i64::from(state)" if state in ("i32", "u32") else "i64::try_from(state).unwrap_or(i64::MAX)"};'))
      steps.append(_line(f'let reached = total_reached({counts}, page, {start}, size, rows.len(), total);'))
      steps.append(_line('let done = rows.is_empty() || reached;'))
    elif kind == 'short_page':
      module.imports.add(f'{CORE}::paging', 'exhausted')
      size_steps()
      steps.extend((rows_step, _line('let done = exhausted(rows.len(), size);')))
    elif kind == 'empty':
      steps.extend((rows_step, _line('let done = rows.is_empty();')))
    else:
      raise _Skipped(f'a `page` walk ended by `{kind}` has no Rust walker')
    steps.append(_line('Ok((rows, if done { None } else { Some(state + 1) }))'))
    seed = str(start)
  else:
    raise _Skipped(f'a `{pagination.strategy}` walk over gRPC has no Rust walker')

  if rows_leaf.type.kind == 'message':
    row_tokens: Tokens = [('local', 'Row')]
    module.declare('Row')
    module.writer.blank()
    module.writer.doc(f'One row of a page `{method}{PAGED_SUFFIX}` walks (`{pagination.rows}`).')
    _alias(module.writer, 'Row', type_path(rows_leaf.type))
  else:
    row_tokens = [('text', value_type(rows_leaf) or 'i32')]
  module.core('PaginatedResponse')

  def body(w: Writer):
    for step in before:
      step(w)
    w.line('let endpoint = self.clone();')
    with w.block(f'let next = move |state: {state}| {{', '};'):
      w.line('let endpoint = endpoint.clone();')
      w.line('let mut request = request.clone();')
      w.line('let options = options.clone();')
      with w.block('async move {'):
        for step in steps:
          step(w)
    w.line(f'PaginatedResponse::new({seed}, next)')

  docs = [
    *method_docs(endpoint)[:1],
    f'Paged variant of `{method}`: await it for every row, or walk `rows()`/`pages()` one page at a time.',
    *method_docs(endpoint)[1:],
  ]
  returns: Tokens = [('core', 'PaginatedResponse'), ('text', '<'), *row_tokens, ('text', f', {state}>')]
  signature = Method(
    f'{method}{PAGED_SUFFIX}', [('request', [('local', 'Request')]), ('options', core_tokens('CallOptions'))],
    returns, is_async=False, doc=docs, deprecated=endpoint.deprecated,
  )
  return signature, _lines([body], 1)


__all__ = ['CODEC_FILE', 'CODEC_MODULE', 'DECODE', 'codec_module', 'render_grpc_endpoint']
